"""Guard rails for extracting from an app that takes payments.

Two jobs:

  * **Never let cardholder data reach disk.** A careless selector on a payments
    screen would otherwise drag PANs and CVVs into a SQLite file on a laptop,
    which drags the whole project into PCI DSS scope. Blocked at the staging
    layer, so no mapping mistake can undo it.
  * **Say what personal data was taken.** The harvest report lists the PII
    fields per collection, which is what a processing record or DPA needs.

This is a safety net, not a compliance programme. Migrating a payments app is
still a conversation to have with whoever is accountable for that data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Never stored, in any form. CVV/CVC must not be retained after authorisation
# under PCI DSS — not even encrypted.
FORBIDDEN_FIELDS = re.compile(
    r"(^|_)(cvv|cvc|csc|cvv2|cid|security_?code|card_?pin|pin_?block|"
    r"track_?[12]|magstripe|mag_?stripe|full_?pan|card_?verification)($|_)",
    re.IGNORECASE,
)

# Stored only as a masked remnant (last four digits).
CARD_NUMBER_FIELDS = re.compile(
    r"(^|_)(pan|card_?number|cc_?number|creditcard|credit_?card|"
    r"account_?number|acct_?number|card_?no)($|_)",
    re.IGNORECASE,
)

# Bank details: keep the last four of an account number, drop routing secrets.
BANK_FIELDS = re.compile(
    r"(^|_)(routing_?number|aba|iban|sort_?code|bank_?account)($|_)", re.IGNORECASE
)

# Reported, not blocked — this is the data the migration exists to move.
# Deliberately narrow: a report that cries wolf is a report nobody reads. Note
# `state` is only counted when qualified as an address field, since on a
# scheduling record `state` means scheduled/completed.
PII_FIELDS = re.compile(
    r"(^|_)(email|e_?mail|phone|mobile|cell|fax|first_?name|last_?name|full_?name|"
    r"name|address|street|address[12]|city|province|zip|postal|dob|"
    r"date_?of_?birth|ssn|social|licen[cs]e|driver_?licen[cs]e|policy_?number|"
    r"claim_?number|vin|insured)($|_)"
    r"|(^|_)(address|billing|shipping|mailing|home|work)_?state($|_)"
    r"|(^|_)state_(code|abbr\w*)($|_)",
    re.IGNORECASE,
)

SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Major card IIN prefixes. Requiring one of these alongside a Luhn check keeps
# false positives (order numbers, job ids) essentially at zero.
IIN = re.compile(
    r"^(4\d{12,18}"                      # Visa
    r"|5[1-5]\d{14}"                     # Mastercard
    r"|2(2[2-9]\d|[3-6]\d{2}|7[01]\d|720)\d{12}"  # Mastercard 2-series
    r"|3[47]\d{13}"                      # Amex
    r"|6(011|5\d{2}|4[4-9]\d)\d{12,15}"  # Discover
    r"|35(2[89]|[3-8]\d)\d{12,15}"       # JCB
    r"|3(0[0-5]|[68]\d)\d{11,16}"        # Diners
    r")$"
)


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for char in reversed(digits):
        if not char.isdigit():
            return False
        value = int(char)
        if alt:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alt = not alt
    return total % 10 == 0


def looks_like_pan(value: Any) -> str | None:
    """Return the bare digits if this really looks like a card number."""
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[ \-]", "", value.strip())
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return None
    if not IIN.match(digits):
        return None
    if not luhn_ok(digits):
        return None
    return digits


def mask_pan(digits: str) -> str:
    return f"•••• {digits[-4:]}"


@dataclass
class ScrubReport:
    blocked: dict[str, int] = field(default_factory=dict)
    """field -> how many rows had a forbidden value removed."""
    masked: dict[str, int] = field(default_factory=dict)
    """field -> how many rows had a card number reduced to its last four."""
    pii_fields: set[str] = field(default_factory=set)

    def merge(self, other: ScrubReport) -> None:
        for key, count in other.blocked.items():
            self.blocked[key] = self.blocked.get(key, 0) + count
        for key, count in other.masked.items():
            self.masked[key] = self.masked.get(key, 0) + count
        self.pii_fields |= other.pii_fields

    @property
    def clean(self) -> bool:
        return not self.blocked and not self.masked

    def summary(self) -> list[str]:
        out: list[str] = []
        for name, count in sorted(self.blocked.items()):
            out.append(f"dropped '{name}' from {count:,} row(s) — must never be stored")
        for name, count in sorted(self.masked.items()):
            out.append(f"masked '{name}' to last four digits in {count:,} row(s)")
        if self.pii_fields:
            out.append("personal data present: " + ", ".join(sorted(self.pii_fields)))
        return out


def scrub(row: dict[str, Any]) -> tuple[dict[str, Any], ScrubReport]:
    """Strip cardholder data out of one extracted record."""
    report = ScrubReport()
    clean: dict[str, Any] = {}

    for name, value in row.items():
        if name.startswith("_"):  # harvester bookkeeping
            clean[name] = value
            continue

        if PII_FIELDS.search(name):
            report.pii_fields.add(name)

        if FORBIDDEN_FIELDS.search(name):
            clean[name] = None
            report.blocked[name] = report.blocked.get(name, 0) + 1
            continue

        if BANK_FIELDS.search(name) and isinstance(value, str) and value.strip():
            digits = re.sub(r"\D", "", value)
            clean[name] = f"•••• {digits[-4:]}" if len(digits) >= 4 else None
            report.masked[name] = report.masked.get(name, 0) + 1
            continue

        if CARD_NUMBER_FIELDS.search(name) and isinstance(value, str) and value.strip():
            digits = re.sub(r"\D", "", value)
            clean[name] = mask_pan(digits) if len(digits) >= 4 else None
            report.masked[name] = report.masked.get(name, 0) + 1
            continue

        # Value-level sweep: a PAN in a field nobody thought to name carefully,
        # e.g. a free-text note field on a quote.
        pan = looks_like_pan(value)
        if pan:
            clean[name] = mask_pan(pan)
            report.masked[name] = report.masked.get(name, 0) + 1
            continue

        if isinstance(value, str) and SSN.search(value):
            clean[name] = SSN.sub("•••-••-####", value)
            report.masked[name] = report.masked.get(name, 0) + 1
            continue

        clean[name] = value

    return clean, report
