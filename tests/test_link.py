"""Reconstructing which customer a job belongs to.

v2 records no customer id on a job. The link has to be inferred, and a wrong
inference is silent — the job simply belongs to the wrong person in v3 and
nothing downstream complains. So these tests are mostly about what the linker
REFUSES to do.
"""
from __future__ import annotations

import sqlite3

import pytest

from eag_migrator.link import MATCH_COLUMN, METHOD_COLUMN, link_jobs_to_customers

CUSTOMER_COLS = ("id", "first_name", "last_name", "email", "phone", "alt_phone", "zip")
JOB_COLS = ("id", "customer_first_name", "customer_last_name", "customer_email",
            "customer_phone", "customer_zip")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "staging.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE customer_details ({','.join(CUSTOMER_COLS)})")
    conn.execute(f"CREATE TABLE jobs ({','.join(JOB_COLS)})")
    conn.commit()
    conn.close()
    return path


def _seed(path, customers, jobs):
    conn = sqlite3.connect(path)
    conn.executemany(
        f"INSERT INTO customer_details VALUES ({','.join('?' * len(CUSTOMER_COLS))})",
        customers)
    conn.executemany(
        f"INSERT INTO jobs VALUES ({','.join('?' * len(JOB_COLS))})", jobs)
    conn.commit()
    conn.close()


def _matches(path):
    conn = sqlite3.connect(path)
    rows = conn.execute(
        f'SELECT id, "{MATCH_COLUMN}", "{METHOD_COLUMN}" FROM jobs').fetchall()
    conn.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def test_a_phone_that_belongs_to_one_person_is_a_link(db):
    _seed(db,
          [("c1", "Dana", "Reyes", "d@x.com", "651-555-1000", None, "55119")],
          [("j1", "Dana", "Reyes", None, "(651) 555-1000", "55119")])
    report = link_jobs_to_customers(db)

    assert report.matched == 1
    assert _matches(db)["j1"] == ("c1", "phone")


def test_one_person_recorded_twice_in_v2_is_not_ambiguity(db):
    """v2 holds ~294 duplicate rows. Four rows for one person is a match."""
    _seed(db,
          [("c1", "Dana", "Reyes", None, "6515551000", None, "55119"),
           ("c2", "Dana", "Reyes", None, "6515551000", None, "55119")],
          [("j1", "Dana", "Reyes", None, "6515551000", "55119")])
    report = link_jobs_to_customers(db)

    assert report.matched == 1
    assert _matches(db)["j1"][0] in {"c1", "c2"}


def test_a_phone_shared_by_two_different_people_is_refused(db):
    """A household. Picking one would put the work on the wrong person."""
    _seed(db,
          [("c1", "Dana", "Reyes", None, "6515551000", None, "55119"),
           ("c2", "Sam", "Oyelaran", None, "6515551000", None, "55119")],
          [("j1", "Nobody", "Here", None, "6515551000", "99999")])
    report = link_jobs_to_customers(db)

    assert report.matched == 0
    assert report.ambiguous == 1
    assert _matches(db)["j1"] == (None, None)


def test_a_shared_phone_falls_through_to_a_signal_that_does_identify_someone(db):
    _seed(db,
          [("c1", "Dana", "Reyes", "dana@x.com", "6515551000", None, "55119"),
           ("c2", "Sam", "Oyelaran", "sam@x.com", "6515551000", None, "55119")],
          [("j1", "Dana", "Reyes", "dana@x.com", "6515551000", "55119")])
    report = link_jobs_to_customers(db)

    assert _matches(db)["j1"] == ("c1", "email")
    assert report.by_method == {"email": 1}


def test_two_different_people_sharing_a_name_and_postcode_are_refused(db):
    """The dangerous case: the signal IS the name, so 'same name means same
    person' proves nothing. Without this the job lands on whichever was first."""
    _seed(db,
          [("c1", "Dana", "Reyes", None, None, None, "55119"),
           ("c2", "Dana", "Reyes", None, None, None, "55119")],
          [("j1", "Dana", "Reyes", None, None, "55119")])
    report = link_jobs_to_customers(db)

    assert report.matched == 0
    assert report.ambiguous == 1


def test_a_name_alone_is_never_enough(db):
    """Same name, different postcode: not a link."""
    _seed(db,
          [("c1", "Dana", "Reyes", None, None, None, "55119")],
          [("j1", "Dana", "Reyes", None, None, "99999")])
    report = link_jobs_to_customers(db)

    assert report.matched == 0
    assert report.unmatched == 1


def test_a_job_for_someone_v2_never_recorded_is_left_alone(db):
    _seed(db,
          [("c1", "Dana", "Reyes", None, "6515551000", None, "55119")],
          [("j1", "Unknown", "Person", None, "6515559999", "00000")])
    report = link_jobs_to_customers(db)

    assert report.matched == 0
    assert report.unmatched == 1


def test_running_it_twice_gives_the_same_answer(db):
    """It rewrites in place, so a re-run must not accumulate or drift."""
    _seed(db,
          [("c1", "Dana", "Reyes", None, "6515551000", None, "55119"),
           ("c2", "Dana", "Reyes", None, "6515551000", None, "55119")],
          [("j1", "Dana", "Reyes", None, "6515551000", "55119")])
    first = link_jobs_to_customers(db)
    first_match = _matches(db)
    second = link_jobs_to_customers(db)

    assert (first.matched, first.ambiguous) == (second.matched, second.ambiguous)
    assert _matches(db) == first_match


def test_a_link_that_no_longer_holds_is_cleared_rather_than_left_behind(db):
    _seed(db,
          [("c1", "Dana", "Reyes", None, "6515551000", None, "55119")],
          [("j1", "Dana", "Reyes", None, "6515551000", "55119")])
    link_jobs_to_customers(db)
    assert _matches(db)["j1"][0] == "c1"

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM customer_details")
    conn.commit()
    conn.close()
    link_jobs_to_customers(db)

    assert _matches(db)["j1"] == (None, None)
