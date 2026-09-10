"""Building a nested JSON body from a flat source row.

v3 does not take a flat row: a job wants `vehicle` and `billingAddress` as
objects, a customer wants `addressList` and `phoneList` as arrays of objects.
`to:` therefore accepts a path. A `to:` without a dot or bracket has to keep
behaving exactly as it did, because that is what a database target needs.
"""
from __future__ import annotations

import pytest

from eag_migrator.mapping import EntityMap
from eag_migrator.runner import Ctx, Runner


def _entity(fields: list[dict]) -> EntityMap:
    return EntityMap.model_validate(
        {
            "name": "jobs",
            "source": {"table": "jobs", "key": "id"},
            "target": {"table": "jobs", "endpoint": "/api/V1/jobs"},
            "fields": fields,
        }
    )


def _build(fields: list[dict], src: dict) -> dict:
    runner = Runner.__new__(Runner)  # build_row touches no collaborators
    ctx = Ctx(state=None, run_id="r1", entity_name="jobs", plan_mode=True)
    return runner.build_row(_entity(fields), src, ctx)


def test_a_plain_target_name_is_still_a_flat_key():
    out = _build([{"to": "customerFirstName", "from": "fname"}], {"fname": "Dana"})
    assert out == {"customerFirstName": "Dana"}


def test_a_dotted_target_builds_an_object():
    out = _build(
        [{"to": "vehicle.vin", "from": "vin"}, {"to": "vehicle.year", "from": "yr"}],
        {"vin": "1HGCM", "yr": "2015"},
    )
    assert out == {"vehicle": {"vin": "1HGCM", "year": "2015"}}


def test_an_indexed_target_builds_an_array_of_objects():
    out = _build(
        [
            {"to": "phoneList[0].phoneNumber", "from": "phone"},
            {"to": "phoneList[0].phoneType", "const": 1},
        ],
        {"phone": "5551234"},
    )
    assert out == {"phoneList": [{"phoneNumber": "5551234", "phoneType": 1}]}


def test_an_array_entry_with_nothing_in_it_is_dropped():
    """Most customers have one address, but the mapping still names a second.

    Without pruning every one of them would be sent an address object of nulls.
    """
    out = _build(
        [
            {"to": "addressList[0].address1", "from": "a1"},
            {"to": "addressList[0].city", "from": "c1"},
            {"to": "addressList[1].address1", "from": "a2"},
            {"to": "addressList[1].city", "from": "c2"},
        ],
        {"a1": "12 Elm", "c1": "Springfield", "a2": None, "c2": ""},
    )
    assert out == {"addressList": [{"address1": "12 Elm", "city": "Springfield"}]}


def test_a_const_type_code_does_not_keep_an_otherwise_empty_entry_alive():
    """An address entry is `addressType` plus a const country whether or not
    the customer has a second address. Counting those as data means nothing is
    ever pruned and every customer is sent an empty address labelled type 2."""
    out = _build(
        [
            {"to": "addressList[0].city", "from": "c1"},
            {"to": "addressList[0].addressType", "const": 1},
            {"to": "addressList[0].country", "const": "US"},
            {"to": "addressList[1].city", "from": "c2"},
            {"to": "addressList[1].addressType", "const": 2},
            {"to": "addressList[1].country", "const": "US"},
        ],
        {"c1": "Springfield", "c2": None},
    )
    assert out == {
        "addressList": [{"city": "Springfield", "addressType": 1, "country": "US"}]
    }


def test_a_null_inside_a_kept_object_is_preserved():
    """Pruning drops empty array entries, not fields. A mapped null is a null."""
    out = _build(
        [
            {"to": "addressList[0].address1", "from": "a1"},
            {"to": "addressList[0].address2", "from": "a2"},
        ],
        {"a1": "12 Elm", "a2": None},
    )
    assert out == {"addressList": [{"address1": "12 Elm", "address2": None}]}


def test_a_flat_field_holding_a_list_is_left_alone():
    """`from: [a, b]` yields a list. That is a value, not a built array —
    pruning it would quietly drop the empty half of a name."""
    out = _build([{"to": "parts", "from": ["a", "b"]}], {"a": "x", "b": ""})
    assert out == {"parts": ["x", ""]}


def test_objects_and_arrays_can_be_mixed_in_one_row():
    out = _build(
        [
            {"to": "customerLastName", "from": "ln"},
            {"to": "vehicle.vin", "from": "vin"},
            {"to": "billingAddress.city", "from": "city"},
            {"to": "notes[0].content", "from": "note"},
        ],
        {"ln": "Reyes", "vin": "1HGCM", "city": "Springfield", "note": "chip repair"},
    )
    assert out == {
        "customerLastName": "Reyes",
        "vehicle": {"vin": "1HGCM"},
        "billingAddress": {"city": "Springfield"},
        "notes": [{"content": "chip repair"}],
    }


def test_a_malformed_path_is_rejected_rather_than_guessed_at():
    with pytest.raises(Exception):
        _build([{"to": "vehicle..vin", "from": "vin"}], {"vin": "1HGCM"})
