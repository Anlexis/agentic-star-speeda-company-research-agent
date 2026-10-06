"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner SPEEDA workflow
graph has run. GraphNode.merge_output() maps the inner result into the outer
state; this node shapes the caller-facing `formatted_output` and is the last
place anything can be stopped before it leaves the agent.

The domain output gate is the MODULE-LEVEL `_security_gate_output()` below,
called from execute(). It is deliberately NOT an instance method and NOT the
framework `_extra_security_gate_output` hook - the framework gate methods are
@final on FunctionNode and the real SDK auto-wraps `_extra_` hooks (which
breaks the .invoke() chain), so domain checks live in a module-level helper
invoked inline.

Two independent layers, each with its own audit signal:

  1. a credential/PII pattern scan over every string leaf of the response,
     including nested ones;
  2. the external precision grid - every monetary figure the response renders
     is expressed in units of 1,000. The grid runs AFTER the pattern scan and
     the response is re-scanned afterwards, because snapping a number can
     destroy the shape a pattern scan recognises.

EVERY non-success return - a gate violation AND a pre-existing inner-workflow
error - goes through the one module-level `_contain()` helper: ERROR status,
every output-bearing field cleared, and a truthy replacement `formatted_output`.
Raising, or returning ERROR while leaving those fields in place, is not
containment: `AgentBaseGraph.get_output()` projects `formatted_output or result`
with NO status check, so the un-gated inner answer would ship inside the error
envelope.

**What the ERROR envelope may say: closed-set labels only.** It carries a
constant reason code chosen by this module (one of `ERROR_REASONS`) and nothing
else - never `error_log`, never the gate's violation entries, never any other
node-authored string. Those lines can embed an upstream SPEEDA response body,
identifiers, names or caller-derived fragments, and truncating them to a first
line, stripping paths or redacting credentials is not a closed set. `error_log`
stays the INTERNAL channel: the state reducer appends to it and the audit trail
needs it; it is simply never projected to the caller. Gate violations are
written to `error_log` naming the offending PATH (a credential-shaped mapping
key is withheld from the label, never quoted), and the audit event carries the
count.

No error envelope carries SPEEDA record evidence either. `record_id` /
`record_ref` are this agent's LOOKUP EVIDENCE - the SUCCESS branch of the gate
below REFUSES an output that lacks them - so returning them under an ERROR
status would tell a caller being informed of failure that a company record was
nonetheless resolved, and which company it was. The reason code is a constant,
so the mapping stays TRUTHY - a falsy `formatted_output` would re-open the
`or result` projection the containment exists to prevent.
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.security import CREDENTIAL_LIKE_RE, walk_strings, withhold_credentials

# Credential-shaped strings that must never reach the caller (defence in depth -
# the framework credential scan in FunctionNode also runs on every result).
_CREDENTIAL_LIKE_RE = CREDENTIAL_LIKE_RE
# Identifier-shaped personal data that a third-party record could carry into a
# research answer. Scanned before the grid, because the grid rewrites digit runs.
_PII_LIKE_RE = re.compile(
    r"\b(?:SSN|TAX|NIN)\s*[:#]?\s*\d{3}-\d{2}-\d{4}\b"
    r"|\b\d{3}-\d{2}-\d{4}\b"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

# --- external precision grid -------------------------------------------------
#
# Monetary figures in the response are expressed in units of 1,000: the answer
# reports aggregates, not the source system's exact line-item values. The
# renderer produces them on the grid; this gate ENFORCES it, so a rendering
# regression cannot quietly put a full-precision figure on the external surface.
_EXTERNAL_ROUND_UNIT = 1000

# A monetary value is identified by FORM and by CURRENCY CONTEXT, never by
# magnitude: comma-grouped numbers and 5+-digit runs are monetary by form; a
# bare 1-4 digit number is monetary when a currency marker sits on either side
# of it, attached or separated. Structural tokens (counts, page numbers, years
# with no currency adjacency) never match.
_SYMBOL = r"[¥￥$€£円₩]"
_ANY_CODE = r"[A-Z]{3}(?![A-Za-z])"

# Currency codes recognised when the marker is ATTACHED to the value with no
# whitespace between them. This is a currency list, not a list of identifiers to
# exclude: attached "<3 uppercase letters><digits>" and "<3 uppercase
# letters>-<digits>" are the shapes an identifier takes (SKF-6205, STU-1234,
# ENE-FAC-20260712-001), and they are indistinguishable from an attached
# negative amount (JPY-9999) by form alone. Naming the currencies resolves the
# ambiguity in the direction that keeps record references intact, and it stays
# closed and stable, whereas an identifier list could never be complete.
# SEPARATED markers are NOT restricted: "SKF 6205" still snaps, because a
# 3-letter word followed by whitespace and a number is currency context by the
# same fail-safe rule the rest of this grammar uses.
_ISO_CODE = (
    r"(?:JPY|USD|EUR|GBP|CNY|CNH|KRW|AUD|CAD|CHF|HKD|SGD|TWD|THB|INR|IDR|MYR|PHP|VND"
    r"|BRL|MXN|SEK|NOK|DKK|PLN|CZK|TRY|NZD|RUB|ZAR|SAR|AED|ILS)(?![A-Za-z])"
)

# Marker-to-value delimiter: horizontal whitespace and at most ONE newline. A
# plain `\s*` spans a paragraph break, which lets a 3-letter uppercase word that
# ends a line bind to the number opening the next block and rewrite the
# document's own structure ("Currency: JPY\n\n3. Cash Position" -> "0. Cash
# Position"). Every leak form (spaces, tabs, one newline, signed, symmetric,
# comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"
# The same delimiter with at least one whitespace character present.
_GATE_DELIM_WS = r"(?:[ \t]+|[ \t]*\n[ \t]*)"

# Marker BEFORE the value: any 3-letter uppercase word or symbol when separated
# by whitespace; only a currency code or symbol when attached.
_CURRENCY_MARKER = rf"(?:(?:{_ANY_CODE}|{_SYMBOL}){_GATE_DELIM_WS}|(?:{_ISO_CODE}|{_SYMBOL}){_GATE_DELIM})"
# Marker AFTER the value, same rule mirrored.
_CURRENCY_MARKER_POST = rf"(?:{_GATE_DELIM_WS}(?:{_ANY_CODE}|{_SYMBOL})|{_GATE_DELIM}(?:{_ISO_CODE}|{_SYMBOL}))"

# Every value alternative absorbs its decimal fraction into the SAME token.
# Without that, the fraction of "8.512345" is a standalone 5+-digit run in its
# own right and gets rewritten ("8.512,000"), and a decimal amount snaps its
# integer part while the fraction dangles ("JPY 1234.56" -> "JPY 1,000.56"),
# which is neither the true figure nor a grid value.
#
# The `(?!\.\d)` arm is what makes the absorption stick. A plain `(?:\.\d+)?`
# lets the engine backtrack out of the fraction and re-match the integer part
# alone whenever the text right after the fraction fails the trailing guard, so
# "JPY 1234.56m" would go back to matching "JPY 1234" and the dangling-fraction
# bug returns. Either the fraction is taken whole, or there is none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

# Identifier guards, widened to THIS template's render alphabet. The generic
# guard class is [A-Za-z0-9-]; that is not enough here, because the identifiers
# this agent renders are also delimited by `_`, `/`, `:`, `=` and quotes:
#
#   speeda://companies/1234567     a numeric company code behind a path segment
#   id=1234567                     the confirmation line's key=value form
#   'c-7203' / '1234567'           the quoted record label
#   sku_48210                      an underscore-joined code
#
# Without those characters in the class, a company code that happens to be a
# 5+-digit run is read as a monetary figure and rewritten - the agent would
# report a record id that does not exist. `.` is in the LEADING guard only: in
# the trailing guard it would let an amount ending a sentence escape the grid
# ("the filing totals JPY 9999.").
_LEAD_GUARD = r"(?<![A-Za-z0-9_/:='\"\.\-])"
_TRAIL_GUARD = r"(?![A-Za-z0-9_/:'\"\-])"

_NUM_TOKEN_RE = re.compile(
    _LEAD_GUARD
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The comma-grouped alternative comes FIRST: matching is leftmost-first, so
    # without it "JPY 1,234" matches as marker + "1" and the snap mangles the
    # number ("JPY 0,234"). An on-grid "JPY 1,000" must stay byte-identical and
    # an off-grid "JPY 1,234" must snap as 1234, not as 1.
    + rf"(?:(?P<pre>{_CURRENCY_MARKER})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})(?P<post>{_CURRENCY_MARKER_POST})"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))" + _TRAIL_GUARD
)

SCHEMA_NOTE = (
    "Monetary figures in this response are reported as aggregates rounded to " "the nearest 1,000 currency units."
)

# Response fields that carry identifiers rather than narrative, and are
# therefore outside the grid: they are validated identifier shapes, they never
# express an amount, and rewriting a digit run inside one would corrupt the
# record reference the caller needs.
_IDENTIFIER_FIELDS = frozenset({"record_id", "record_ref", "company_name", "intent"})


def _enforce_precision(text: str) -> "tuple[str, int]":
    """Snap every monetary-form token onto the external grid.

    Returns (text, snap_count). A snap means a full-precision figure reached the
    external surface and was rounded. The currency marker, the original
    delimiter whitespace and the explicit sign are preserved.
    """
    snaps = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal snaps
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal fraction, and the
        # whole amount - not just its integer part - is what sits on the grid.
        value = float(token.replace(",", ""))
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        snaps += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, text), snaps


def _scan_patterns(value: Any) -> "list[str]":
    """Report credential / PII findings in every string reachable in *value*.

    Walks nested mappings and sequences, and mapping KEYS as well as values:
    caller or third-party text riding one level down
    (`speeda_request.company_code`) is on the external surface just as much as a
    top-level string, and a gate that only reads top-level values reports zero
    findings on exactly the payload that leaks.

    A finding names the offending PATH, never the value - and the path itself is
    run through `withhold_credentials()`, because a credential-shaped KEY would
    otherwise be quoted verbatim by the very entry that reports it, into
    `error_log`, where the framework's own scan raises on the node result.
    """
    problems: list[str] = []
    for path, text in walk_strings(value):
        label = withhold_credentials(path)
        if _CREDENTIAL_LIKE_RE.search(text):
            problems.append(f"credential-like value in formatted_output{label}")
        if _PII_LIKE_RE.search(text):
            problems.append(f"personal-identifier-like value in formatted_output{label}")

    # The framework's own detector is the FLOOR, not a replacement: it knows
    # `AKIA`, `sk_live_` and db connection strings, which the pattern above does
    # not, while the pattern above reads mapping keys, which it does not. Its
    # finding type is a closed-set label and carries no path.
    for finding in detect_credentials_in_value(value):
        problems.append(f"credential pattern '{finding['type']}' in formatted_output")
    return problems


def _apply_grid(value: Any, field: str) -> "tuple[Any, int]":
    """Apply the precision grid to the narrative parts of *value*."""
    if field in _IDENTIFIER_FIELDS:
        return value, 0
    if isinstance(value, str):
        return _enforce_precision(value)
    if isinstance(value, dict):
        snaps = 0
        shaped: dict[Any, Any] = {}
        for key, item in value.items():
            shaped[key], count = _apply_grid(item, str(key))
            snaps += count
        return shaped, snaps
    if isinstance(value, list):
        snaps = 0
        items = []
        for item in value:
            shaped_item, count = _apply_grid(item, field)
            items.append(shaped_item)
            snaps += count
        return items, snaps
    return value, 0


def _security_gate_output(
    formatted_output: dict[str, Any], is_success: bool
) -> "tuple[dict[str, Any], list[str], int]":
    """Domain output gate (module-level; called from PostProcessNode.execute()).

    Returns (gated_output, violations, snap_count). Violations block:
      - a SUCCESS response with no record evidence (record_id/record_ref), which
        would misrepresent the research outcome to the caller;
      - a credential- or personal-identifier-shaped string anywhere in the
        response, before the grid runs and again after it.
    """
    problems: list[str] = []
    if is_success and not (formatted_output.get("record_id") or formatted_output.get("record_ref")):
        problems.append("output gate: SUCCESS response missing record_id/record_ref evidence")

    # Layer 1: pattern scan BEFORE the grid. The grid rewrites digit runs, which
    # can destroy the shape a pattern scan matches on, so scanning only after it
    # would let a mangled-but-still-sensitive value through.
    problems.extend(f"output gate: {p}" for p in _scan_patterns(formatted_output))
    if problems:
        return formatted_output, problems, 0

    # Layer 2: the precision grid.
    gated, snaps = _apply_grid(formatted_output, "")
    gated_dict: dict[str, Any] = gated if isinstance(gated, dict) else dict(formatted_output)

    # Re-scan: the grid may have produced a new string.
    problems.extend(f"output gate: {p}" for p in _scan_patterns(gated_dict))
    return gated_dict, problems, snaps


# Every state field that can carry released content or record evidence to the
# caller. On EVERY error return - a gate violation and a pre-existing
# inner-workflow error alike - all of them are overwritten, because the response
# envelope reads formatted_output/result even on an error status, and because
# omitting a field from one envelope is not clearing it: a checkpoint or a
# downstream reader picks it straight back up out of state.
#
# record_id / record_ref / company_id are the LOOKUP EVIDENCE, not content, and
# they are cleared for that reason: a caller told the operation failed must not
# learn which company record was resolved.
_CLEARED_OUTPUT_STATE: "dict[str, Any]" = {
    "result": None,
    "confirmation": "",
    "summary": "",
    "company_name": "",
    "speeda_payload": None,
    "intent": "",
    "record_id": "",
    "record_ref": "",
    "company_id": "",
}

# Reason codes - the ONLY values the caller-visible ERROR envelope may carry.
# Chosen here, never derived from state, so the envelope is a closed set: it
# says WHAT happened, never to which company and never in whose words.
_REASON_WORKFLOW_FAILED = "speeda_workflow_failed"  # the inner workflow reported an error
_REASON_OUTPUT_WITHHELD = "output_withheld_by_gate"  # the output gate refused the response
ERROR_REASONS = frozenset({_REASON_WORKFLOW_FAILED, _REASON_OUTPUT_WITHHELD})


def _contain(reason: str, new_errors: "list[str] | None" = None) -> "dict[str, Any]":
    """The node result for ANY non-success outcome - the single error shape.

    ERROR status, every output-bearing field cleared, and an envelope made of
    closed-set labels only: `reason` is one of ERROR_REASONS and there is no
    other key. `new_errors` (gate violations - path labels only) are appended to
    `error_log`, the internal channel the state reducer accumulates, and never
    enter the envelope. Nothing is read out of state: not the record, not
    `error_log` (its inner entries are already there - re-emitting them would
    duplicate every line), not the count (that is an audit signal, and a count
    of nothing is still a statement about the run).

    The constant `reason` key keeps the mapping TRUTHY, so the framework's
    `formatted_output or result` projection (AgentBaseGraph.get_output() applies
    no status check) serves this envelope and never whatever survived in
    `result`.
    """
    contained: dict[str, Any] = {
        **_CLEARED_OUTPUT_STATE,
        "formatted_output": {"reason": reason},
        "status": AgentStatus.ERROR.value,
    }
    if new_errors:
        contained["error_log"] = list(new_errors)
    return contained


class PostProcessNode(FunctionNode):
    """Format the final agent output behind the output gate."""

    # Read-only formatting of the already-produced result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        errored = state.get("status") == AgentStatus.ERROR.value

        # If the inner workflow errored, preserve the error status (do not mask
        # it) and publish NOTHING of it. error_log already carries the inner
        # entries and the state reducer appends, so re-emitting them here would
        # duplicate every line - and they are not the caller's to read: the
        # framework writes a formatted traceback into error_log when a node
        # raises, and upstream reasons can embed a SPEEDA response body.
        if errored:
            # CONTAIN the failure rather than re-publishing the run:
            #   * the envelope is a closed-set reason code and nothing else -
            #     a failure report must not disclose which SPEEDA company was
            #     resolved, nor quote any node's own error text;
            #   * the delta clears every output-bearing field, so the identifiers
            #     and the research summary cannot be recovered from the
            #     checkpoint or by a downstream reader either.
            #
            # Outcome signals only - a closed-set reason code, the intent label
            # and a count. The audit log is not a store for company research
            # content, and the count belongs here rather than in the envelope.
            emit_trace_event(
                "post_process_error_contained",
                {
                    "reason": _REASON_WORKFLOW_FAILED,
                    "intent": state.get("intent", ""),
                    "n_errors": len(state.get("error_log", []) or []),
                },
                state,
            )
            return _contain(_REASON_WORKFLOW_FAILED)

        payload = from_json(state.get("speeda_payload"), {}) or {}
        formatted_output = {
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "company_name": state.get("company_name", ""),
            "intent": state.get("intent", ""),
            "summary": state.get("summary", ""),
            "confirmation": state.get("confirmation", ""),
            # The request that was issued, described rather than echoed: the
            # filter values are caller text, and reflecting them into the
            # response makes the response a channel the caller writes.
            "speeda_request": {
                "company_code": str(payload.get("company_code", "")),
                "detail": str(payload.get("detail", "")),
                "filter_count": len(payload.get("filters", []) or []),
            },
            "schema_note": SCHEMA_NOTE,
        }

        gated_output, violations, snaps = _security_gate_output(formatted_output, is_success=True)
        if violations:
            emit_trace_event(
                "post_process_output_blocked",
                {
                    "reason": _REASON_OUTPUT_WITHHELD,
                    "n_violations": len(violations),
                    "intent": state.get("intent", ""),
                },
                state,
            )
            # Containment: clear every output-bearing field. The graph falls back
            # to state["result"] when formatted_output is absent or falsy, so a
            # bare error return would still ship the un-gated inner answer. The
            # violations go to error_log only - they are path labels this module
            # composed, but they are still not a closed set, so the caller gets
            # the reason code alone.
            return _contain(_REASON_OUTPUT_WITHHELD, violations)

        # Audit the final response shaping - outcome signals only, no content.
        emit_trace_event(
            "post_process_complete",
            {
                "intent": state.get("intent", ""),
                "has_record_id": bool(state.get("record_id")),
                "precision_snaps": snaps,
            },
            state,
        )

        return {
            "formatted_output": gated_output,
            "status": AgentStatus.SUCCESS.value,
        }
