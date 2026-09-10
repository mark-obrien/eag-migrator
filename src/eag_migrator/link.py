"""Work out which harvested customer each harvested job belongs to.

v2 does not say. The job carries the customer's details copied onto it, but no
customer id, and the one endpoint that knows the answer
(/customer/jobquotesmodal) covers about 2% of the work. So the link has to be
reconstructed from the data — which is exactly the kind of inference that goes
wrong silently, a job landing on the wrong person with nothing to notice it.

Three rules keep that in check:

  * Only evidence that identifies ONE person is used. Anything ambiguous is
    left unmatched rather than guessed, and an unmatched job still migrates —
    v3 creates a customer from the details the job carries.
  * The strongest available signal wins: phone, then email, then name+postcode.
    A bare name is never enough; 204 of v2's 628 distinct names belong to more
    than one person.
  * Several v2 rows for the SAME person is not ambiguity. v2 holds roughly 294
    duplicate rows, so a phone matching four rows that are all one person is a
    match, not a coin toss. Rows for DIFFERENT people sharing a phone — a
    household — are not.

The result is written to the staging database so it can be read, counted and
argued with before any of it reaches v3.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MATCH_COLUMN = "matched_customer_id"
METHOD_COLUMN = "match_method"


@dataclass
class LinkReport:
    jobs: int = 0
    customers: int = 0
    matched: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    ambiguous: int = 0
    unmatched: int = 0

    @property
    def matched_pct(self) -> float:
        return 100.0 * self.matched / self.jobs if self.jobs else 0.0


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _person(row: dict[str, Any]) -> tuple[str, str]:
    return _norm(row.get("first_name")), _norm(row.get("last_name"))


def _resolve(
    candidates: set[str],
    people: dict[str, tuple[str, str]],
    *,
    keyed_on_name: bool = False,
) -> str | None:
    """One customer id, if the candidates are all the same person.

    `keyed_on_name` matters. Collapsing several candidates into one person is
    decided by comparing names — which proves nothing when the signal that
    produced them was itself the name. Two different people who share a name
    and a postcode would look like one person recorded twice, and the job would
    silently land on whichever was picked. So a name-keyed signal only counts
    when it produces a single candidate.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return next(iter(candidates))
    if keyed_on_name:
        return None
    if len({people[c] for c in candidates}) == 1:
        # Same person, recorded more than once in v2. Any of them is the right
        # person; the lowest id keeps the choice stable across re-runs. Ids
        # arrive as ints from a JSON detail and as text from a scraped table,
        # so order on length-then-value rather than on the raw type.
        return sorted(candidates, key=lambda c: (len(str(c)), str(c)))[0]
    return None


def link_jobs_to_customers(db_path: Path) -> LinkReport:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        customers = [dict(r) for r in conn.execute("SELECT * FROM customer_details")]
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs")]

        people = {c["id"]: _person(c) for c in customers}
        by_phone: dict[str, set[str]] = defaultdict(set)
        by_email: dict[str, set[str]] = defaultdict(set)
        by_name_zip: dict[tuple[tuple[str, str], str], set[str]] = defaultdict(set)
        for c in customers:
            for phone in (c.get("phone"), c.get("alt_phone")):
                if len(_digits(phone)) == 10:
                    by_phone[_digits(phone)].add(c["id"])
            if _norm(c.get("email")):
                by_email[_norm(c["email"])].add(c["id"])
            if any(_person(c)):
                by_name_zip[(_person(c), _digits(c.get("zip")))].add(c["id"])

        for column in (MATCH_COLUMN, METHOD_COLUMN):
            try:
                conn.execute(f'ALTER TABLE jobs ADD COLUMN "{column}" TEXT')
            except sqlite3.OperationalError:
                pass  # already added by an earlier run
        conn.execute(
            f'UPDATE jobs SET "{MATCH_COLUMN}" = NULL, "{METHOD_COLUMN}" = NULL'
        )

        report = LinkReport(jobs=len(jobs), customers=len(customers))
        for job in jobs:
            signals = (
                ("phone", by_phone.get(_digits(job.get("customer_phone")), set())
                 if len(_digits(job.get("customer_phone"))) == 10 else set()),
                ("email", by_email.get(_norm(job.get("customer_email")), set())),
                ("name+zip", by_name_zip.get(
                    ((_norm(job.get("customer_first_name")),
                      _norm(job.get("customer_last_name"))),
                     _digits(job.get("customer_zip"))), set())),
            )
            saw_candidates = False
            for method, candidates in signals:
                if candidates:
                    saw_candidates = True
                match = _resolve(
                    candidates, people, keyed_on_name=method == "name+zip"
                )
                if match is not None:
                    conn.execute(
                        f'UPDATE jobs SET "{MATCH_COLUMN}"=?, "{METHOD_COLUMN}"=? '
                        f"WHERE id=?",
                        (match, method, job["id"]),
                    )
                    report.matched += 1
                    report.by_method[method] = report.by_method.get(method, 0) + 1
                    break
            else:
                # Candidates existed but pointed at different people: that is a
                # refusal to guess, and worth telling apart from "never heard
                # of them", because the two need different follow-up.
                if saw_candidates:
                    report.ambiguous += 1
                else:
                    report.unmatched += 1
        conn.commit()
        return report
    finally:
        conn.close()


def render_markdown(report: LinkReport) -> str:
    lines = ["# Linking jobs to customers\n"]
    lines.append(f"- Jobs: **{report.jobs:,}**  Customers: **{report.customers:,}**")
    lines.append(f"- Linked: **{report.matched:,}** ({report.matched_pct:.1f}%)")
    for method, count in sorted(report.by_method.items(), key=lambda kv: -kv[1]):
        lines.append(f"  - by {method}: {count:,}")
    lines.append(f"- Left unlinked because the evidence pointed at more than one "
                 f"person: **{report.ambiguous:,}**")
    lines.append(f"- Left unlinked because no customer matched at all: "
                 f"**{report.unmatched:,}**")
    lines.append("")
    lines.append("An unlinked job is not a lost job. It migrates carrying the "
                 "customer details v2 holds on it, and v3 creates a customer "
                 "from those. What it does not get is a link to the customer "
                 "record migrated from v2, so that person exists twice.")
    return "\n".join(lines)
