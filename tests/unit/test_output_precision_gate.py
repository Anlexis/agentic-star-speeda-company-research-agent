# CMN-C2-287 - Unit tests: the external precision grid in
# src/nodes/post_process_node.py.
#
# The response reports aggregates: every monetary figure it renders sits in units
# of 1,000. The renderer produces them that way; this gate ENFORCES it. Each case
# below corresponds to a form the grammar has to get right, and each is asserted
# in BOTH directions - a leak form must snap, a structural token must come back
# byte-identical. A gate that only snaps is a gate that corrupts identifiers.

import pytest

from src.nodes.post_process_node import _apply_grid, _enforce_precision, _scan_patterns

SNAPS = [
    ("revenue 12345678", "revenue 12,346,000", "unformatted run of 5+ digits"),
    ("operating income 1234567", "operating income 1,235,000", "5+ run in prose"),
    ("1,234,567", "1,235,000", "comma-grouped standalone"),
    ("JPY 1,234", "JPY 1,000", "marker + grouped value, matched whole"),
    ("JPY 9999", "JPY 10,000", "marker + short value in currency context"),
    ("9999 JPY", "10,000 JPY", "value then marker (symmetric)"),
    ("-9999 USD", "-10,000 USD", "signed value then marker"),
    ("JPY-9999", "JPY-10,000", "signed, attached to a currency code"),
    ("JPY9999", "JPY10,000", "attached to a currency code"),
    ("JPY +9999", "JPY +10,000", "explicit plus, sign preserved"),
    ("JPY  9999", "JPY  10,000", "multi-space delimiter preserved"),
    ("JPY\t9999", "JPY\t10,000", "tab delimiter preserved"),
    ("JPY\n9999", "JPY\n10,000", "single newline delimiter preserved"),
    ("¥9999", "¥10,000", "currency symbol attached"),
    ("¥-9999", "¥-10,000", "symbol + sign attached"),
    ("9999円", "10,000円", "fullwidth suffix marker"),
    ("JPY 1234.56", "JPY 1,000", "off-grid decimal amount snaps as ONE number"),
    ("the filing totals JPY 9999.", "the filing totals JPY 10,000.", "amount ends a sentence"),
    ("SKF 6205", "SKF 6,000", "separated 3-letter word stays fail-safe currency context"),
]

IDENTICAL = [
    ("JPY 1,000", "already on the grid"),
    ("¥1,000", "symbol, already on the grid"),
    ("Currency: JPY\n\n3. Cash Position", "delimiter must not span a paragraph break"),
    ("8.512345", "bare decimal - the fraction is not a monetary run"),
    ("9999.99999%", "percentage"),
    ("ratio 0.123456", "ratio after a word"),
    ("JPY 1234.56m", "decimal + suffix: the fraction cannot be backtracked out of"),
    ("speeda://companies/1234567", "numeric company code behind a path segment"),
    ("id=1234567", "numeric code in the confirmation's key=value form"),
    ("'1234567'", "quoted numeric record label"),
    ("sku_48210", "underscore-joined code"),
    ("c-7203", "hyphenated company code"),
    ("SKF-6205", "3-letter prefix + hyphen + digits is an identifier"),
    ("STU-1234", "same shape, different code"),
    ("SKF6205", "3-letter prefix attached to digits"),
    ("ENE-FAC-20260712-001", "long dashed identifier"),
    ("fy2025 highlights", "fiscal year"),
    ("in 2026", "bare year, no currency adjacency"),
    ("STAR 2026", "embedded acronym is not a standalone 3-letter word"),
    ("p.21", "page reference"),
    ("v12", "version tag"),
    ("90d horizon", "horizon"),
    ("ind-a1b2", "industry code"),
    ("SSN 123-45-6789", "personal-identifier shape survives for the pattern scan"),
]


@pytest.mark.parametrize("text,expected,label", SNAPS, ids=[c[2] for c in SNAPS])
def test_leak_forms_snap_onto_the_grid(text, expected, label):
    gated, snaps = _enforce_precision(text)
    assert gated == expected
    assert snaps == 1


@pytest.mark.parametrize("text,label", IDENTICAL, ids=[c[1] for c in IDENTICAL])
def test_structural_tokens_are_byte_identical(text, label):
    gated, snaps = _enforce_precision(text)
    assert gated == text
    assert snaps == 0


def test_no_magnitude_exemption():
    """If the invariant says every amount, the gate covers every magnitude."""
    assert _enforce_precision("JPY 9,999")[0] == "JPY 10,000"
    assert _enforce_precision("JPY 999,999,999")[0] == "JPY 1,000,000,000"


def test_grid_skips_identifier_fields_only():
    payload = {
        "record_id": "1234567",
        "record_ref": "speeda://companies/1234567",
        "company_name": "12345678 holdings",
        "intent": "summarize_financials",
        "summary": "revenue 12345678",
    }
    gated, snaps = _apply_grid(payload, "")
    assert gated["record_id"] == "1234567"
    assert gated["record_ref"] == "speeda://companies/1234567"
    assert gated["company_name"] == "12345678 holdings"
    assert gated["summary"] == "revenue 12,346,000"
    assert snaps == 1


def test_grid_walks_nested_values():
    gated, snaps = _apply_grid({"detail": {"note": ["revenue 12345678"]}}, "")
    assert gated["detail"]["note"] == ["revenue 12,346,000"]
    assert snaps == 1


class TestPatternScan:
    """A nested probe alone cannot separate 'gate is blind' from 'probe is wrong',
    so the top-level control runs beside it."""

    def test_top_level_credential_is_found(self):
        bearer_like = "Bearer " + "a" * 24
        findings = _scan_patterns({"summary": bearer_like})
        assert findings and "['summary']" in findings[0]

    def test_nested_credential_is_found(self):
        bearer_like = "Bearer " + "a" * 24
        findings = _scan_patterns({"speeda_request": {"company_code": bearer_like}})
        assert findings and "['speeda_request']['company_code']" in findings[0]

    def test_credential_inside_a_list_is_found(self):
        findings = _scan_patterns({"notes": ["clean", "sk-" + "b" * 24]})
        assert findings and "['notes'][1]" in findings[0]

    def test_personal_identifier_is_found(self):
        findings = _scan_patterns({"summary": "SSN 123-45-6789 on file"})
        assert findings and "personal-identifier" in findings[0]

    def test_clean_payload_has_no_findings(self):
        assert _scan_patterns({"summary": "revenue 12,346,000", "record_id": "c-7203"}) == []
