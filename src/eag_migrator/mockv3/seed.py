"""The reference data a fresh v3 tenant ships with, from the survey.

The UUIDs here are fixed and obviously synthetic, so a mapping can point at
them with `const:` and a rehearsal is reproducible. The enum codes are the
ones the survey confirmed (customerType 9 = Cash, paymentTerms 1 = NET30);
the rest are the labels in the order the survey saw them, which is a guess at
their codes and is marked as such.
"""

from __future__ import annotations

# Fixed, synthetic, and unmistakable as real v7 UUIDs.
PP_DEFAULT = "11111111-1111-4111-8111-111111111111"
LOC_DEFAULT = "22222222-2222-4222-8222-222222222222"
USER_OWNER = "33333333-3333-4333-8333-333333333333"

PRICING_PROFILES = [
    {"key": PP_DEFAULT, "profileName": "Default", "isDefault": True},
]

LOCATIONS = [
    {"key": LOC_DEFAULT, "name": "Default", "isDefault": True},
]

USERS = [
    {"key": USER_OWNER, "id": USER_OWNER, "name": "Owner", "email": "owner@example.com"},
]

INSTALLERS: list[dict] = []

# termsCode + a fixed key + the integer the survey saw (only NET30=1 confirmed).
PAYMENT_TERMS = [
    {"key": "44444444-0000-4000-8000-000000000001", "termsCode": "NET30",
     "name": "Net 30 Days", "code": 1, "active": True},
    {"key": "44444444-0000-4000-8000-000000000002", "termsCode": "2%10NET30",
     "name": "2% 10 Days Net 30", "code": 2, "active": True},
    {"key": "44444444-0000-4000-8000-000000000003", "termsCode": "2%10PROX",
     "name": "2% 10th Prox", "code": 3, "active": True},
    {"key": "44444444-0000-4000-8000-000000000004", "termsCode": "DOR",
     "name": "Due on Receipt", "code": 4, "active": True},
    {"key": "44444444-0000-4000-8000-000000000005", "termsCode": "CASH",
     "name": "Cash", "code": 5, "active": True},
    {"key": "44444444-0000-4000-8000-000000000006", "termsCode": "COD",
     "name": "COD", "code": 6, "active": True},
    {"key": "44444444-0000-4000-8000-000000000007", "termsCode": "NET60",
     "name": "Net 60 Days", "code": 7, "active": False},
    {"key": "44444444-0000-4000-8000-000000000008", "termsCode": "NET90",
     "name": "Net 90 Days", "code": 8, "active": True},
]

# Only Cash=9 is confirmed; the rest are the survey's order, which may or may
# not be the code. The mock validates membership, not the exact number.
CUSTOMER_TYPES = [
    {"code": 1, "label": "Insurance"},
    {"code": 2, "label": "Agent"},
    {"code": 3, "label": "Body Shop"},
    {"code": 4, "label": "Fleets/Car Rentals"},
    {"code": 5, "label": "Other Commercial"},
    {"code": 6, "label": "Auto Dealers"},
    {"code": 7, "label": "Retail Customer"},
    {"code": 8, "label": "Government"},
    {"code": 9, "label": "Cash"},
]

JOB_STATUS = [
    {"code": 1, "label": "Quote"},
    {"code": 2, "label": "Work Order"},
    {"code": 3, "label": "Invoice"},
]

JOB_TYPES = [
    {"label": "Back Glass"},
    {"label": "Door Glass"},
    {"label": "Partition Glass"},
    {"label": "Quarter Glass"},
    {"label": "Roof Glass"},
    {"label": "Vent Glass"},
    {"label": "Windshield Glass"},
]

CAUSE_OF_LOSS = [
    "Rock from Road - No One at Fault",
    "Rock from Road - 3rd Party Fault License #",
    "Animal", "Tree Branch", "Other Object", "Collision",
    "Vandalism - 3rd Party Known", "Vandalism - 3rd Party Unknown",
    "Attempted Theft", "Theft", "Extreme Heat or Cold Weather",
    "Hail Storm", "Other Storm", "Unknown",
]

# The single seed customer a fresh tenant carries.
SEED_CUSTOMER = {
    "key": "00000000-0000-4000-8000-000000000001",
    "customerId": "CUST-0001",
    "customerName": "Default Customer",
    "customerType": 9,
    "paymentTerms": 1,
    "isSeed": True,
}

CUSTOMER_TYPE_CODES = {t["code"] for t in CUSTOMER_TYPES}
