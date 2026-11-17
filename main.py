"""CLI for the charter-quote workflow. See README.md for usage."""
import argparse
import os
import sys

from engine import PAUSED, Step, WorkflowEngine, WorkflowError, format_event
from mock_data import CHARTER_REQUEST
from tools import MockTools

RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs")


def build_steps(tools):
    """The workflow definition. 'Wait for human approval' is not a handler:
    it is the requires_approval gate the engine enforces before the send step."""
    return [
        Step("availability", "Check availability", tools.check_availability,
             "Aircraft availability received", max_attempts=3),
        Step("quote", "Prepare quote", tools.prepare_quote,
             "Draft prepared", max_attempts=3),
        # max_attempts=1: sending is not safe to repeat, so it is never auto-retried.
        Step("send", "Send quote (mock)", tools.send_quote,
             "Mock send completed", max_attempts=1, requires_approval=True),
    ]


def build_engine(fail_times=0):
    return WorkflowEngine(build_steps(MockTools(fail_times)), RUNS_DIR)


def print_quote(quote):
    print(f"\n--- Draft quote {quote['quote_id']} ---")
    print(f"Client:   {quote['client_name']} <{quote['client_email']}>")
    print(f"Trip:     {quote['route']} on {quote['date']}, {quote['passengers']} passengers")
    print(f"Aircraft: {quote['aircraft']}")
    for item in quote["line_items"]:
        print(f"  {item['description']:<34}${item['amount']:>12,.2f}")
    print(f"  {'Total':<34}${quote['total']:>12,.2f}\n")


def print_how_to_decide(request_id):
    print("Nothing has been sent. To continue:")
    print(f"  python main.py approve {request_id}")
    print(f"  python main.py reject {request_id}")


def interactive_approval(engine, request_id, ask=input):
    """Only exactly 'y' approves and exactly 'n' rejects. Anything else
    (Enter, other text, Ctrl+C, EOF) leaves the run PAUSED."""
    try:
        answer = ask("Approve this quote? [y/n] ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer == "y":
        return engine.approve(request_id)
    if answer == "n":
        return engine.reject(request_id)
    print("\nNo clear answer, so this counts as NOT approved. The run stays PAUSED.")
    print_how_to_decide(request_id)
    return engine.load(request_id)


def cmd_run(args):
    engine = build_engine(args.fail_times)
    state = engine.start(CHARTER_REQUEST)
    if state["status"] == PAUSED:
        print_quote(state["context"]["quote"])
        if args.interactive:
            interactive_approval(engine, state["request_id"])
        else:
            print_how_to_decide(state["request_id"])


def cmd_status(args):
    engine = build_engine()
    state = engine.load(args.request_id)
    if state is None:
        raise WorkflowError(f"No run found for {args.request_id}")
    print(f"Workflow {state['request_id']}: {state['status']}")
    print("Steps:")
    for step in engine.steps:
        print(f"  {step.name:<20}{state['steps'][step.key]}")
    if state["approval"]:
        print(f"Approval: {state['approval']['decision']} "
              f"(quote {state['approval']['quote_id']})")
    for key, message_id in state["sent"].items():
        print(f"Sent: {message_id} (idempotency key {key})")
    print("History:")
    for event in state["history"]:
        print(f"  {event['timestamp']}  {format_event(event)}")


def cmd_reset(args):
    if build_engine().reset(args.request_id):
        print(f"Deleted saved run for {args.request_id}")
    else:
        print(f"No run found for {args.request_id}; nothing to reset")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Charter quote workflow engine (all mocked)")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="start the workflow for REQ-1042")
    run.add_argument("--fail-times", type=int, default=1,
                     help="how many times the availability API times out (default 1)")
    run.add_argument("--interactive", action="store_true",
                     help="ask for approval in this terminal instead of stopping")
    for name in ("approve", "reject", "status", "reset"):
        commands.add_parser(name).add_argument("request_id")
    args = parser.parse_args(argv)

    try:
        if args.command == "run":
            cmd_run(args)
        elif args.command == "approve":
            build_engine().approve(args.request_id)
        elif args.command == "reject":
            build_engine().reject(args.request_id)
        elif args.command == "status":
            cmd_status(args)
        elif args.command == "reset":
            cmd_reset(args)
    except WorkflowError as error:
        print(f"Error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
