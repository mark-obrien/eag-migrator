"""Named value transforms referenced from the mapping YAML.

A transform step is either a bare name:

    transform: [trim, lower, nullif_empty]

or a single-key mapping carrying arguments:

    transform:
      - truncate: {length: 255}
      - lookup: {entity: customers, required: true}

Register new ones with @transform("name"). Signature is
``fn(value, *, row, ctx, **params)``.
"""

from __future__ import annotations

import datetime as dt
import decimal
import html
import json
import re
import unicodedata
from typing import Any, Callable

REGISTRY: dict[str, Callable[..., Any]] = {}


class TransformError(ValueError):
    """Raised when a value cannot be transformed. Caught per-row by the runner."""


def transform(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[name] = fn
        return fn

    return deco


# --- strings ----------------------------------------------------------------


@transform("trim")
def _trim(value: Any, **_: Any) -> Any:
    """Strip leading and trailing whitespace."""
    return value.strip() if isinstance(value, str) else value


@transform("lower")
def _lower(value: Any, **_: Any) -> Any:
    """Lowercase the value."""
    return value.lower() if isinstance(value, str) else value


@transform("upper")
def _upper(value: Any, **_: Any) -> Any:
    """Uppercase the value."""
    return value.upper() if isinstance(value, str) else value


@transform("title")
def _title(value: Any, **_: Any) -> Any:
    """Title-case the value ("honda accord" -> "Honda Accord")."""
    return value.title() if isinstance(value, str) else value


@transform("nullif_empty")
def _nullif_empty(value: Any, **_: Any) -> Any:
    """Turn empty and whitespace-only strings into NULL."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


@transform("truncate")
def _truncate(value: Any, *, length: int = 255, **_: Any) -> Any:
    """Cut a string to `length` characters so it fits the target column."""
    if isinstance(value, str) and len(value) > length:
        return value[:length]
    return value


@transform("strip_html")
def _strip_html(value: Any, **_: Any) -> Any:
    """Reduce HTML to plain text, dropping script/style and unescaping entities."""
    if not isinstance(value, str):
        return value
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", value, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


@transform("slugify")
def _slugify(value: Any, **_: Any) -> Any:
    """Make a URL-safe slug ("Front Windshield" -> "front-windshield")."""
    if not isinstance(value, str):
        return value
    norm = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")


@transform("concat")
def _concat(value: Any, *, sep: str = " ", **_: Any) -> Any:
    """Join a multi-column `from: [a, b]` into one string, skipping blanks."""
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return sep.join(parts) or None
    return value


@transform("split")
def _split(value: Any, *, sep: str = " ", index: int = 0, **_: Any) -> Any:
    """Split on `sep` and keep the part at `index`."""
    if not isinstance(value, str):
        return value
    parts = value.split(sep)
    try:
        return parts[index]
    except IndexError:
        return None


# --- numbers & booleans -----------------------------------------------------


@transform("int")
def _int(value: Any, **_: Any) -> Any:
    """Parse an integer; raises on anything that is not one."""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TransformError(f"not an integer: {value!r}") from exc


@transform("float")
def _float(value: Any, **_: Any) -> Any:
    """Parse a float; raises on anything that is not one."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TransformError(f"not a number: {value!r}") from exc


@transform("decimal")
def _decimal(value: Any, *, places: int = 2, **_: Any) -> Any:
    """Parse currency-ish text into a Decimal ("$1,250.00" -> 1250.00)."""
    if value is None or value == "":
        return None
    try:
        quant = decimal.Decimal(10) ** -places
        return decimal.Decimal(str(value).replace("$", "").replace(",", "").strip()).quantize(quant)
    except (decimal.InvalidOperation, TypeError, ValueError) as exc:
        raise TransformError(f"not a decimal: {value!r}") from exc


TRUE_SET = {"1", "true", "t", "yes", "y", "on", "active", "enabled"}
FALSE_SET = {"0", "false", "f", "no", "n", "off", "inactive", "disabled", ""}


@transform("bool")
def _bool(value: Any, **_: Any) -> Any:
    """Read 1/0, Y/N, true/false, yes/no, on/off, active/inactive as a boolean."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in TRUE_SET:
        return True
    if s in FALSE_SET:
        return False
    raise TransformError(f"not a boolean: {value!r}")


# --- dates ------------------------------------------------------------------

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y%m%d",
]


@transform("to_datetime")
def _to_datetime(value: Any, *, formats: list[str] | None = None, **_: Any) -> Any:
    """Parse a datetime from common formats or a unix epoch; MySQL zero-dates become NULL."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)
    if isinstance(value, (int, float)):  # unix epoch
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    s = str(value).strip()
    # MySQL's zero-date is not a real date; it maps to NULL.
    if s.startswith("0000-00-00"):
        return None
    for fmt in (formats or DATE_FORMATS):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise TransformError(f"unrecognised datetime: {value!r}") from exc


@transform("to_date")
def _to_date(value: Any, **kw: Any) -> Any:
    """Parse a date, discarding any time component."""
    got = _to_datetime(value, **kw)
    return got.date() if isinstance(got, dt.datetime) else got


@transform("now")
def _now(_value: Any, **__: Any) -> Any:
    """The current UTC time, ignoring the incoming value."""
    return dt.datetime.now(dt.timezone.utc)


# --- JSON -------------------------------------------------------------------


@transform("json_encode")
def _json_encode(value: Any, **_: Any) -> Any:
    """Serialise the value to a JSON string."""
    return None if value is None else json.dumps(value, default=str)


@transform("json_decode")
def _json_decode(value: Any, *, strict: bool = False, **_: Any) -> Any:
    """Parse a JSON string; NULL on malformed input unless strict."""
    if value is None or value == "":
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        if strict:
            raise TransformError(f"invalid JSON: {str(value)[:80]!r}") from exc
        return None


@transform("php_unserialize")
def _php_unserialize(value: Any, **_: Any) -> Any:
    """Minimal PHP serialize() reader — WordPress/WooCommerce meta uses it heavily."""
    if not isinstance(value, str) or not value:
        return value

    pos = 0

    def parse() -> Any:
        nonlocal pos
        kind = value[pos]
        if kind == "N":
            pos += 2
            return None
        if kind in "id":
            end = value.index(";", pos)
            raw = value[pos + 2 : end]
            pos = end + 1
            return int(raw) if kind == "i" else float(raw)
        if kind == "b":
            end = value.index(";", pos)
            raw = value[pos + 2 : end]
            pos = end + 1
            return raw == "1"
        if kind == "s":
            colon = value.index(":", pos + 2)
            length = int(value[pos + 2 : colon])
            start = colon + 2
            out = value[start : start + length]
            pos = start + length + 2
            return out
        if kind == "a":
            colon = value.index(":", pos + 2)
            count = int(value[pos + 2 : colon])
            pos = colon + 2
            result: dict[Any, Any] = {}
            for _ in range(count):
                k = parse()
                result[k] = parse()
            pos += 1
            return result
        raise TransformError(f"unsupported PHP serialization marker {kind!r}")

    try:
        return parse()
    except TransformError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TransformError(f"malformed PHP serialized value: {str(value)[:80]!r}") from exc


# --- domain-specific --------------------------------------------------------


@transform("phone")
def _phone(value: Any, *, region: str = "US", **_: Any) -> Any:
    """Normalise to E.164. Anything that isn't a plausible number is left alone."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    if region == "US":
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        if len(digits) == 10:
            return f"+1{digits}"
    return f"+{digits}" if len(digits) > 10 else str(value).strip()


@transform("digits")
def _digits(value: Any, *, last: int = 0, **_: Any) -> Any:
    """Keep only the digits, optionally just the last N.

    Some targets will not take a formatted number: v3 answers "Phone number
    must contain only digits" and stores ten bare digits, so the E.164 that
    `phone` produces has to have its "+" and country code taken back off.
    Chain them — `[phone, {digits: {last: 10}}]` — so the number is normalised
    first and then reduced, rather than digits being scraped off whatever
    punctuation the source happened to use.
    """
    if value is None:
        return None
    kept = re.sub(r"\D", "", str(value))
    if not kept:
        return None
    return kept[-last:] if last and len(kept) > last else kept


@transform("clock")
def _clock(value: Any, **_: Any) -> Any:
    """An hour-of-day number into a v3 wall-clock string: 8 -> '08:00:00'.

    v2's appointment-window selects hold a bare hour (0-23); v3 stores
    customerTFStart / customerTFEnd as 'HH:MM:SS'. Anything that is not an
    hour in range is passed through untouched rather than forced.
    """
    if value is None or value == "":
        return None
    try:
        hour = int(str(value).strip())
    except (TypeError, ValueError):
        return value
    if 0 <= hour <= 23:
        return f"{hour:02d}:00:00"
    return value


@transform("email")
def _email(value: Any, *, strict: bool = False, **_: Any) -> Any:
    """Trim, lowercase and sanity-check an address; NULL if implausible."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if "@" not in s or "." not in s.split("@")[-1]:
        if strict:
            raise TransformError(f"invalid email: {value!r}")
        return None
    return s


@transform("vin")
def _vin(value: Any, *, strict: bool = False, **_: Any) -> Any:
    """Vehicle Identification Number: 17 chars, no I/O/Q."""
    if value is None:
        return None
    s = re.sub(r"[\s-]", "", str(value)).upper()
    if not s:
        return None
    if strict and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", s):
        raise TransformError(f"invalid VIN: {value!r}")
    return s


@transform("nags")
def _nags(value: Any, **_: Any) -> Any:
    """NAGS part number — uppercase, strip separators. The auto-glass parts key."""
    if value is None:
        return None
    s = re.sub(r"[\s]", "", str(value)).upper()
    return s or None


@transform("postal_code")
def _postal(value: Any, *, region: str = "US", **_: Any) -> Any:
    """Normalise a postal code (US: 5-digit, or ZIP+4 as 12345-6789)."""
    if value is None:
        return None
    s = str(value).strip().upper()
    if region == "US":
        digits = re.sub(r"\D", "", s)
        if len(digits) == 9:
            return f"{digits[:5]}-{digits[5:]}"
        if len(digits) == 5:
            return digits
    return s or None


# --- structural -------------------------------------------------------------


@transform("map")
def _map(value: Any, *, values: dict[Any, Any] | None = None, strict: bool = False, **_: Any) -> Any:
    """Translate enum-ish values, e.g. status codes."""
    table = values or {}
    key = value
    if key in table:
        return table[key]
    skey = str(value)
    if skey in table:
        return table[skey]
    if "*" in table:
        return table["*"]
    if strict:
        raise TransformError(f"no mapping for value {value!r}")
    return value


@transform("coalesce")
def _coalesce(value: Any, *, fallback: Any = None, **_: Any) -> Any:
    """First non-empty value from a multi-column `from`, else `fallback`."""
    if isinstance(value, (list, tuple)):
        for v in value:
            if v is not None and v != "":
                return v
        return fallback
    return value if value is not None and value != "" else fallback


@transform("lookup")
def _lookup(
    value: Any,
    *,
    entity: str,
    required: bool = False,
    ctx: Any = None,
    **_: Any,
) -> Any:
    """Resolve a v2 foreign key to the v3 id that entity's rows were given.

    Requires the referenced entity to run earlier (declare it in depends_on)
    with id_map: true.
    """
    if value is None or value == "":
        return None
    if ctx is None or getattr(ctx, "state", None) is None:
        raise TransformError("lookup requires run state; not available in this context")
    target = ctx.state.lookup_id(entity, value)
    if target is None:
        if getattr(ctx, "plan_mode", False):
            # Nothing has been migrated yet during a dry run, so an unresolved
            # lookup is expected rather than an error. Pass the source id
            # through so downstream transforms still see a realistic value; the
            # plan report flags the entity as carrying unverified lookups.
            ctx.note_unresolved_lookup(entity)
            return value
        if required:
            raise TransformError(
                f"no migrated '{entity}' row for source id {value!r} "
                f"(is '{entity}' listed in depends_on and migrated first?)"
            )
        return None
    return target


def apply_chain(value: Any, steps: list[Any], *, row: dict[str, Any], ctx: Any) -> Any:
    """Run a mapping's transform list over one value."""
    for step in steps:
        if isinstance(step, str):
            name, params = step, {}
        elif isinstance(step, dict) and len(step) == 1:
            name, params = next(iter(step.items()))
            params = params or {}
            if not isinstance(params, dict):
                raise TransformError(f"transform '{name}' arguments must be a mapping")
        else:
            raise TransformError(f"malformed transform step: {step!r}")

        fn = REGISTRY.get(name)
        if fn is None:
            raise TransformError(
                f"unknown transform '{name}'. Available: {', '.join(sorted(REGISTRY))}"
            )
        value = fn(value, row=row, ctx=ctx, **params)
    return value
