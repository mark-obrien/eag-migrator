"""The dashboard.

Weighted towards the things that would actually hurt: leaking a session
credential into a page, starting a migration by accident, or letting a crafted
URL read files outside the reports directory.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard extras not installed")

from fastapi.testclient import TestClient  # noqa: E402

from eag_migrator.dashboard import app as dash  # noqa: E402
from eag_migrator.dashboard.jobs import JobBusy, JobManager  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    """Point every dashboard path at a scratch directory."""
    for name in ("CONFIG_DIR", "PROFILES_DIR", "REPORTS_DIR", "STATE_DIR"):
        folder = tmp_path / name.split("_")[0].lower()
        folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(dash, name, folder)

    state = tmp_path / "state"
    monkeypatch.setattr(dash, "STATE_DB", state / "migration.sqlite")
    monkeypatch.setattr(dash, "STAGING_DB", state / "staging.sqlite")
    monkeypatch.setattr(dash, "SESSION_FILE", state / "session.json")
    monkeypatch.setattr(dash, "MAPPING_FILE", tmp_path / "config" / "mapping.yaml")
    monkeypatch.setattr(dash, "MAPPING_DRAFT", tmp_path / "config" / "mapping.draft.yaml")
    monkeypatch.setattr(dash, "HARVEST_FILE", tmp_path / "config" / "harvest.yaml")
    monkeypatch.setattr(dash, "HARVEST_DRAFT", tmp_path / "config" / "harvest.draft.yaml")
    monkeypatch.setattr(dash, "jobs", JobManager())

    monkeypatch.delenv("V2_DATABASE_URL", raising=False)
    monkeypatch.delenv("V3_DATABASE_URL", raising=False)
    monkeypatch.delenv("EAGM_DASHBOARD_TOKEN", raising=False)
    return tmp_path


@pytest.fixture
def client(workspace):
    return TestClient(dash.create_app())


# --- it loads ---------------------------------------------------------------


def test_overview_renders_with_nothing_configured(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "EAG migrator" in res.text
    assert "not configured" in res.text


def test_status_endpoint_describes_the_whole_pipeline(client):
    body = client.get("/api/status").json()
    assert {"databases", "session", "harvest", "mapping", "staging", "runs"} <= set(body)
    assert body["mapping"]["present"] is False


# --- credentials never reach the browser ------------------------------------


def test_session_values_are_never_rendered(client, workspace):
    """The page shows that a session exists, never what it is."""
    from eag_migrator.web.session import Session

    session = Session.from_cookie_header("sid=SUPER-SECRET-VALUE", "https://v2.example")
    session.headers["Authorization"] = "Bearer TOP-SECRET-TOKEN"
    session.save(dash.SESSION_FILE)

    page = client.get("/").text
    assert "SUPER-SECRET-VALUE" not in page
    assert "TOP-SECRET-TOKEN" not in page
    assert "sid" in page                       # the name is fine
    assert "withheld" in page

    body = client.get("/api/status").json()
    assert "SUPER-SECRET-VALUE" not in str(body)
    assert "TOP-SECRET-TOKEN" not in str(body)


def test_database_passwords_are_stripped(client, monkeypatch):
    monkeypatch.setenv(
        "V2_DATABASE_URL", "postgresql+psycopg://eag:hunter2@db.internal:5432/eag"
    )
    page = client.get("/").text
    assert "hunter2" not in page
    assert "db.internal" in page               # host is useful, password is not


# --- destructive actions need confirming ------------------------------------


def test_migration_will_not_start_without_the_confirmation_word(client):
    res = client.post("/actions/run", data={"confirm": ""}, follow_redirects=False)
    assert res.status_code == 303
    assert "MIGRATE" in res.headers["location"]
    assert dash.jobs.current is None           # nothing was started


def test_rollback_will_not_start_without_the_confirmation_word(client):
    res = client.post(
        "/actions/rollback",
        data={"run_id": "whatever", "confirm": "yes"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "ROLLBACK" in res.headers["location"]
    assert dash.jobs.current is None


# --- path traversal ---------------------------------------------------------


def test_reports_cannot_be_walked_out_of(client, workspace):
    (workspace / "secret.md").write_text("private")
    (dash.REPORTS_DIR / "fine.md").write_text("# fine")

    assert client.get("/reports/reports/fine.md").status_code == 200
    for attempt in ("../secret.md", "..%2Fsecret.md", "....//secret.md"):
        assert client.get(f"/reports/reports/{attempt}").status_code == 404
    assert client.get("/reports/etc/passwd").status_code == 404


def test_staging_rejects_an_unknown_table(client, workspace):
    dash.STAGING_DB.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(dash.STAGING_DB).executescript(
        "CREATE TABLE customers (_id INTEGER PRIMARY KEY, _key TEXT, name TEXT);"
        "INSERT INTO customers VALUES (1,'k','Dana');"
    )
    assert client.get("/staging?table=customers").status_code == 200
    assert "Dana" in client.get("/staging?table=customers").text
    assert client.get("/staging?table=sqlite_master").status_code == 404
    assert client.get("/staging?table=nope").status_code == 404


# --- token gate -------------------------------------------------------------


def test_token_gate_blocks_and_admits(workspace, monkeypatch):
    monkeypatch.setenv("EAGM_DASHBOARD_TOKEN", "s3cret")
    guarded = TestClient(dash.create_app())

    assert guarded.get("/").status_code == 401
    assert guarded.get("/api/status").status_code == 401
    assert guarded.post("/actions/discover", data={"side": "v2"}).status_code == 401

    assert guarded.get("/?token=s3cret").status_code == 200
    assert guarded.get("/api/status", headers={"x-eagm-token": "s3cret"}).status_code == 200
    assert guarded.get("/?token=wrong").status_code == 401


# --- jobs -------------------------------------------------------------------


def _wait(job, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while job.active and time.time() < deadline:
        time.sleep(0.02)


def test_jobs_run_one_at_a_time():
    """Two concurrent migrations would interleave writes and checkpoints."""
    manager = JobManager()
    gate = {"open": False}

    first = manager.start("slow", "first", lambda job: [
        time.sleep(0.01) for _ in range(200) if not gate["open"]
    ] and None)

    with pytest.raises(JobBusy, match="still running"):
        manager.start("other", "second", lambda job: None)

    gate["open"] = True
    _wait(first)
    manager.start("other", "second", lambda job: None)   # fine once the first ends


def test_job_failure_is_captured_not_crashed():
    manager = JobManager()

    def explode(job):
        raise RuntimeError("the database went away")

    job = manager.start("boom", "explodes", explode)
    _wait(job)

    assert job.status == "failed"
    assert "the database went away" in job.error
    assert any("FAILED" in line for line in job.log)


def test_stop_is_cooperative_and_reported():
    manager = JobManager()

    def loop(job):
        for _ in range(500):
            if job.should_stop():
                return {"stopped_early": True}
            time.sleep(0.005)
        return {"stopped_early": False}

    job = manager.start("loop", "long thing", loop)
    time.sleep(0.05)
    job.stop()
    _wait(job)

    assert job.status == "stopped"
    assert job.result == {"stopped_early": True}
    assert any("stop requested" in line for line in job.log)


def test_job_log_is_bounded():
    """A chatty job must not grow without limit."""
    manager = JobManager()
    job = manager.start("chatty", "lots of output",
                        lambda j: [j.say(f"line {i}") for i in range(3000)] and None)
    _wait(job, timeout=10)

    assert len(job.log) <= 2000
    assert "line 2999" in job.log[-2]          # the tail is what was kept


# --- the runner honours the stop flag ---------------------------------------


def test_runner_stops_between_batches_and_stays_resumable(tmp_path):
    """Stopping mid-run must leave a checkpoint, not a half-written mess."""
    from eag_migrator.adapters import SqlSink, SqlSource
    from eag_migrator.config import Settings
    from eag_migrator.db import build_engine
    from eag_migrator.mapping import Mapping
    from eag_migrator.runner import Runner
    from eag_migrator.state import RunState

    v2, v3 = tmp_path / "v2.sqlite", tmp_path / "v3.sqlite"
    src = sqlite3.connect(v2)
    src.executescript("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT);")
    src.executemany(
        "INSERT INTO people VALUES (?,?)", [(i, f"P{i}") for i in range(1, 21)]
    )
    src.commit()
    sqlite3.connect(v3).executescript(
        "CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " legacy_id INTEGER, name TEXT);"
    )

    mapping = Mapping.model_validate({
        "entities": [{
            "name": "people",
            "source": {"table": "people", "key": "id"},
            "target": {"table": "people", "key": "id"},
            "fields": [{"to": "legacy_id", "from": "id"}, {"to": "name", "from": "name"}],
        }]
    })

    # Trip the stop flag once, after the first batch — not on every batch,
    # or the resume would stop again immediately.
    stop = {"now": False, "armed": True}

    def trip(*_args):
        if stop["armed"]:
            stop["now"] = True
            stop["armed"] = False
    settings = Settings(v2_url=f"sqlite:///{v2}", v3_url=f"sqlite:///{v3}", batch_size=5)
    with RunState(tmp_path / "state.sqlite") as state:
        runner = Runner(
            settings, mapping,
            SqlSource(build_engine(f"sqlite:///{v2}")),
            SqlSink(build_engine(f"sqlite:///{v3}")),
            state,
            progress=trip,
            should_stop=lambda: stop["now"],
        )
        first = runner.apply()

        written = sqlite3.connect(v3).execute("SELECT COUNT(*) FROM people").fetchone()[0]
        assert 0 < written < 20
        assert any("stopped on request" in n for e in first.entities for n in e.notes)

        # Resuming finishes the job without duplicating what already landed.
        stop["now"] = False
        runner.apply(resume_run_id=first.run_id)

    rows = sqlite3.connect(v3).execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert rows == 20
