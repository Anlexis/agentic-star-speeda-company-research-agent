"""Regression: an ERROR envelope must not disclose SPEEDA record evidence.

Molt source review, 2026-09-04 (wave-9 batch, family-level finding over sixteen
sibling repos): "existing-ERROR rebuilds truthy `formatted_output` with record
evidence; and caller-visible `error_log` can carry interpolated exception /
upstream text rather than closed-set labels."

The `errored` branch of PostProcessNode rebuilt `formatted_output` from
`record_id` / `record_ref` read straight back out of state, and returned ONLY
that plus `status` - so every other output-bearing field (`result`,
`company_name`, `summary`, `confirmation`, `speeda_payload`, `company_id`,
`intent`) survived in state untouched.

Those identifiers ARE the lookup evidence: this node's own gate
(`_security_gate_output`, is_success=True) REFUSES a SUCCESS that lacks them. An
error envelope carrying them tells a caller who is being informed of a FAILURE
that a SPEEDA company record was nonetheless resolved, and which company it was.
`AgentBaseGraph.get_output()` projects `formatted_output or result` with NO
status check, so anything left in `result` ships in the error response too.

The containment contract is not "shape a nicer error mapping". It is: no
un-gated caller-facing content or record evidence survives on any error path -
neither in the replacement `formatted_output`, nor in the diagnostics it
carries, nor in the state left behind for a checkpoint or a downstream reader.

Reachability: on the compiled graph this branch is defence-in-depth.
`AgentBaseGraph.route()` sends an ERROR status to `finalize`, bypassing
`post_process`, and `BaseNode.__call__` short-circuits an already-errored state
before `execute()` runs. It is reachable by a direct `execute()` call, which is
how the tests below drive it - and how any future re-wiring would reach it.
"""

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.call_speeda_api_node import CallSpeedaApiNode
from src.nodes.post_process_node import ERROR_REASONS, PostProcessNode
from src.schemas.state import to_json
from src.services.security import WITHHELD, mask_field_name

# Record evidence + the research content hanging off it.
_COMPANY_CODE = "SPD-4307"
_RECORD_REF = "speeda://companies/SPD-4307"
_COMPANY_NAME = "Kitahara Precision K.K."
_SUMMARY = "Precision components maker; FY2025 revenue JPY 8,420,000,000"


def _errored_state() -> dict:
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": ["CallSpeedaApiNode: SPEEDA API error 500"],
        "record_id": _COMPANY_CODE,
        "record_ref": _RECORD_REF,
        "company_id": _COMPANY_CODE,
        "company_name": _COMPANY_NAME,
        "summary": _SUMMARY,
        "intent": "lookup_company",
        "confirmation": f"Researched SPEEDA company '{_COMPANY_NAME}' - ref={_RECORD_REF}",
        "speeda_payload": to_json({"company_code": _COMPANY_CODE, "detail": "profile", "filters": []}),
        "result": {"record_id": _COMPANY_CODE, "summary": _SUMMARY},
    }


def _flatten(value) -> str:
    """Render every reachable string in a returned value - nesting is not cover."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value)


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


class TestErrorEnvelopeContainment:
    def test_error_envelope_carries_no_record_evidence(self):
        out = PostProcessNode().execute(_errored_state())

        assert out["status"] == AgentStatus.ERROR.value

        # formatted_output must be PRESENT and TRUTHY. The framework projects
        # `formatted_output or result` with no status check, so a falsy value
        # re-opens the fallback onto whatever survived in state.
        assert "formatted_output" in out
        assert out["formatted_output"], "falsy formatted_output re-opens the `or result` fallback"

        shipped = _flatten(out["formatted_output"])
        assert _COMPANY_CODE not in shipped, "company code shipped in the error envelope"
        assert _RECORD_REF not in shipped, "record ref shipped in the error envelope"
        assert "speeda://" not in shipped
        assert _COMPANY_NAME not in shipped
        assert _SUMMARY not in shipped
        assert "Researched SPEEDA company" not in shipped

    def test_error_path_clears_output_bearing_state(self):
        """Omitting a field from one envelope is not clearing it from state."""
        out = PostProcessNode().execute(_errored_state())
        for field in (
            "result",
            "confirmation",
            "summary",
            "company_name",
            "speeda_payload",
            "record_id",
            "record_ref",
            "company_id",
            "intent",
        ):
            assert field in out, f"{field} not cleared on the error path"
            assert not out[field], f"{field} still carries content on the error path"

    def test_success_path_still_returns_the_answer(self):
        """Control: containment must not empty out the clean path."""
        out = PostProcessNode().execute(
            {
                "status": AgentStatus.SUCCESS.value,
                "record_id": _COMPANY_CODE,
                "record_ref": _RECORD_REF,
                "company_name": _COMPANY_NAME,
                "summary": "Precision components maker",
                "intent": "lookup_company",
                "confirmation": f"Researched SPEEDA company - ref={_RECORD_REF}",
                "speeda_payload": to_json({"company_code": _COMPANY_CODE, "detail": "profile", "filters": []}),
            }
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        shipped = _flatten(out["formatted_output"])
        assert _COMPANY_CODE in shipped, "the success path must still return the record evidence"
        assert _RECORD_REF in shipped


class TestErrorLogNamesNoRecord:
    """The diagnostics the error envelope carries are a channel of their own.

    `formatted_output["error"]` is the summarised `error_log`, so an upstream
    message that interpolates the company code puts the same evidence back in
    the envelope by another key. Reasons must be closed-set labels, never the
    record value.
    """

    def _state(self, **overrides) -> dict:
        state = {
            "speeda_payload": to_json({"company_code": _COMPANY_CODE, "detail": "profile", "filters": []}),
            "intent": "lookup_company",
            "company_id": _COMPANY_CODE,
            "correlation_id": "pb-error-envelope",
            "session_id": "pb-s1",
            "thread_id": "pb-th1",
            "trace_id": "pb-t1",
            "node_history": [],
            "error_log": [],
            "execution_time": {},
        }
        state.update(overrides)
        return state

    def test_company_not_found_reason_does_not_name_the_code(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_speeda_api_node.emit_trace_event", lambda *a, **k: None)

        class _EmptyLookupClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_company(self, company_code, api_token):
                return {"company": {}}

        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _EmptyLookupClient)
        out = CallSpeedaApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert _COMPANY_CODE not in joined, "the not-found reason names the company code"
        # The reason still has to be actionable: it names the record TYPE
        # (a closed-set label), not the record.
        assert "company" in joined

    def test_financials_not_found_reason_does_not_name_the_code(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_speeda_api_node.emit_trace_event", lambda *a, **k: None)

        class _EmptyFinancialsClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_financials(self, company_code, api_token):
                return {"financials": []}

        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _EmptyFinancialsClient)
        out = CallSpeedaApiNode().execute(self._state(intent="summarize_financials"))
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert _COMPANY_CODE not in joined, "the not-found reason names the company code"
        assert "financial" in joined

    def test_upstream_api_failure_reason_carries_no_upstream_body(self, monkeypatch):
        """Control on the already-contained channel: a live tenant's error body is
        unbounded third-party text, and only the HTTP status belongs in a
        caller-facing reason. Asserted so a future edit cannot re-open it."""
        monkeypatch.setattr("src.nodes.call_speeda_api_node.emit_trace_event", lambda *a, **k: None)
        from src.services.speeda_client import SpeedaApiError

        class _ApiErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_company(self, company_code, api_token):
                raise SpeedaApiError(403, f"denied for {_COMPANY_NAME} ({_COMPANY_CODE})")

        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _ApiErrorClient)
        out = CallSpeedaApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "403" in joined, "the HTTP status is the actionable signal - keep it"
        assert "denied for" not in joined
        assert _COMPANY_NAME not in joined
        assert _COMPANY_CODE not in joined

    def test_transport_failure_reason_carries_only_the_exception_type(self, monkeypatch):
        """Control on the already-contained channel: a transport error string can
        carry the request URL and the company code."""
        monkeypatch.setattr("src.nodes.call_speeda_api_node.emit_trace_event", lambda *a, **k: None)

        class _TransportErrorClient:
            uses_stub_transport = True

            def __init__(self, *args, **kwargs):
                pass

            def get_company(self, company_code, api_token):
                raise ConnectionError(f"GET https://api.speeda.com/companies/{_COMPANY_CODE} failed")

        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _TransportErrorClient)
        out = CallSpeedaApiNode().execute(self._state())
        assert out["status"] == AgentStatus.ERROR.value
        joined = " ".join(out["error_log"])
        assert "ConnectionError" in joined, "the exception TYPE is the actionable signal - keep it"
        assert "api.speeda.com" not in joined
        assert _COMPANY_CODE not in joined


# ---------------------------------------------------------------------------
# molt source review, 2026-09-04 (family-level NEEDS-FIX, issue #14): the
# caller-visible error must be CLOSED-SET labels only.
#
# The envelope above was record-free and every output-bearing field was cleared
# - and it still published `error` = the summarised `error_log`. Clearing the
# answer is a different property from bounding the error channel: a first line,
# capped at 200 characters, is still an arbitrary node-authored string and can
# embed an upstream SPEEDA response body, a person's name, an identifier.
#
# The contract these tests hold: `formatted_output` on ANY non-success path is
# `{"reason": <one of ERROR_REASONS>}` - truthy, and nothing else.
# ---------------------------------------------------------------------------

# Recognisable upstream text of exactly the kind error_log can carry. Not
# credential-shaped: a credential-shaped literal would be caught by the
# repo-wide credential gate, and the framework's own S-3 scan raises on one
# inside a node result, which would mask what this sentinel is for. The
# credential-shaped case has its own test below, assembled at runtime.
_SENTINEL = "boom: upstream said {'customer': 'A. Tanaka', 'ref': 'ZZQ-SENTINEL-4d21'}"


def _all_strings(value):
    """Render every string in a mapping - nested KEYS as well as values."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)
    else:
        yield value if isinstance(value, str) else str(value)


def _blocked_state(**overrides) -> dict:
    """A SUCCESS state that the output gate will refuse."""
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "",
        "record_ref": "",
        "company_name": _COMPANY_NAME,
        "summary": _SUMMARY,
        "intent": "lookup_company",
        "confirmation": "done",
        "speeda_payload": to_json({"company_code": _COMPANY_CODE, "detail": "profile"}),
        "error_log": [_SENTINEL],
        "result": {"record_id": _COMPANY_CODE, "summary": _SUMMARY},
    }
    state.update(overrides)
    return state


class TestErrorEnvelopeIsClosedSet:
    """Every value in the caller-visible error comes from the module's constants."""

    def _paths(self) -> dict:
        """Every non-success path through execute(), by name."""
        return {
            "inner_workflow_error": _errored_state() | {"error_log": [_SENTINEL]},
            "gate_missing_record_evidence": _blocked_state(),
            "gate_credential_shaped_value": _blocked_state(
                record_id=_COMPANY_CODE,
                record_ref=_RECORD_REF,
                summary="Bearer " + "a" * 24,
            ),
            "gate_personal_identifier": _blocked_state(
                record_id=_COMPANY_CODE,
                record_ref=_RECORD_REF,
                summary="SSN 123-45-6789 on file",
            ),
        }

    @pytest.mark.parametrize(
        "path",
        [
            "inner_workflow_error",
            "gate_missing_record_evidence",
            "gate_credential_shaped_value",
            "gate_personal_identifier",
        ],
    )
    def test_envelope_carries_only_declared_reason_codes(self, path):
        out = PostProcessNode().execute(self._paths()[path])
        envelope = out["formatted_output"]

        assert out["status"] == AgentStatus.ERROR.value
        # Truthy: a falsy formatted_output re-opens the `or result` fallback.
        assert envelope, "falsy formatted_output re-opens the `or result` fallback"
        assert set(envelope) == {"reason"}, f"{path}: envelope grew a key beyond the reason code"
        assert envelope["reason"] in ERROR_REASONS, f"{path}: reason is not a declared constant"

    @pytest.mark.parametrize(
        "path",
        [
            "inner_workflow_error",
            "gate_missing_record_evidence",
            "gate_credential_shaped_value",
            "gate_personal_identifier",
        ],
    )
    def test_error_log_sentinel_appears_nowhere_in_the_returned_mapping(self, path):
        """The whole node result, walked - nested keys and values alike."""
        out = PostProcessNode().execute(self._paths()[path])
        shipped = [s for s in _all_strings(out["formatted_output"])]
        assert _SENTINEL not in " ".join(shipped)
        for fragment in ("A. Tanaka", "ZZQ-SENTINEL-4d21", "upstream said"):
            assert fragment not in " ".join(shipped), f"{path}: {fragment} reached the caller envelope"

    def test_gate_violations_stay_on_the_internal_channel(self):
        """Withholding them from the caller must not delete them from the audit
        trail: an unexplained refusal is not the goal."""
        out = PostProcessNode().execute(_blocked_state())
        assert any("record_id/record_ref" in entry for entry in out["error_log"])


class TestViolationLabelWithholdsCredentialShapedKeys:
    """A credential-shaped mapping KEY must not be quoted by the entry reporting it.

    The label rides `error_log`, and FunctionNode._security_gate_output scans
    EVERY string in a node result: a credential pattern there raises, __call__
    turns that into a bare error with no `formatted_output` key at all, and the
    `formatted_output or result` fallback re-opens onto whatever survived. The
    label is what the fix withholds; the gate itself is never weakened.
    """

    def test_credential_shaped_key_is_withheld_from_the_label(self):
        import src.nodes.post_process_node as ppn

        # Assembled at runtime - no credential-shaped literal in the tree.
        leaky_key = "sk-" + "c" * 24
        violations = ppn._scan_patterns({"record_id": "c-7203", leaky_key: "clean"})
        assert violations, "a credential-shaped KEY must be a finding at all"
        joined = " ".join(violations)
        assert leaky_key not in joined, "the label quoted the credential-shaped key it reports"
        assert WITHHELD in joined

    def test_an_ordinary_key_is_still_named(self):
        """Control: withholding must not blind the label - a refusal that names
        no path is not actionable."""
        import src.nodes.post_process_node as ppn

        violations = ppn._scan_patterns({"summary": "Bearer " + "a" * 24})
        assert violations and "['summary']" in violations[0]

    def test_the_framework_detector_is_the_floor_not_a_replacement(self):
        """The local pattern does not know AKIA; the framework's does. Keeping
        both is what makes the union wider than either."""
        import src.nodes.post_process_node as ppn

        aws_like = "AKIA" + "Q7XWVB3M2NDKLPZR"
        assert ppn._CREDENTIAL_LIKE_RE.search(aws_like) is None, "probe is not a local-pattern match"
        assert ppn._scan_patterns({"summary": aws_like}), "framework detector floor is not applied"


class TestRefusalNoticesDoNotEchoCallerKeys:
    """pre_process names a rejected context field; the name is caller text.

    IDENTIFIER_RE accepts up to twenty characters of [A-Za-z0-9_-], which is
    exactly the shape of an AWS access key id - so "inert" was not enough.
    """

    def test_credential_shaped_field_name_is_withheld(self):
        aws_like = "AKIA" + "Q7XWVB3M2NDKLPZR"
        assert len(aws_like) == 20, "probe must be inside the identifier alphabet to be a real case"
        masked = mask_field_name(aws_like)
        assert aws_like not in masked
        assert WITHHELD in masked

    def test_ordinary_field_name_still_passes_through(self):
        """Control: the mask must not describe every name by shape - the caller
        has to be told which field was wrong."""
        assert mask_field_name("company_id") == "company_id"
