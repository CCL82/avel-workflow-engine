"""Workflow engine: runs steps in order, retries transient failures, pauses for
human approval, and saves all state to a JSON file so a run can be resumed."""
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from tools import ToolError, TransientToolError

# Step statuses
PENDING, RUNNING, SUCCESS, FAILED, SKIPPED = "PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED"
# Workflow statuses (RUNNING is shared with the step statuses)
PAUSED, COMPLETE, REJECTED, ABORTED = "PAUSED", "COMPLETE", "REJECTED", "ABORTED"

BACKOFF_SECONDS = 0.2  # doubles after each failed attempt: 0.2s, 0.4s, ...


class WorkflowError(Exception):
    """The requested action is not valid for the run's current state."""


@dataclass
class Step:
    key: str                         # where the step's output is stored in context
    name: str                        # shown in the console and history
    handler: Callable[[dict], dict]  # takes the context, returns the step's output
    success_message: str
    max_attempts: int = 1
    requires_approval: bool = False  # pause before this step until a human approves


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_event(event):
    tag = f"[{event['tag']}]"
    return f"{tag:<12}{event['message']}"


class WorkflowEngine:
    def __init__(self, steps, runs_dir="runs", sleep=time.sleep):
        self.steps = steps
        self.runs_dir = runs_dir
        self.sleep = sleep  # injectable so tests do not really wait

    # ---- state persistence ----

    def _path(self, request_id):
        return os.path.join(self.runs_dir, f"{request_id}.json")

    def load(self, request_id):
        """Return the saved state dict, or None if there is no such run."""
        if not os.path.exists(self._path(request_id)):
            return None
        with open(self._path(request_id)) as state_file:
            return json.load(state_file)

    def save(self, state):
        os.makedirs(self.runs_dir, exist_ok=True)
        path = self._path(state["request_id"])
        # Write to a temp file, then rename, so a crash never leaves half a file.
        with open(path + ".tmp", "w") as state_file:
            json.dump(state, state_file, indent=2)
        os.replace(path + ".tmp", path)

    def reset(self, request_id):
        """Delete the state file. Returns False if there was nothing to delete."""
        if self.load(request_id) is None:
            return False
        os.remove(self._path(request_id))
        return True

    def log(self, state, tag, message, step=None):
        """Append a history event, print it, and save. Every status change is
        followed by a log call, so the state file is always up to date."""
        event = {"timestamp": now(), "step": step, "tag": tag, "message": message}
        state["history"].append(event)
        print(format_event(event))
        self.save(state)

    # ---- public actions ----

    def start(self, request):
        request_id = request["request_id"]
        if self.load(request_id) is not None:
            raise WorkflowError(f"A run for {request_id} already exists. "
                                f"Use: python main.py reset {request_id}")
        state = {
            "request_id": request_id,
            "status": RUNNING,
            "steps": {step.key: PENDING for step in self.steps},
            "context": {"request": dict(request)},  # shared data passed between steps
            "approval": None,                       # set by approve() / reject()
            "sent": {},                             # idempotency key -> message_id
            "history": [],
        }
        self.log(state, "START", f"Workflow {request_id}")
        return self._run_steps(state)

    def approve(self, request_id, quote_id=None):
        """Record approval of a quote (default: the paused draft) and resume."""
        state = self._load_paused(request_id)
        if quote_id is None:
            quote_id = state["context"]["quote"]["quote_id"]
        state["approval"] = {"decision": "APPROVED", "quote_id": quote_id, "timestamp": now()}
        state["status"] = RUNNING
        self.log(state, "APPROVED", "Human approves draft")
        return self._run_steps(state)

    def reject(self, request_id):
        state = self._load_paused(request_id)
        quote_id = state["context"]["quote"]["quote_id"]
        state["approval"] = {"decision": "REJECTED", "quote_id": quote_id, "timestamp": now()}
        state["status"] = REJECTED
        self._skip_pending_steps(state)
        self.log(state, "REJECTED", "Human rejects draft; nothing was sent")
        return state

    # ---- internals ----

    def _load_paused(self, request_id):
        state = self.load(request_id)
        if state is None:
            raise WorkflowError(f"No run found for {request_id}")
        if state["status"] != PAUSED:
            raise WorkflowError(f"Run {request_id} is {state['status']}, not PAUSED; "
                                f"nothing changed")
        return state

    def _run_steps(self, state):
        for step in self.steps:
            if state["steps"][step.key] == SUCCESS:
                continue  # done in an earlier process; reuse its saved output

            send_key = None
            if step.requires_approval:
                # Gate 1: no approval on record -> save and stop here.
                if state["approval"] is None:
                    state["status"] = PAUSED
                    self.log(state, "PAUSED", "Waiting for human approval", step.name)
                    return state
                # Gate 2: the approval must be for exactly this quote.
                quote_id = state["context"]["quote"]["quote_id"]
                if state["approval"]["quote_id"] != quote_id:
                    return self._abort(state, step, "Approved quote_id does not match "
                                                    "the current quote; nothing was sent")
                # Gate 3: never send the same quote for the same request twice.
                send_key = f"{state['request_id']}-{quote_id}"
                if send_key in state["sent"]:
                    state["steps"][step.key] = SKIPPED
                    self.log(state, "SUCCESS", f"Already sent ({send_key}); send skipped",
                             step.name)
                    continue

            state["steps"][step.key] = RUNNING
            self.log(state, "RUNNING", step.name, step.name)
            try:
                result = self._call_with_retry(state, step)
            except ToolError as error:
                return self._abort(state, step, str(error))

            state["context"][step.key] = result
            state["steps"][step.key] = SUCCESS
            if send_key is not None:
                state["sent"][send_key] = result["message_id"]
            self.log(state, "SUCCESS", step.success_message, step.name)

        state["status"] = COMPLETE
        self.log(state, "COMPLETE", "Workflow finished")
        return state

    def _call_with_retry(self, state, step):
        """Return the handler's output, or raise ToolError once we give up."""
        for attempt in range(1, step.max_attempts + 1):
            try:
                return step.handler(state["context"])
            except TransientToolError as error:
                self.log(state, "FAILED", str(error), step.name)
                if attempt == step.max_attempts:
                    raise ToolError(f"{step.name} failed after {attempt} attempt(s)")
                self.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
                self.log(state, "RETRY", f"Attempt {attempt + 1}", step.name)
            except ToolError as error:
                # Not transient: retrying would not help, so give up right away.
                self.log(state, "FAILED", str(error), step.name)
                raise

    def _abort(self, state, step, reason):
        state["steps"][step.key] = FAILED
        self._skip_pending_steps(state)
        state["status"] = ABORTED
        self.log(state, "ABORTED", f"{reason}. Remaining steps skipped", step.name)
        return state

    def _skip_pending_steps(self, state):
        for key, status in state["steps"].items():
            if status == PENDING:
                state["steps"][key] = SKIPPED
