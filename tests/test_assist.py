"""The optional model-assisted drafting step.

Two things matter here and nothing else does. First, that no page content can
reach the API — this runs against a system holding customer records and card
data, and the request is built from a skeleton for exactly that reason.
Second, that a proposal is believed only if it works: the model's selectors
are run against the page and dropped if they extract less than the heuristics.

No network: the call is stubbed. What is under test is what we send and what
we do with what comes back.
"""

from __future__ import annotations

import pytest

from eag_migrator.web import assist
from eag_migrator.web.listing import measure, propose

# A customers list with everything you would not want to send anywhere.
PAGE = """<!DOCTYPE html>
<html><head><title>Customers</title>
<script>var user = {name: "Dana Reyes", token: "sk-live-abc123"};</script></head>
<body>
<nav><a href="/logout" title="Sign out Dana Reyes">Log out</a></nav>
<table class="listing">
  <thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Total</th></tr></thead>
  <tbody>
    <tr class="customer" data-id="5001" data-vin="1HGCM82633A004352">
      <td class="name"><a href="/customers/5001">Dana Reyes</a></td>
      <td class="email">dana@example.com</td>
      <td class="phone">(555) 123-4567</td>
      <td class="total">$449.99</td>
    </tr>
    <tr class="customer" data-id="5002" data-vin="5NPE24AF1FH012345">
      <td class="name"><a href="/customers/5002">Sam Oyelaran</a></td>
      <td class="email">sam.oyelaran@example.com</td>
      <td class="phone">555-987-6543</td>
      <td class="total">$1,250.00</td>
    </tr>
  </tbody>
</table>
<p>Card on file: 4111 1111 1111 1111</p>
</body></html>"""

URL = "https://app.example.test/customers"


# --- nothing from the page leaves ------------------------------------------


def test_the_skeleton_carries_no_page_content():
    shape = assist.skeleton(PAGE)

    for secret in (
        "Dana Reyes", "Sam Oyelaran",
        "dana@example.com", "sam.oyelaran@example.com",
        "555", "123-4567", "449.99", "1,250.00",
        "1HGCM82633A004352", "5NPE24AF1FH012345",
        "4111", "sk-live-abc123",
    ):
        assert secret not in shape, f"{secret!r} reached the request"


def test_the_skeleton_keeps_the_structure_that_matters():
    shape = assist.skeleton(PAGE)

    assert 'class="listing"' in shape
    assert 'class="customer"' in shape
    assert "data-id" in shape          # the attribute is the point...
    assert "5001" not in shape         # ...its value is not
    assert "<th>" in shape and "<td" in shape
    # Type placeholders stand in for values, so columns are still tellable apart.
    assert "EMAIL" in shape
    assert "MONEY" in shape
    assert "PHONE" in shape
    assert "TEXT(" in shape


def test_scripts_and_hover_text_are_dropped_whole():
    shape = assist.skeleton(PAGE)
    assert "<script" not in shape
    assert "token" not in shape
    # title="Sign out Dana Reyes" — an attribute that carries prose.
    assert "title=" not in shape


def test_link_targets_keep_their_shape_but_not_their_ids():
    shape = assist.skeleton(PAGE)
    assert "/customers/0000" in shape
    assert "/customers/5001" not in shape


def test_column_headings_are_kept_because_they_are_schema_not_data():
    shape = assist.skeleton(PAGE)
    for heading in ("Name", "Email", "Phone", "Total"):
        assert f"<th>{heading}</th>" in shape


def test_a_heading_that_is_really_a_value_is_still_masked():
    shape = assist.skeleton(
        '<table><tr><th>dana@example.com</th><th>Status</th></tr>'
        '<tr><td>x</td><td>y</td></tr></table>'
    )
    assert "dana@example.com" not in shape
    assert "<th>Status</th>" in shape


def test_query_keys_survive_but_their_values_do_not():
    shape = assist.skeleton('<a href="/customers?page=2&q=Dana+Reyes">Next</a>')
    assert "page=V" in shape
    assert "Dana" not in shape
    assert "2" not in shape


def test_long_lists_are_collapsed_before_they_are_paid_for():
    rows = "\n".join(
        f'<tr class="r"><td>Customer {i}</td></tr>' for i in range(200)
    )
    shape = assist.skeleton(f"<table><tbody>{rows}</tbody></table>", max_repeats=3)

    assert shape.count("<tr") == 3
    assert "more like the above" in shape


def test_the_header_row_is_never_collapsed_away():
    """Collapsing repeats must not eat the column names."""
    heads = "".join(f"<th>Col{i}</th>" for i in range(8))
    shape = assist.skeleton(
        f"<table><thead><tr>{heads}</tr></thead>"
        f"<tbody><tr><td>x</td></tr></tbody></table>",
        max_repeats=3,
    )
    for i in range(8):
        assert f"<th>Col{i}</th>" in shape


def test_it_refuses_to_run_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(assist.AssistUnavailable, match="ANTHROPIC_API_KEY"):
        assist.ask(PAGE, URL)


# --- what comes back is checked, not trusted --------------------------------


def _answer(**over):
    base = {
        "is_list": True,
        "rows": "table.listing tbody tr.customer",
        "fields": [
            {"to": "id", "selector": ".", "attr": "data-id"},
            {"to": "name", "selector": "td.name", "attr": "text"},
            {"to": "email", "selector": "td.email", "attr": "text"},
            {"to": "phone", "selector": "td.phone", "attr": "text"},
            {"to": "total", "selector": "td.total", "attr": "text"},
            {"to": "name_url", "selector": "td.name a", "attr": "href"},
        ],
        "key": "id",
        "reasoning": "A table of customers, one row each.",
    }
    base.update(over)
    return base


@pytest.fixture
def stub(monkeypatch):
    """Answer the model call with whatever the test wants, and record the request."""
    sent: dict = {}

    def install(answer):
        def fake(payload, api_key, timeout):
            sent["payload"] = payload
            sent["key"] = api_key
            return {"content": [{"type": "tool_use", "input": answer}]}

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(assist, "_request", fake)
        return sent

    return install


# Markup the heuristics genuinely cannot read: no table, no classes worth
# anything, nothing to name a column after. This is what --assist is for.
DIV_SOUP = """<html><body><div id="app"><div class="w">
  <div class="x"><div>Dana Reyes</div><div>dana@example.com</div><div>$449.99</div></div>
  <div class="x"><div>Sam Oyelaran</div><div>sam@example.com</div><div>$1,250.00</div></div>
  <div class="x"><div>Kit Nakamura</div><div>kit@example.org</div><div>$310.50</div></div>
</div></div></body></html>"""


def test_the_model_wins_where_the_heuristics_cannot_read_the_markup(stub):
    from eag_migrator.web.listing import _propose_heuristic

    # Establish the premise rather than assuming it.
    assert _propose_heuristic(DIV_SOUP, URL) is None

    stub({
        "is_list": True,
        "rows": "div.w > div.x",
        "fields": [
            {"to": "name", "selector": "div:nth-of-type(1)", "attr": "text"},
            {"to": "email", "selector": "div:nth-of-type(2)", "attr": "text"},
            {"to": "total", "selector": "div:nth-of-type(3)", "attr": "text"},
        ],
        "key": "",
        "reasoning": "Three repeated blocks of name, email and an amount.",
    })
    found = propose(DIV_SOUP, URL, assist=True)

    assert found is not None
    assert found.kind == "model"
    assert found.row_count == 3
    assert found.filled == 3
    assert found.key is None
    # Pagination is still worked out locally — the model is not asked for it.
    assert found.follow == r"/customers/?($|\?)"


def test_an_equivalent_proposal_does_not_displace_the_heuristics(stub):
    """A draw goes to the answer that is free and identical every run."""
    stub(_answer())
    found = propose(PAGE, URL, assist=True)

    assert found.kind == "table"
    assert any("were kept" in n for n in found.notes)


def test_the_request_is_built_from_the_skeleton_not_the_page(stub):
    sent = stub(_answer())
    propose(PAGE, URL, assist=True)

    body = str(sent["payload"]["messages"])
    assert "dana@example.com" not in body
    assert "Dana Reyes" not in body
    assert "EMAIL" in body
    assert sent["payload"]["tool_choice"]["name"] == "propose_extraction"


def test_selectors_that_match_nothing_are_discarded(stub):
    stub(_answer(rows="div.does-not-exist"))
    found = propose(PAGE, URL, assist=True)

    assert found.kind == "table"           # fell back to the heuristics
    assert any("did not hold up" in n for n in found.notes)


def test_invalid_css_is_a_fallback_not_a_crash(stub):
    stub(_answer(rows="tr[unclosed"))
    found = propose(PAGE, URL, assist=True)
    assert found.kind == "table"


def test_a_thinner_proposal_loses_to_the_heuristics(stub):
    stub(_answer(fields=[
        {"to": "name", "selector": "td.name", "attr": "text"},
        {"to": "email", "selector": "td.email", "attr": "text"},
    ]))
    found = propose(PAGE, URL, assist=True)

    assert found.kind == "table"
    assert any("were kept" in n for n in found.notes)


def test_a_detail_page_is_reported_as_not_a_list(stub):
    stub(_answer(is_list=False, rows="", fields=[]))
    found = propose(PAGE, URL, assist=True)
    assert found.kind == "table"           # the heuristics still had a table


def test_an_api_failure_falls_back_rather_than_stopping(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def boom(payload, api_key, timeout):
        raise assist.AssistFailed("HTTP 529: overloaded")

    monkeypatch.setattr(assist, "_request", boom)
    found = propose(PAGE, URL, assist=True)

    assert found.kind == "table"
    assert any("overloaded" in n for n in found.notes)


def test_without_assist_no_key_is_read_and_no_call_is_made(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("the model must not be called without --assist")

    monkeypatch.setattr(assist, "ask", boom)
    assert propose(PAGE, URL).kind == "table"


# --- the check that makes the rest safe -------------------------------------


def test_measure_counts_only_fields_that_fill_most_rows():
    from eag_migrator.web.harvest import FieldSpec

    fields = [
        FieldSpec(to="name", selector="td.name"),
        FieldSpec(to="nothing", selector="td.nope"),
    ]
    rows, filled = measure("table.listing tbody tr.customer", fields, PAGE, URL)

    assert rows == 2
    assert filled == 1
