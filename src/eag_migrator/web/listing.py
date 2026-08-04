"""Read a server-rendered list screen and propose how to extract it.

When v2 has no JSON API, every record has to come out of markup — and the
tedious part of that is writing a CSS selector per column per screen. This
looks at a real page and proposes them: the repeated element, one field per
column, the id the rows carry, and the link to the next page.

Everything it returns is a starting point. The caller writes it into the
harvest config with TODO notes so it gets checked before a full run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .harvest import FieldSpec

# Attributes a row uses to carry its record id, in preference order.
ID_ATTRS = ("data-id", "data-record-id", "data-row-id", "data-pk", "data-key", "id")

# Link text that means "the next page of the same list".
NEXT_TEXT = re.compile(r"^\s*(next|older|more|›|»|→|>>?)\s*$", re.IGNORECASE)

PAGE_PARAM = re.compile(r"[?&](page|p|pg|offset|start)=\d+", re.IGNORECASE)
PAGE_PATH = re.compile(r"/page/\d+/?$", re.IGNORECASE)

# Wrappers that repeat for layout reasons and hold nothing worth harvesting.
CHROME_CLASS = re.compile(
    r"nav|menu|breadcrumb|pagination|pager|tab|toolbar|footer|header|sidebar|"
    r"dropdown|modal|toast|alert|badge|icon|spinner",
    re.IGNORECASE,
)

MIN_ROWS = 3


def _ident(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_").lower()
    if not slug:
        return ""
    if slug[0].isdigit():
        slug = f"c_{slug}"
    return slug[:40]


@dataclass
class Proposal:
    """One repeated structure on a page, and how to read it."""

    kind: str                    # "table" or "cards"
    rows: str                    # CSS selector for the repeated element
    fields: list[FieldSpec]
    row_count: int
    key: str | None = None
    next_url: str | None = None
    follow: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        """Bigger is a better candidate: more rows, more columns."""
        return self.row_count * 10 + len(self.fields)


def _text(node) -> str:
    return node.get_text(" ", strip=True)


def _usable_classes(node) -> list[str]:
    return [c for c in (node.get("class") or []) if not CHROME_CLASS.search(c)]


def _row_selector(rows_parent, sample) -> str:
    """A selector for the repeated element, scoped enough to be unambiguous."""
    classes = _usable_classes(sample)
    own = sample.name + ("." + classes[0] if classes else "")

    # Scope to the nearest ancestor carrying a class of its own. A screen with
    # two tables on it would otherwise read both into the same collection.
    node = rows_parent
    while getattr(node, "name", None) not in (None, "[document]", "body", "html"):
        found = _usable_classes(node)
        if found:
            return f"{node.name}.{found[0]} {own}"
        node = node.parent

    if rows_parent.name in ("tbody", "table", "ul", "ol"):
        return f"{rows_parent.name} > {own}"
    return own


def _id_field(rows: list) -> tuple[FieldSpec | None, str | None]:
    for attr in ID_ATTRS:
        values = [r.get(attr) for r in rows]
        if not all(values) or len(set(values)) != len(values):
            continue
        note = None
        if attr == "id":
            note = "TODO: this is the DOM id (e.g. 'customer_5001'), not a bare record id"
        return FieldSpec(to="id", selector=".", attr=attr, note=note), "id"
    return None, None


def _is_safe_link(href: str) -> bool:
    from .capture import UNSAFE_LINK

    if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
        return False
    parsed = urlparse(href)
    return not UNSAFE_LINK.search(parsed.path + "?" + (parsed.query or ""))


def _table_proposal(table, url: str) -> Proposal | None:
    body = table.find("tbody") or table
    rows = [tr for tr in body.find_all("tr", recursive=False) if tr.find("td")]
    if len(rows) < 2:
        return None

    heads = [_text(th) for th in table.select("thead th")]
    if not heads:
        first = table.find("tr")
        heads = [_text(th) for th in first.find_all("th")] if first else []

    cells = [r.find_all("td", recursive=False) for r in rows]
    width = max(len(c) for c in cells)
    fields: list[FieldSpec] = []
    notes: list[str] = []

    id_field, key = _id_field(rows)
    if id_field:
        fields.append(id_field)

    used: set[str] = {"id"} if id_field else set()
    for i in range(width):
        column = [c[i] for c in cells if len(c) > i]
        if not column:
            continue

        per_cell = [cell.find_all("a", href=True) for cell in column]
        anchors = [a for links in per_cell for a in links]
        texts = [_text(cell) for cell in column]

        # The actions column: every cell is buttons, and none of them go
        # anywhere safe. "Delete" and "Refund" are not fields.
        only_links = all(
            links and _text(cell) == " ".join(_text(a) for a in links)
            for cell, links in zip(column, per_cell)
        )
        if only_links and not any(_is_safe_link(a["href"]) for a in anchors):
            continue

        name = _ident(heads[i]) if i < len(heads) else ""
        if not name:
            if anchors and not any(texts):
                continue
            name = f"col_{i + 1}"
        base, n = name, 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)

        if not any(texts) and not anchors:
            continue

        fields.append(FieldSpec(to=name, selector=f"td:nth-of-type({i + 1})"))

        # A cell whose only content is one link usually points at the record.
        safe = [a for a in anchors if _is_safe_link(a["href"])]
        if safe and len(safe) >= len(column) - 1 and f"{name}_url" not in used:
            fields.append(
                FieldSpec(to=f"{name}_url", selector=f"td:nth-of-type({i + 1}) a", attr="href")
            )
            used.add(f"{name}_url")
            if not key:
                notes.append(
                    f"TODO: no id attribute on the rows — the record id is probably in "
                    f"{name}_url ({safe[0]['href']}). Set `key:` once you have it as a field."
                )

    # Two columns is enough to be a record. One is only convincing when the
    # table labels it — otherwise it is as likely to be a layout table.
    if not fields or (len(fields) < 2 and not heads):
        return None

    selector = _row_selector(body, rows[0])
    if selector.endswith("tr"):
        # In a table with no <thead>, the header row is a plain sibling — and a
        # selector that matches it produces one record of empty columns.
        selector += ":has(td)"

    return Proposal(
        kind="table",
        rows=selector,
        fields=fields,
        row_count=len(rows),
        key=key,
        notes=notes,
    )


def _card_proposal(parent, siblings: list, url: str) -> Proposal | None:
    """A list of repeated blocks — cards, list items, panels."""
    fields: list[FieldSpec] = []
    used: set[str] = set()

    id_field, key = _id_field(siblings)
    if id_field:
        fields.append(id_field)
        used.add("id")

    def add(to: str, selector: str, attr: str = "text") -> None:
        if to in used:
            return
        hits = sum(1 for s in siblings if s.select(selector))
        if hits < max(2, len(siblings) // 2):
            return
        fields.append(FieldSpec(to=to, selector=selector, attr=attr))
        used.add(to)

    for tag in ("h1", "h2", "h3", "h4", ".title", ".name"):
        add("title", tag)
        if "title" in used:
            break
    add("link", "a[href]", "href")
    add("date", "time", "datetime")
    add("summary", "p")

    if len(fields) < 2:
        return None

    return Proposal(
        kind="cards",
        rows=_row_selector(parent, siblings[0]),
        fields=fields,
        row_count=len(siblings),
        key=key,
        notes=[
            "TODO: these are generic selectors for a repeated block — open the page "
            "and name the real fields."
        ],
    )


def _repeated_blocks(doc) -> list[tuple[object, list]]:
    """Sibling groups that look like a list of records."""
    groups: list[tuple[object, list]] = []
    for parent in doc.find_all(["ul", "ol", "div", "section", "main", "tbody"]):
        buckets: dict[tuple, list] = {}
        for child in parent.find_all(recursive=False):
            classes = tuple(sorted(c for c in (child.get("class") or [])))
            buckets.setdefault((child.name, classes), []).append(child)
        for (name, classes), members in buckets.items():
            if len(members) < MIN_ROWS or name in ("script", "style", "br", "option"):
                continue
            if any(CHROME_CLASS.search(c) for c in classes):
                continue
            if not any(_text(m) for m in members):
                continue
            groups.append((parent, members))
    return groups


def find_next_page(html: str, url: str, soup: BeautifulSoup | None = None) -> str | None:
    """The 'next page' link, if this screen paginates."""
    doc = soup or BeautifulSoup(html, "lxml")

    for tag in doc.find_all("a", href=True):
        rel = " ".join(tag.get("rel") or []).lower()
        if rel == "next" or NEXT_TEXT.match(_text(tag)):
            href = urljoin(url, tag["href"])
            if href.split("#")[0] != url.split("#")[0]:
                return href.split("#")[0]

    for tag in doc.find_all("a", href=True):
        href = urljoin(url, tag["href"]).split("#")[0]
        if href != url and (PAGE_PARAM.search(href) or PAGE_PATH.search(href)):
            return href
    return None


def follow_pattern(url: str, next_url: str | None) -> str | None:
    """A crawl `follow:` regex covering this list and its later pages.

    Deliberately anchored to the path so it cannot wander into detail pages:
    /legacy/customers and /legacy/customers?page=2, never /legacy/customers/5001.
    """
    path = urlparse(url).path.rstrip("/") or "/"
    if next_url and PAGE_PATH.search(next_url):
        return re.escape(path) + r"(/page/\d+)?/?($|\?)"
    return re.escape(path) + r"/?($|\?)"


def propose(html: str, url: str) -> Proposal | None:
    """The best repeated structure on the page, or None if there isn't one."""
    doc = BeautifulSoup(html, "lxml")
    candidates: list[Proposal] = []

    for table in doc.find_all("table"):
        got = _table_proposal(table, url)
        if got:
            candidates.append(got)

    if not candidates:
        for parent, members in _repeated_blocks(doc):
            got = _card_proposal(parent, members, url)
            if got:
                candidates.append(got)

    if not candidates:
        return None

    best = max(candidates, key=lambda p: p.score)
    if len(candidates) > 1:
        best.notes.append(
            f"{len(candidates)} repeated structures on this page; the largest "
            f"({best.row_count} rows) was used."
        )

    best.next_url = find_next_page(html, url, doc)
    best.follow = follow_pattern(url, best.next_url)
    if not best.next_url:
        best.notes.append(
            "No next-page link found — if the list paginates, check whether it "
            "does so over XHR and capture that instead."
        )
    return best
