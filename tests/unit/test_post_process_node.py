# CMN-C2-287 - Unit tests: PostProcessNode (outer backbone, domain output gate).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input mask
# -> execute() -> output scan); this backbone formatter declares ANONYMOUS -> the
# state builder sets caller_trust_level = TrustLevel.ANONYMOUS.value. The domain
# gate is the MODULE-LEVEL _security_gate_output() helper (the framework gate
# methods are final and the SDK auto-wraps the _extra_ hooks), so the helper is
# also unit-tested directly as a plain function.
#
# The precision grid inside the gate has its own module,
# tests/unit/test_output_precision_gate.py.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode, SCHEMA_NOTE, _security_gate_output
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "status": AgentStatus.SUCCESS.value,
        "record_id": "c-7203",
        "record_ref": "speeda://companies/c-7203",
        "company_name": "company c-7203",
        "summary": "synthetic company profile for c-7203",
        "intent": "lookup_company",
        "confirmation": "Retrieved company profile 'company c-7203' - ref=speeda://companies/c-7203 - id=c-7203",
        "speeda_payload": to_json({"company_code": "c-7203", "detail": "profile"}),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "post-process-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["record_id"] == "c-7203"
        assert out["record_ref"] == "speeda://companies/c-7203"
        assert out["intent"] == "lookup_company"
        assert out["summary"] == "synthetic company profile for c-7203"
        assert out["confirmation"].startswith("Retrieved company profile")
        assert out["schema_note"] == SCHEMA_NOTE

    def test_request_is_described_not_echoed(self):
        """Filter values are caller text; reflecting them makes the response a
        channel the caller writes. Only the request's shape is reported."""
        state = _state(
            speeda_payload=to_json(
                {
                    "company_code": "c-7203",
                    "detail": "profile",
                    "filters": [{"name": "region", "values": ["north america"]}],
                }
            )
        )
        out = self.node(state)["formatted_output"]
        assert out["speeda_request"] == {
            "company_code": "c-7203",
            "detail": "profile",
            "filter_count": 1,
        }
        assert "north america" not in str(out)

    def test_monetary_figures_are_reported_on_the_grid(self):
        state = _state(
            intent="summarize_financials",
            summary="fy2025 highlights (jpy): revenue 12345678, operating income 1234567",
        )
        out = self.node(state)["formatted_output"]
        assert out["summary"] == ("fy2025 highlights (jpy): revenue 12,346,000, operating income 1,235,000")
        # The record identifiers around them are untouched.
        assert out["record_ref"] == "speeda://companies/c-7203"

    def test_error_status_preserved(self):
        """Inner-workflow error must not be masked as success. Real-SDK
        pipeline behavior: BaseNode.__call__ short-circuits on an incoming
        errored state (execute() is skipped), so the error status + error_log
        pass through untouched and no success shape is fabricated."""
        state = _state(
            status=AgentStatus.ERROR.value,
            record_id="",
            record_ref="",
            error_log=["CallSpeedaApiNode: SPEEDA API error 403"],
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "SPEEDA API error 403" in "\n".join(result["error_log"])
        assert "formatted_output" not in result

    def test_error_envelope_publishes_the_reason_code_and_nothing_else(self):
        """execute() directly: error_log is the INTERNAL channel.

        Dropping the traceback and keeping the first line was the old contract,
        and a first line is not a closed set - it can carry an upstream response
        body, a name, an identifier. The envelope now says only WHAT happened.
        """
        noisy = 'boom\nTraceback (most recent call last):\n  File "/srv/app/src/x.py", line 1'
        result = self.node.execute(_state(status=AgentStatus.ERROR.value, error_log=[noisy]))
        assert result["formatted_output"] == {"reason": "speeda_workflow_failed"}
        rendered = str(result["formatted_output"])
        assert "boom" not in rendered
        assert "Traceback" not in rendered
        assert "/srv/app" not in rendered

    def test_inner_error_entries_are_not_re_emitted(self):
        """The state reducer APPENDS to error_log, so echoing the inner entries
        back out of this node would duplicate every line in the audit trail."""
        result = self.node.execute(
            _state(status=AgentStatus.ERROR.value, error_log=["CallSpeedaApiNode: SPEEDA API error 500"])
        )
        assert "error_log" not in result

    def test_gate_blocks_success_without_record_evidence(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("record_id/record_ref" in entry for entry in result["error_log"])

    def test_violation_clears_every_output_bearing_field(self):
        """Returning an error is not containment.

        The graph's get_output() falls back to state["result"] when
        formatted_output is absent, so a gate that merely reports a violation
        still ships the un-gated inner answer inside the error envelope.
        """
        result = self.node(_state(record_id="", record_ref=""))
        assert result["result"] is None
        assert result["confirmation"] == ""
        assert result["summary"] == ""
        assert result["company_name"] == ""
        assert result["speeda_payload"] is None
        # The record evidence is cleared too - a caller told the operation
        # failed must not learn which company record was resolved.
        assert result["record_id"] == ""
        assert result["record_ref"] == ""
        assert result["company_id"] == ""
        assert result["intent"] == ""
        # formatted_output is truthy but carries nothing that was released: a
        # closed-set reason code and NOTHING else - not the block reasons, which
        # are node-authored strings. Truthy on purpose - a falsy value re-opens
        # the `formatted_output or result` projection this containment exists to
        # prevent.
        assert set(result["formatted_output"]) == {"reason"}
        assert result["formatted_output"]["reason"] == "output_withheld_by_gate"
        assert "speeda://" not in str(result["formatted_output"])
        # The violations themselves stay on the internal channel.
        assert any("record_id/record_ref" in entry for entry in result["error_log"])


class TestSecurityGateOutputHelper:
    """The module-level domain gate as a plain function (not a node call)."""

    def test_passes_success_with_record_evidence(self):
        gated, violations, snaps = _security_gate_output(
            {"record_id": "c-7203", "record_ref": "speeda://companies/c-7203", "confirmation": "ok"},
            is_success=True,
        )
        assert violations == []
        assert snaps == 0
        assert gated["confirmation"] == "ok"

    def test_blocks_success_without_record_evidence(self):
        _, violations, _ = _security_gate_output(
            {"record_id": "", "record_ref": "", "confirmation": "looks done"},
            is_success=True,
        )
        assert len(violations) == 1
        assert "record_id/record_ref" in violations[0]

    def test_blocks_credential_shaped_value(self):
        # Built at runtime so no credential-shaped literal is committed.
        bearer_like = "Bearer " + "a" * 24
        _, violations, _ = _security_gate_output({"record_id": "c-7203", "note": bearer_like}, is_success=True)
        assert any("note" in v for v in violations)

    def test_blocks_nested_credential_shaped_value(self):
        bearer_like = "Bearer " + "a" * 24
        _, violations, _ = _security_gate_output(
            {"record_id": "c-7203", "speeda_request": {"company_code": bearer_like}},
            is_success=True,
        )
        assert any("speeda_request" in v for v in violations)

    @pytest.mark.parametrize("text", ["SSN 123-45-6789", "TAX 987-65-4321"])
    def test_pattern_scan_runs_before_the_grid(self, text):
        """The grid rewrites digit runs, which can destroy the shape a pattern
        scan matches on - so the scan must see the un-snapped text first, and
        the value must be blocked rather than mangled into something no scan
        would recognise."""
        gated, violations, snaps = _security_gate_output({"record_id": "c-7203", "summary": text}, is_success=True)
        assert any("personal-identifier" in v for v in violations)
        assert snaps == 0
        assert gated["summary"] == text

    def test_error_output_not_required_to_carry_evidence(self):
        _, violations, _ = _security_gate_output({"record_id": "", "record_ref": ""}, is_success=False)
        assert violations == []
