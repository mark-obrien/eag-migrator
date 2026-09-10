"""A rollback must never destroy its own evidence.

The journal and the id map are the only local record of what a run put in the
target. A rollback that undoes nothing — a dead session, a wrong endpoint, a
target with no DELETE — and then clears both anyway leaves the rows in
production with no way left to find them. That happened: 966 rows, 0 undone,
journal and id map both emptied.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from eag_migrator.config import Settings
from eag_migrator.mapping import Mapping
from eag_migrator.runner import Runner
from eag_migrator.state import RunState


class _Sink:
    """A sink whose undo reports back however many rows the test says."""

    def __init__(self, undone: int) -> None:
        self.undone = undone
        self.calls: list[list[dict]] = []

    def undo(self, entity_name, table, key_column, entries):
        self.calls.append(entries)
        return self.undone


def _mapping() -> Mapping:
    return Mapping.model_validate(
        {
            "entities": [
                {
                    "name": "customers",
                    "source": {"table": "customer_details", "key": "id"},
                    "target": {"table": "customers", "endpoint": "/api/V1/customers",
                               "key": "key"},
                    "fields": [{"to": "customerFullName", "from": "full_name"}],
                }
            ]
        }
    )


@pytest.fixture
def seeded(tmp_path: Path):
    """A finished apply run that journalled three inserts."""
    state = RunState(tmp_path / "migration.sqlite")
    mapping = _mapping()
    run_id = state.start_run("apply", mapping.fingerprint())
    state.record_ids("customers", run_id, [(1, "k1"), (2, "k2"), (3, "k3")])
    state.journal(run_id, "customers", "customers", "key",
                  [("k1", "inserted", None), ("k2", "inserted", None),
                   ("k3", "inserted", None)])
    state.finish_run(run_id, "completed")
    return state, mapping, run_id


def _runner(state, mapping, sink) -> Runner:
    return Runner(Settings(v2_url="", v3_url=""), mapping, source=None, sink=sink, state=state)


def test_a_rollback_that_undid_nothing_keeps_the_journal_and_id_map(seeded):
    state, mapping, run_id = seeded
    result = _runner(state, mapping, _Sink(undone=0)).rollback(run_id)

    assert result["rows_undone"] == 0
    assert result["rows_journalled"] == 3
    assert result["complete"] is False
    assert result["evidence_kept"] is True
    assert state.journal_count(run_id) == 3
    assert state.lookup_id("customers", 1) == "k1"
    assert state.get_run(run_id)["status"] == "rollback_incomplete"


def test_a_partial_rollback_also_keeps_the_evidence(seeded):
    state, mapping, run_id = seeded
    result = _runner(state, mapping, _Sink(undone=2)).rollback(run_id)

    assert result["complete"] is False
    assert state.journal_count(run_id) == 3
    assert state.lookup_id("customers", 3) == "k3"


def test_a_complete_rollback_clears_them(seeded):
    state, mapping, run_id = seeded
    result = _runner(state, mapping, _Sink(undone=3)).rollback(run_id)

    assert result["complete"] is True
    assert state.journal_count(run_id) == 0
    assert state.lookup_id("customers", 1) is None
    assert state.get_run(run_id)["status"] == "rolled_back"


def test_the_sink_is_told_the_entitys_real_endpoint(seeded):
    """Without it an API sink has to guess a path, and a guessed DELETE that
    the host's front end answers with 200 looks like a success."""
    state, mapping, run_id = seeded
    sink = _Sink(undone=3)
    _runner(state, mapping, sink).rollback(run_id)

    assert sink.calls, "undo was never called"
    assert all(e["endpoint"] == "/api/V1/customers" for e in sink.calls[0])
