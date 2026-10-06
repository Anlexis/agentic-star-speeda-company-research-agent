# CMN-C2-287 - Unit tests: ConfirmNode (inner Step 5)
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# mask -> execute -> output scan); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.confirm_node import ConfirmNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.confirm_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "record_id": "c-7203",
        "record_ref": "speeda://companies/c-7203",
        "company_name": "company c-7203",
        "summary": "synthetic company profile for c-7203",
        "intent": "lookup_company",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "confirm-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestConfirmNode:
    def setup_method(self):
        self.node = ConfirmNode()

    def test_lookup_confirmation(self):
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Retrieved company profile" in result["confirmation"]
        assert "company c-7203" in result["confirmation"]
        assert "synthetic company profile for c-7203" in result["confirmation"]
        assert "ref=speeda://companies/c-7203" in result["confirmation"]
        assert "id=c-7203" in result["confirmation"]
        assert result["result"]["record_id"] == "c-7203"
        assert result["result"]["record_ref"] == "speeda://companies/c-7203"
        assert result["result"]["summary"] == "synthetic company profile for c-7203"

    def test_industry_verb(self):
        result = self.node(_state(intent="lookup_industry", summary="industry classification: diversified industrials"))
        assert "Retrieved industry classification" in result["confirmation"]

    def test_financials_verb(self):
        result = self.node(_state(intent="summarize_financials", summary="fy2025 highlights (jpy): revenue 1"))
        assert "Summarized financial highlights" in result["confirmation"]

    def test_unknown_intent_uses_generic_verb(self):
        result = self.node(_state(intent="mystery"))
        assert "Processed company research" in result["confirmation"]

    def test_id_only_no_ref(self):
        result = self.node(_state(record_ref=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "id=c-7203" in result["confirmation"]
        assert "ref=" not in result["confirmation"]

    def test_falls_back_to_record_id_when_name_missing(self):
        result = self.node(_state(company_name=""))
        assert "'c-7203'" in result["confirmation"]

    def test_missing_record_evidence_errors(self):
        result = self.node(_state(record_id="", record_ref=""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]
