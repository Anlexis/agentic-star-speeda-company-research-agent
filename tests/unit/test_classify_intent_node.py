# CMN-C2-287 - Unit tests: ClassifyIntentNode (inner Step 2)
# Intents: lookup_company / lookup_industry / summarize_financials
# (deterministic keyword heuristic, v1 - no LLM; every intent is READ-ONLY -
# the research domain has no mutation - and unknown falls back to the
# read-only lookup_company default).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input
# mask -> execute -> output scan); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.classify_intent_node import ClassifyIntentNode


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.classify_intent_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str) -> dict:
    return {
        "validated_input": text,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "classify-intent-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }


class TestClassifyIntentNode:
    def setup_method(self):
        self.node = ClassifyIntentNode()

    def test_keyword_lookup_company(self):
        result = self.node(_state("look up the company profile for company code c-7203."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_company"

    def test_keyword_lookup_industry(self):
        result = self.node(_state("which industry does company code c-7203 belong to"))
        assert result["intent"] == "lookup_industry"

    def test_keyword_summarize_financials(self):
        result = self.node(_state("summarize the financial highlights for company code c-7203"))
        assert result["intent"] == "summarize_financials"

    def test_most_specific_keyword_wins_over_lookup(self):
        # Priority order is most-specific-first: a "summarize the financials of
        # the company" style request classifies as the financial-highlights
        # summary, never the generic profile lookup.
        result = self.node(_state("summarize the financials of the company profile for company code c-7203"))
        assert result["intent"] == "summarize_financials"

    def test_no_signal_defaults_to_readonly_lookup(self):
        result = self.node(_state("please handle this for the team"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "lookup_company"
        # Non-fatal low-confidence note travels in error_log; status stays SUCCESS.
        assert any("defaulted to lookup_company" in entry for entry in result.get("error_log", []))

    def test_empty_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_audit_emits_intent_label_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.classify_intent_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state("look up the company profile for company code c-7203."))
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - the label, never the text.
        assert payloads["classify_intent_complete"]["intent"] == "lookup_company"
        assert payloads["classify_intent_complete"]["defaulted"] is False
