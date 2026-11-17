# Charter Quote Workflow Engine

A small execution engine that runs a four-step workflow for a fictional charter
inquiry (REQ-1042): check aircraft availability → prepare a quote → wait for
human approval → send the approved quote. Every outside service is mocked and
nothing is actually sent.

## Run it

Needs Python 3.10+ and nothing else (standard library only).

```bash
python main.py run                    # runs until the approval pause (1 simulated timeout)
python main.py approve REQ-1042       # or: python main.py reject REQ-1042
python main.py status REQ-1042        # current status + full history
python main.py reset REQ-1042         # delete the saved run so you can start again

python main.py run --fail-times 3     # retries exhausted -> ABORTED
python main.py run --interactive      # ask y/n in the terminal instead of stopping
python -m unittest -v                 # tests
```

## Architecture

```
main.py (CLI) ──> engine.py (WorkflowEngine) ──> Step handlers ──> tools.py (mocks) ──> mock_data.py
                        │
                        └──> runs/REQ-1042.json  (status, step statuses, context, approval, sent keys, history)
```

- **Steps** are data: name, handler, `max_attempts`, `requires_approval`. The engine runs them in order.
- **Context** is one shared dict. Each step reads earlier outputs and adds its own
  (`availability` → `quote` → `send`).
- **Approval** is not a handler. It is a gate the engine enforces before any step
  marked `requires_approval` (the send step).
- **History**: every event is `{timestamp, step, tag, message}`, printed and saved.
  The state file is rewritten after every event.

**Data:** All tool responses are mocked from mock_data.py. In production,
check_availability would call a fleet/scheduling API and the quote would use real pricing rules.

## Failure handling

- `TransientToolError` (e.g. a timeout) is retried up to `max_attempts` (3) with
  exponential backoff: 0.2s, then 0.4s. `--fail-times N` makes the availability
  tool time out on its first N calls, deterministically.
- A plain `ToolError` is not retried; the step fails immediately.
- When a step fails for good, the workflow is `ABORTED` and later steps are skipped, so send never runs.
- The send step is never auto-retried (`max_attempts=1`), because sending is not
  safe to repeat. It is also protected by an idempotency key
  (`request_id-quote_id`) and by a check that the approved `quote_id` matches the quote being sent.

## Key decision

State is persisted at the approval pause, so approval is a separate, explicit
action (a later command, possibly another process) and not a blocking `input()`.
A real agent may wait hours for a human, and a crash during that wait must not
lose the run or cause an unapproved send. On resume, finished steps are not
re-run; the saved availability and quote are reused.

## Limitation

Single-process, file-based state with no locking, so two simultaneous approvals
could race. In production, use a database with transactional state transitions
and a durable idempotency store.

## Evidence

Real console output is saved in [evidence/](evidence/): `success_run.txt`,
`failure_run.txt` (retries exhausted → ABORTED), `rejected_run.txt`, and `tests.txt`.
