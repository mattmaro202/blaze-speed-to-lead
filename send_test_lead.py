"""Post a sample lead to the running webhook.

Usage:
    python send_test_lead.py [strong|borderline|junk]

Defaults to "strong".
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("WEBHOOK_BASE_URL", "http://127.0.0.1:8000")
ENDPOINT = f"{BASE_URL}/webhook/lead"

LEADS = {
    "strong": {
        "full_name": "Dana Reyes",
        "email": "dana@summitpm.com",
        "phone": "+1-512-555-0142",
        "company": "Summit Property Management",
        "num_doors": 210,
        "message": (
            "We manage about 210 units across Austin and our bookkeeping is a "
            "mess at month-end. Looking for something to automate invoicing and "
            "cash flow."
        ),
    },
    "borderline": {
        "full_name": "Marcus Lee",
        "email": "marcus@leerentals.com",
        "phone": "+1-206-555-0199",
        "company": "Lee Rentals",
        "num_doors": 25,
        "message": (
            "I self-manage around 25 rental units and spend way too long on "
            "spreadsheets. Not sure if I'm big enough for this?"
        ),
    },
    "junk": {
        "full_name": "Chris P.",
        "email": "chrisp123@gmail.com",
        "phone": "+1-305-555-0170",
        "company": "N/A",
        "num_doors": 1,
        "message": (
            "My landlord raised my rent again and won't fix the AC. Is this where "
            "I file a complaint??"
        ),
    },
}


def main() -> None:
    kind = sys.argv[1].lower() if len(sys.argv) > 1 else "strong"
    if kind not in LEADS:
        print(f"Unknown lead '{kind}'. Choose from: {', '.join(LEADS)}")
        sys.exit(1)

    lead = LEADS[kind]
    print(f"Posting '{kind}' lead to {ENDPOINT} ...")
    resp = requests.post(ENDPOINT, json=lead, timeout=10)
    print(f"HTTP {resp.status_code}: {resp.text}")
    print("Watch the server console for the pipeline timing.")


if __name__ == "__main__":
    main()
