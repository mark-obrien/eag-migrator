import datetime as dt
import decimal

import pytest

from eag_migrator.transforms import TransformError, apply_chain


def chain(value, steps):
    return apply_chain(value, steps, row={}, ctx=None)


def test_trim_lower_email():
    assert chain("  Dana@Example.COM ", ["trim", "email"]) == "dana@example.com"


def test_invalid_email_is_null_by_default():
    assert chain("not-an-email", ["email"]) is None


def test_invalid_email_raises_when_strict():
    with pytest.raises(TransformError):
        chain("not-an-email", [{"email": {"strict": True}}])


def test_phone_normalises_to_e164():
    assert chain("(555) 123-4567", ["phone"]) == "+15551234567"
    assert chain("1-555-123-4567", ["phone"]) == "+15551234567"


def test_digits_strips_everything_that_is_not_a_number():
    assert chain("(555) 123-4567", ["digits"]) == "5551234567"
    assert chain("+1 555 123 4567", ["digits"]) == "15551234567"
    assert chain(None, ["digits"]) is None
    assert chain("no digits here", ["digits"]) is None


def test_digits_can_keep_just_the_last_n():
    """v3 refuses E.164 — "Phone number must contain only digits" — and holds
    ten bare digits, so the country code `phone` adds has to come back off."""
    assert chain("(555) 123-4567", ["phone", {"digits": {"last": 10}}]) == "5551234567"
    assert chain("1-555-123-4567", ["phone", {"digits": {"last": 10}}]) == "5551234567"
    # Shorter than the window is left whole rather than padded or refused.
    assert chain("12345", ["digits", {"digits": {"last": 10}}]) == "12345"


def test_vin_uppercases_and_strips_separators():
    assert chain("5NPE24AF1FH-012345", ["vin"]) == "5NPE24AF1FH012345"


def test_strict_vin_rejects_wrong_length():
    with pytest.raises(TransformError):
        chain("ABC123", [{"vin": {"strict": True}}])


def test_nags_part_number_is_normalised():
    assert chain("dw01234 gtyn", ["nags"]) == "DW01234GTYN"


def test_mysql_zero_date_becomes_null():
    assert chain("0000-00-00 00:00:00", ["to_datetime"]) is None


def test_datetime_parses_common_formats():
    assert chain("2021-03-04 09:15:00", ["to_datetime"]) == dt.datetime(2021, 3, 4, 9, 15)
    assert chain("03/04/2021", ["to_datetime"]) == dt.datetime(2021, 3, 4)


def test_decimal_strips_currency_formatting():
    assert chain("$1,250.00", [{"decimal": {"places": 2}}]) == decimal.Decimal("1250.00")


def test_bool_accepts_yn():
    assert chain("Y", ["bool"]) is True
    assert chain("n", ["bool"]) is False
    with pytest.raises(TransformError):
        chain("maybe", ["bool"])


def test_map_falls_back_to_wildcard():
    steps = [{"map": {"values": {"A": "active", "*": "inactive"}}}]
    assert chain("A", steps) == "active"
    assert chain("Z", steps) == "inactive"


def test_map_strict_raises_on_unknown():
    steps = [{"map": {"values": {"A": "active"}, "strict": True}}]
    with pytest.raises(TransformError):
        chain("Z", steps)


def test_strip_html_keeps_text():
    assert chain("<p>Prefers <b>mobile</b> service</p>", ["strip_html"]) == "Prefers mobile service"


def test_postal_code_formats_zip_plus_four():
    assert chain("941072233", [{"postal_code": {"region": "US"}}]) == "94107-2233"


def test_concat_skips_blanks():
    assert apply_chain(["Dana", None], [{"concat": {"sep": " "}}], row={}, ctx=None) == "Dana"


def test_truncate_respects_length():
    assert chain("abcdefghij", [{"truncate": {"length": 4}}]) == "abcd"


def test_php_unserialize_reads_wordpress_meta():
    raw = 'a:2:{s:5:"phone";s:10:"5551112222";s:6:"mobile";b:1;}'
    assert chain(raw, ["php_unserialize"]) == {"phone": "5551112222", "mobile": True}


def test_unknown_transform_names_itself():
    with pytest.raises(TransformError, match="unknown transform"):
        chain("x", ["definitely_not_a_transform"])
