"""All fictional data used by the mock tools. Nothing here is real."""

CHARTER_REQUEST = {
    "request_id": "REQ-1042",
    "client_name": "Jordan Reyes",
    "client_email": "jordan.reyes@example.com",
    "origin": "KVNY",
    "destination": "KTEB",
    "date": "2026-12-05",
    "passengers": 4,
}

# One aircraft is unavailable and one is too small for 4 passengers,
# so the availability step really has to filter.
FLEET = [
    {"tail_number": "N101AV", "type": "Gulfstream G280", "seats": 10,
     "hourly_rate": 6800.0, "available": False},
    {"tail_number": "N202AV", "type": "Cirrus Vision Jet", "seats": 3,
     "hourly_rate": 2400.0, "available": True},
    {"tail_number": "N303AV", "type": "Challenger 350", "seats": 9,
     "hourly_rate": 6200.0, "available": True},
]

# Hardcoded flight times in hours, keyed by (origin, destination).
ROUTE_HOURS = {("KVNY", "KTEB"): 5.0}

FEDERAL_EXCISE_TAX_RATE = 0.075
HANDLING_FEE = 500.0
