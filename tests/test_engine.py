"""Engine tests. Run from the repo root with: python -m unittest -v"""
import contextlib
import io
import tempfile
import unittest

from engine import (ABORTED, COMPLETE, FAILED, PAUSED, PENDING, REJECTED, SKIPPED, SUCCESS,
                    Step, WorkflowEngine, WorkflowError)
from main import build_steps, interactive_approval
from mock_data import CHARTER_REQUEST
from tools import MockTools, ToolError

REQUEST_ID = CHARTER_REQUEST["request_id"]


class EngineTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.runs_dir = temp_dir.name
        self.sleeps = []  # backoff delays the engine asked for (no real waiting)
        # Hide the engine's console output so the test report stays readable.
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)

    def make_engine(self, tools):
        """A new engine each time, like a separate CLI process would create."""
        return WorkflowEngine(build_steps(tools), self.runs_dir, sleep=self.sleeps.append)

    def run_until_paused(self, fail_times=0):
        tools = MockTools(fail_times)
        state = self.make_engine(tools).start(CHARTER_REQUEST)
        self.assertEqual(state["status"], PAUSED)
        return tools

    def test_happy_path_with_one_transient_failure(self):
        tools = self.run_until_paused(fail_times=1)
        state = self.make_engine(tools).approve(REQUEST_ID)

        self.assertEqual(state["status"], COMPLETE)
        self.assertEqual(tools.send_calls, 1)
        self.assertEqual(tools.availability_calls, 2)  # not re-run after resume
        self.assertEqual(
            [event["tag"] for event in state["history"]],
            ["START", "RUNNING", "FAILED", "RETRY", "SUCCESS", "RUNNING", "SUCCESS",
             "PAUSED", "APPROVED", "RUNNING", "SUCCESS", "COMPLETE"])

    def test_three_failures_abort_and_never_send(self):
        tools = MockTools(fail_times=3)
        state = self.make_engine(tools).start(CHARTER_REQUEST)

        self.assertEqual(state["status"], ABORTED)
        self.assertEqual(state["steps"],
                         {"availability": FAILED, "quote": SKIPPED, "send": SKIPPED})
        self.assertEqual(tools.send_calls, 0)

    def test_send_does_not_run_while_paused(self):
        tools = self.run_until_paused()
        saved = self.make_engine(tools).load(REQUEST_ID)

        self.assertEqual(saved["status"], PAUSED)
        self.assertEqual(saved["steps"]["send"], PENDING)
        self.assertEqual(saved["sent"], {})
        self.assertEqual(tools.send_calls, 0)

    def test_reject_ends_rejected_and_never_sends(self):
        tools = self.run_until_paused()
        state = self.make_engine(tools).reject(REQUEST_ID)

        self.assertEqual(state["status"], REJECTED)
        self.assertEqual(state["steps"]["send"], SKIPPED)
        self.assertEqual(tools.send_calls, 0)

    def test_approving_a_complete_run_does_not_send_again(self):
        tools = self.run_until_paused()
        engine = self.make_engine(tools)
        engine.approve(REQUEST_ID)
        before = engine.load(REQUEST_ID)

        with self.assertRaises(WorkflowError):
            engine.approve(REQUEST_ID)
        self.assertEqual(tools.send_calls, 1)
        self.assertEqual(engine.load(REQUEST_ID), before)  # nothing changed

    def test_idempotency_key_blocks_a_second_send(self):
        # Force the worst case: the send happened, but the state file claims the
        # run is still waiting to send. The recorded key must stop a re-send.
        tools = self.run_until_paused()
        engine = self.make_engine(tools)
        state = engine.approve(REQUEST_ID)
        state["status"], state["approval"] = PAUSED, None
        state["steps"]["send"] = PENDING
        engine.save(state)

        state = engine.approve(REQUEST_ID)
        self.assertEqual(state["status"], COMPLETE)
        self.assertEqual(state["steps"]["send"], SKIPPED)
        self.assertEqual(tools.send_calls, 1)

    def test_quote_id_mismatch_aborts_without_sending(self):
        tools = self.run_until_paused()
        state = self.make_engine(tools).approve(REQUEST_ID, quote_id="Q-stale000")

        self.assertEqual(state["status"], ABORTED)
        self.assertEqual(tools.send_calls, 0)

    def test_unclear_interactive_input_stays_paused(self):
        tools = self.run_until_paused()
        engine = self.make_engine(tools)

        def raises(error):
            def ask(prompt):
                raise error
            return ask

        answers = [lambda prompt: "", lambda prompt: "yes", lambda prompt: "Y",
                   lambda prompt: "maybe", raises(EOFError()), raises(KeyboardInterrupt())]
        for ask in answers:
            state = interactive_approval(engine, REQUEST_ID, ask=ask)
            self.assertEqual(state["status"], PAUSED)
            self.assertEqual(tools.send_calls, 0)

    def test_interactive_y_approves_and_sends_once(self):
        tools = self.run_until_paused()
        state = interactive_approval(self.make_engine(tools), REQUEST_ID, ask=lambda prompt: "y")

        self.assertEqual(state["status"], COMPLETE)
        self.assertEqual(tools.send_calls, 1)

    def test_attempt_numbering_and_backoff(self):
        tools = self.run_until_paused(fail_times=2)
        history = self.make_engine(tools).load(REQUEST_ID)["history"]
        retries = [event["message"] for event in history if event["tag"] == "RETRY"]

        self.assertEqual(retries, ["Attempt 2", "Attempt 3"])
        self.assertEqual(tools.availability_calls, 3)
        self.assertEqual(self.sleeps, [0.2, 0.4])

    def test_never_exceeds_max_attempts_or_retries_after_last_failure(self):
        tools = MockTools(fail_times=10)
        state = self.make_engine(tools).start(CHARTER_REQUEST)
        tags = [event["tag"] for event in state["history"]]

        self.assertEqual(tools.availability_calls, 3)
        self.assertEqual(tags.count("RETRY"), 2)
        self.assertEqual(tags[-2:], ["FAILED", "ABORTED"])  # no RETRY after final failure

    def test_non_transient_error_is_not_retried(self):
        calls = []

        def broken(context):
            calls.append(1)
            raise ToolError("Bad request")

        engine = WorkflowEngine([Step("broken", "Broken step", broken, "ok", max_attempts=3)],
                                self.runs_dir, sleep=self.sleeps.append)
        state = engine.start(CHARTER_REQUEST)

        self.assertEqual(state["status"], ABORTED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_approve_or_reject_without_a_paused_run_is_an_error(self):
        engine = self.make_engine(MockTools())
        with self.assertRaises(WorkflowError):
            engine.approve(REQUEST_ID)  # no run at all
        with self.assertRaises(WorkflowError):
            engine.reject(REQUEST_ID)
        self.assertIsNone(engine.load(REQUEST_ID))


if __name__ == "__main__":
    unittest.main()
