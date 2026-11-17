"""Mock tools. Every outside service is faked here; nothing touches the network."""
import hashlib
import json

from mock_data import FEDERAL_EXCISE_TAX_RATE, FLEET, HANDLING_FEE, ROUTE_HOURS


class ToolError(Exception):
    """A tool failed in a way that retrying will not fix."""


class TransientToolError(ToolError):
    """A tool failed temporarily (e.g. a timeout). Safe to retry."""


class MockTools:
    """The three tools the workflow calls. Each takes the shared context dict
    and returns a JSON-serializable dict. Call counters exist for the tests."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times  # first N availability calls time out
        self.availability_calls = 0
        self.send_calls = 0

    def check_availability(self, context):
        self.availability_calls += 1
        if self.availability_calls <= self.fail_times:
            raise TransientToolError("Temporary API timeout")

        request = context["request"]
        route = (request["origin"], request["destination"])
        if route not in ROUTE_HOURS:
            raise ToolError(f"Unknown route {route[0]} -> {route[1]}")

        suitable = [
            aircraft for aircraft in FLEET
            if aircraft["available"] and aircraft["seats"] >= request["passengers"]
        ]
        if not suitable:
            raise ToolError("No suitable aircraft available")

        cheapest = min(suitable, key=lambda aircraft: aircraft["hourly_rate"])
        return {"aircraft": cheapest, "flight_hours": ROUTE_HOURS[route]}

    def prepare_quote(self, context):
        request = context["request"]
        aircraft = context["availability"]["aircraft"]
        hours = context["availability"]["flight_hours"]

        base = round(aircraft["hourly_rate"] * hours, 2)
        tax = round(base * FEDERAL_EXCISE_TAX_RATE, 2)
        quote = {
            "request_id": request["request_id"],
            "client_name": request["client_name"],
            "client_email": request["client_email"],
            "route": f"{request['origin']} -> {request['destination']}",
            "date": request["date"],
            "passengers": request["passengers"],
            "aircraft": f"{aircraft['tail_number']} ({aircraft['type']})",
            "line_items": [
                {"description": f"Charter: {hours} h x ${aircraft['hourly_rate']:,.2f}/h",
                 "amount": base},
                {"description": "Federal excise tax (7.5%)", "amount": tax},
                {"description": "Handling fee", "amount": HANDLING_FEE},
            ],
            "total": round(base + tax + HANDLING_FEE, 2),
        }
        # The id is a hash of the contents, so a changed quote gets a new id.
        contents = json.dumps(quote, sort_keys=True)
        quote["quote_id"] = "Q-" + hashlib.sha256(contents.encode()).hexdigest()[:8]
        return quote

    def send_quote(self, context):
        """Mock send: prints instead of emailing, returns a fake message id."""
        self.send_calls += 1
        quote = context["quote"]
        print(f"            (mock) Quote {quote['quote_id']} for ${quote['total']:,.2f} "
              f"\"sent\" to {quote['client_name']} <{quote['client_email']}>")
        return {"message_id": f"MOCK-MSG-{quote['quote_id']}",
                "recipient": quote["client_email"]}
