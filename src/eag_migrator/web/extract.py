"""Pull fields out of a page or a JSON record.

Deliberately small: extraction gets the value out of the document, and the
existing `transform:` pipeline cleans it up. Same transforms as the database
path, so `phone`, `vin`, `nags`, `strip_html` and the rest all still apply.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..transforms import TransformError


class ExtractError(ValueError):
    """A selector or path did not resolve and the field was marked required."""


#: Selectors meaning "the row element itself" rather than something inside it.
SELF_SELECTORS = (".", ":scope", "&", "self")


def _resolve_path(record: Any, path: str) -> Any:
    """Dotted path with list indexing: 'title.rendered', 'images.0.src'."""
    if not path:
        return record
    current = record
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if not part.isdigit():
                # Map the rest of the path over the list.
                return [_resolve_path(item, part) for item in current]
            idx = int(part)
            current = current[idx] if 0 <= idx < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _node_value(node: Any, attr: str, base_url: str | None) -> Any:
    if attr == "text":
        return node.get_text(" ", strip=True)
    if attr == "html":
        return node.decode_contents()
    value = node.get(attr)
    if value is None:
        return None
    if isinstance(value, list):  # e.g. class="a b"
        value = " ".join(value)
    if base_url and attr in ("href", "src", "data-src", "srcset", "content"):
        return urljoin(base_url, value)
    return value


def extract_html(
    html: str,
    url: str,
    fields: list[Any],
    *,
    soup: BeautifulSoup | None = None,
    node: Any = None,
) -> dict[str, Any]:
    """Pull fields out of a page, or out of one row within it.

    `node` scopes the selectors — a list screen holds many records, and each
    row's cells have to be read relative to that row rather than to the page,
    or every record comes back identical to the first.
    """
    doc = soup or BeautifulSoup(html, "lxml")
    scope = node if node is not None else doc
    out: dict[str, Any] = {}

    for field in fields:
        if field.source == "url":
            out[field.to] = url
            continue
        if field.const is not None:
            out[field.to] = field.const
            continue
        if field.source == "jsonld":
            # JSON-LD belongs to the page, never to a row inside it.
            out[field.to] = _from_jsonld(doc, field.path or "")
            continue

        if not field.selector:
            out[field.to] = None
            continue

        if field.selector in SELF_SELECTORS and node is not None:
            # The row element itself. `<tr data-id="5001">` is how list screens
            # usually carry the record id, and `select()` only ever looks at
            # descendants — soupsieve does not honour `:scope` there.
            nodes = [node]
        else:
            try:
                nodes = scope.select(field.selector)
            except Exception as exc:  # noqa: BLE001 - bad CSS is a config error
                raise ExtractError(
                    f"{field.to}: invalid selector {field.selector!r}: {exc}"
                ) from exc

        if not nodes:
            if field.required:
                raise ExtractError(
                    f"{field.to}: selector {field.selector!r} matched nothing on {url}"
                )
            out[field.to] = [] if field.many else None
            continue

        if field.many:
            values = [_node_value(n, field.attr, url) for n in nodes]
            out[field.to] = [v for v in values if v not in (None, "")]
        else:
            out[field.to] = _node_value(nodes[0], field.attr, url)

    return out


def extract_json(record: Any, fields: list[Any], *, url: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in fields:
        if field.source == "url":
            out[field.to] = url
            continue
        if field.const is not None:
            out[field.to] = field.const
            continue
        value = _resolve_path(record, field.path or field.to)
        if value is None and field.required:
            raise ExtractError(f"{field.to}: path {field.path or field.to!r} resolved to null")
        out[field.to] = value
    return out


def _from_jsonld(doc: BeautifulSoup, path: str) -> Any:
    """Read a value out of the page's JSON-LD blocks.

    `path` may be prefixed with a @type to pick the right block, e.g.
    'LocalBusiness.telephone' or just 'telephone' to search every block.
    """
    blocks: list[Any] = []
    for tag in doc.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            continue

    wanted_type: str | None = None
    if "." in path:
        head, rest = path.split(".", 1)
        if head and head[0].isupper():
            wanted_type, path = head, rest

    def search(node: Any) -> Any:
        if isinstance(node, dict):
            types = node.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if wanted_type is None or wanted_type in types:
                found = _resolve_path(node, path)
                if found is not None:
                    return found
            if "@graph" in node:
                got = search(node["@graph"])
                if got is not None:
                    return got
            for value in node.values():
                if isinstance(value, (dict, list)):
                    got = search(value)
                    if got is not None:
                        return got
        elif isinstance(node, list):
            for item in node:
                got = search(item)
                if got is not None:
                    return got
        return None

    for block in blocks:
        got = search(block)
        if got is not None:
            return got
    return None


def select_rows(html: str, selector: str, soup: BeautifulSoup | None = None) -> list[Any]:
    """The elements a list screen repeats — table rows, cards, list items."""
    doc = soup or BeautifulSoup(html, "lxml")
    try:
        return list(doc.select(selector))
    except Exception as exc:  # noqa: BLE001 - bad CSS is a config error
        raise ExtractError(f"invalid rows selector {selector!r}: {exc}") from exc


def links_from(
    html: str, url: str, pattern: str | None = None
) -> tuple[list[str], list[tuple[str, str]]]:
    """Same-page links, split into safe-to-follow and must-not-touch.

    Returns (safe, [(url, reason), ...]). A crawl runs signed in to a live
    system, so following a delete or logout link is not a crawl, it is an
    incident.
    """
    from .capture import UNSAFE_LINK

    doc = BeautifulSoup(html, "lxml")
    regex = re.compile(pattern) if pattern else None
    safe: list[str] = []
    skipped: list[tuple[str, str]] = []

    for tag in doc.find_all("a", href=True):
        raw = tag["href"]
        if raw.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        href = urljoin(url, raw).split("#")[0]

        method = (tag.get("data-method") or "").lower()
        if method and method != "get":
            skipped.append((href, f"data-method={method}"))
            continue
        if tag.get("data-confirm") or tag.get("data-turbo-confirm"):
            skipped.append((href, "has a confirmation prompt"))
            continue

        parsed = urlparse(href)
        if UNSAFE_LINK.search(parsed.path + "?" + (parsed.query or "")):
            skipped.append((href, "looks like a state change"))
            continue
        if regex and not regex.search(href):
            continue
        safe.append(href)

    return list(dict.fromkeys(safe)), skipped


def flatten(value: Any) -> Any:
    """Staging columns are scalars; structures go in as JSON text."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        raise TransformError(f"could not serialise value: {exc}") from exc
