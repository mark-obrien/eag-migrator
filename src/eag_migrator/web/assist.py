"""Ask a model to read a list screen's *structure* and name the selectors.

`listing.py` guesses from heuristics, which is fine for a table with a header
row and poor at nested markup with no classes. A model reads that better. It
is used for one call per screen — never per record: bulk extraction has to be
deterministic, because resume, verify and rollback all assume the same page
gives the same rows every run.

**No page content leaves this machine.** Every text node is replaced with a
type placeholder and every attribute value that is not structural is masked
*before* the request is built, so the model sees

    <td class="email">EMAIL</td>

and never an address, a name, a VIN or a card number. That is also better
input: shape is the entire question here, and the values are noise.

Off unless asked for. Set ANTHROPIC_API_KEY and pass --assist.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from bs4 import BeautifulSoup, Comment, Doctype

DEFAULT_MODEL = "claude-sonnet-5"
API_VERSION = "2023-06-01"

# Dropped whole: no structure worth reading, and the biggest source of bulk.
DROP_TAGS = ("script", "style", "noscript", "svg", "canvas", "template", "head", "iframe")

# Attributes kept verbatim — these *are* the structure.
KEEP_ATTRS = ("class", "rel", "role", "type", "scope", "colspan", "rowspan", "headers")

# Text kept verbatim. A column heading is a schema, not a record: "Email" is
# the single most useful thing on the page for naming a field, and it is not
# anybody's data.
LABEL_TAGS = ("th", "label", "caption", "legend")

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE = re.compile(r"^[\d\s().+-]{7,20}$")
MONEY = re.compile(r"^[$£€]?\s?[\d,]+\.\d{2}$|^[$£€]\s?[\d,]+$")
DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")
DIGITS = re.compile(r"\d")


class AssistUnavailable(RuntimeError):
    """No API key, or the key was rejected. The caller falls back to heuristics."""


class AssistFailed(RuntimeError):
    """The model was reached but did not return a usable proposal."""


def _placeholder(text: str) -> str:
    """A type, never a value."""
    value = text.strip()
    if not value:
        return ""
    if EMAIL.match(value):
        return "EMAIL"
    if MONEY.match(value):
        return "MONEY"
    if DATEISH.match(value):
        return "DATE"
    if value.isdigit():
        return f"NUM({len(value)})"
    # Ten digits, so a postal code plus four does not read as a phone number.
    if PHONE.match(value) and sum(c.isdigit() for c in value) >= 10:
        return "PHONE"
    return f"TEXT({len(value)})"


def _label(text: str) -> str:
    """A heading, kept as written — unless it turns out to hold a value."""
    value = " ".join(text.split())
    if not value:
        return ""
    typed = _placeholder(value)
    if typed in ("EMAIL", "PHONE", "MONEY", "DATE") or typed.startswith("NUM("):
        return typed
    return value[:60]


def _mask_attr(name: str, value: Any) -> str | None:
    """Keep what tells you the shape; mask what carries a record's data."""
    if isinstance(value, list):
        value = " ".join(value)
    value = str(value)

    if name in KEEP_ATTRS:
        return value
    if name == "id":
        # <tr id="customer_5001"> — the pattern matters, the number does not.
        return DIGITS.sub("0", value)
    if name in ("href", "src", "action", "data-url"):
        base, _, query = value.partition("?")
        path = DIGITS.sub("0", base.split("#")[0])
        # Query *keys* are structure — ?page= is how the list paginates. The
        # values are not.
        keys = [p.partition("=")[0] for p in query.split("&") if p.partition("=")[0]]
        if keys:
            path += "?" + "&".join(f"{k}=V" for k in keys[:6])
        return path
    if name.startswith("data-") or name == "name":
        return _placeholder(value) or "EMPTY"
    # aria-label and title routinely read "Delete quote for Dana Reyes".
    return None


def skeleton(html: str, *, max_repeats: int = 3, max_chars: int = 60_000) -> str:
    """The page with every value removed and long repeats collapsed."""
    doc = BeautifulSoup(html, "lxml")

    for tag in doc.find_all(DROP_TAGS):
        tag.decompose()
    for node in list(doc.find_all(string=lambda s: isinstance(s, (Comment, Doctype)))):
        node.extract()

    for node in list(doc.find_all(string=True)):
        parent = node.parent.name if node.parent else ""
        raw = str(node)
        replacement = _label(raw) if parent in LABEL_TAGS else _placeholder(raw)
        if replacement:
            node.replace_with(replacement)
        else:
            node.extract()

    for tag in doc.find_all(True):
        masked: dict[str, Any] = {}
        for name, value in tag.attrs.items():
            got = _mask_attr(name, value)
            if got is not None:
                masked[name] = got
        tag.attrs = masked

    # A list of 200 rows says nothing that 3 rows do not, and costs 200x.
    for parent in doc.find_all(True):
        counts: dict[tuple, int] = {}
        surplus: dict[tuple, list] = {}
        for child in parent.find_all(recursive=False):
            if child.name in LABEL_TAGS:
                continue    # collapsing a header row throws away column names
            signature = (child.name, tuple(child.get("class") or []))
            counts[signature] = counts.get(signature, 0) + 1
            if counts[signature] > max_repeats:
                surplus.setdefault(signature, []).append(child)
        for signature, extra in surplus.items():
            anchor = extra[0]
            anchor.insert_before(Comment(f" {len(extra)} more like the above "))
            for child in extra:
                child.decompose()

    out = doc.decode()
    out = re.sub(r"\n\s*\n+", "\n", out)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n<!-- truncated -->"
    return out


TOOL = {
    "name": "propose_extraction",
    "description": "Report how to extract the records repeated on this screen.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_list": {
                "type": "boolean",
                "description": "True if the page repeats a record. False for a "
                               "detail page, a form, a login screen or a dashboard.",
            },
            "rows": {
                "type": "string",
                "description": "CSS selector matching exactly one element per record. "
                               "Empty when is_list is false.",
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string",
                               "description": "snake_case column name for the value"},
                        "selector": {"type": "string",
                                     "description": "CSS selector relative to the row. "
                                                    "Use \".\" for the row element itself."},
                        "attr": {"type": "string",
                                 "description": "text, html, or an attribute name "
                                                "such as href or data-id"},
                        "note": {"type": "string",
                                 "description": "Only when something needs checking"},
                    },
                    "required": ["to", "selector", "attr"],
                },
            },
            "key": {
                "type": "string",
                "description": "The field name that uniquely identifies a record, "
                               "or an empty string if none does.",
            },
            "reasoning": {"type": "string", "description": "One or two sentences."},
        },
        "required": ["is_list", "rows", "fields", "key", "reasoning"],
    },
}

SYSTEM = """You read the HTML skeleton of a screen from a business application \
and report how to extract the records it lists.

The markup has been stripped of all content on purpose: text nodes are replaced \
with type placeholders (TEXT(n), NUM(n), EMAIL, PHONE, MONEY, DATE) and \
attribute values are masked. Infer from structure, class names and table \
headers. Do not ask for the real values; you will not be given them.

Rules:
- `rows` must match one element per record and nothing else. Scope it so a \
second table or list on the page is not caught by it.
- Field selectors are evaluated relative to a row. Use "." to read an \
attribute off the row element itself, which is where a record id usually is.
- Name fields from the table headers when there are any, otherwise from the \
placeholder types and class names.
- Do not propose fields for action columns (Delete, Edit, Refund buttons).
- Prefer a column's link (attr "href") as an extra field when it points at the \
record, named <column>_url.
- If the page does not repeat a record, set is_list false and stop."""


def _request(payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    import httpx

    base = (os.getenv("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    response = httpx.post(
        f"{base}/v1/messages",
        json=payload,
        timeout=timeout,
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
    )
    if response.status_code in (401, 403):
        raise AssistUnavailable(
            f"the API rejected the key (HTTP {response.status_code}) — "
            f"check ANTHROPIC_API_KEY"
        )
    if response.status_code >= 400:
        raise AssistFailed(f"HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def ask(html: str, url: str, *, model: str | None = None, timeout: float = 60.0) -> dict[str, Any]:
    """Returns the model's proposal as a plain dict, plus what was sent.

    Raises AssistUnavailable when it cannot run at all, AssistFailed when it
    ran and the answer was unusable. Both are recoverable — the caller falls
    back to the heuristics.
    """
    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise AssistUnavailable(
            "ANTHROPIC_API_KEY is not set. Add it to .env, or drop --assist to "
            "use the built-in heuristics."
        )

    shape = skeleton(html)
    payload = {
        "model": model or os.getenv("EAGM_ASSIST_MODEL") or DEFAULT_MODEL,
        "max_tokens": 2000,
        "system": SYSTEM,
        "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": "propose_extraction"},
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Screen: {url}\n\nStructure:\n\n{shape}"
                ),
            }
        ],
    }

    body = _request(payload, api_key, timeout)
    for block in body.get("content") or []:
        if block.get("type") == "tool_use":
            proposal = block.get("input") or {}
            proposal["_skeleton_chars"] = len(shape)
            return proposal

    raise AssistFailed(
        "the model answered without using the tool: "
        + json.dumps(body.get("content"))[:200]
    )
