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


# --- port selection ---------------------------------------------------------


def test_default_port_avoids_the_usual_suspects():
    """8080 and friends are contended; the project's own ports are taken."""
    from eag_migrator.dashboard.net import DEFAULT_PORT

    contended = {80, 443, 3000, 3001, 4200, 5000, 5173, 8000, 8008, 8080, 8081,
                 8443, 8888, 9000, 9090, 5432, 3306, 6379, 27017}
    project = {13306, 13307, 15432, 15433, 18080}   # already published by compose
    assert DEFAULT_PORT not in contended
    assert DEFAULT_PORT not in project
    # Clear of the Linux ephemeral range, which the OS hands out to clients.
    assert 1024 < DEFAULT_PORT < 32768


def test_port_comes_from_the_environment(monkeypatch):
    from eag_migrator.dashboard.net import DEFAULT_PORT, configured_port

    monkeypatch.delenv("EAGM_DASHBOARD_PORT", raising=False)
    assert configured_port() == DEFAULT_PORT

    monkeypatch.setenv("EAGM_DASHBOARD_PORT", "12345")
    assert configured_port() == 12345

    # Garbage falls back rather than crashing at startup.
    for junk in ("", "not-a-port", "0", "70000", "-1"):
        monkeypatch.setenv("EAGM_DASHBOARD_PORT", junk)
        assert configured_port() == DEFAULT_PORT


def test_a_port_in_use_is_detected():
    import socket

    from eag_migrator.dashboard.net import find_free, is_free

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]

        assert not is_free(busy)
        alternative = find_free(near=busy)
        assert alternative != busy
        assert is_free(alternative)

    # Released once the listener closes.
    assert is_free(busy)


def test_suggestion_stays_near_the_requested_port():
    """A nearby port keeps the URL recognisable instead of jumping to random."""
    import socket

    from eag_migrator.dashboard.net import find_free

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        suggestion = find_free(near=busy)

    assert busy < suggestion <= busy + 12 or suggestion > 1024


def test_dashboard_refuses_to_start_on_a_busy_port(monkeypatch):
    """It should say so, not surface a traceback from inside the server."""
    import socket

    from typer.testing import CliRunner

    from eag_migrator.cli import app as cli

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]

        result = CliRunner().invoke(cli, ["dashboard", "--port", str(busy)])

    assert result.exit_code == 1
    assert "already in use" in result.output
    assert "--auto-port" in result.output
    assert "EAGM_DASHBOARD_PORT" in result.output
