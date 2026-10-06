# CMN-C2-287 - Unit tests: InferSpeedaFieldsNode (inner Step 3).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input mask
# -> execute -> output scan); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value. Positive payloads carry no personal data: the
# framework mask rewrites Title-Case bigrams in validated_input (even across
# newlines), so quoted display names use a single-word name and Key: value lines
# keep Title-Case words apart.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.infer_speeda_fields_node import InferSpeedaFieldsNode
from src.schemas.state import from_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.infer_speeda_fields_node.emit_trace_event", lambda *a, **k: None)


def _state(text: str, intent: str = "lookup_company", company_hint: str = "", **overrides) -> dict:
    state = {
        "validated_input": text,
        "intent": intent,
        "company_hint": company_hint,
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "infer-fields-test",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class TestInferSpeedaFieldsNode:
    def setup_method(self):
        self.node = InferSpeedaFieldsNode()

    def test_lookup_extracts_code_from_text(self):
        result = self.node(_state("look up the company profile for company code c-7203 and show the record on file."))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["company_id"] == "c-7203"
        # speeda_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["speeda_payload"], str)
        assert from_json(result["speeda_payload"], {}) == {"company_code": "c-7203", "detail": "profile"}

    def test_code_shaped_hint_used_when_text_has_no_code(self):
        result = self.node(_state("show the current company overview", company_hint="a123"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["company_id"] == "a123"
        assert from_json(result["speeda_payload"], {}) == {"company_code": "a123", "detail": "profile"}

    def test_industry_intent_builds_industry_params(self):
        result = self.node(_state("which industry does company code c-7203 belong to", intent="lookup_industry"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["speeda_payload"], {}) == {"company_code": "c-7203", "detail": "industry"}

    def test_financials_intent_builds_financials_params(self):
        result = self.node(
            _state("summarize the financial highlights for company code c-7203", intent="summarize_financials")
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["speeda_payload"], {}) == {"company_code": "c-7203", "detail": "financials"}

    def test_quoted_display_name_extracted(self):
        # Single-word Title-Case name: the framework mask rewrites Title-Case
        # word PAIRS, so "Acme" alone survives to execute().
        result = self.node(_state('look up the company named "Acme"', company_hint="c-9001"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["company_name"] == "Acme"
        assert result["company_id"] == "c-9001"

    def test_filter_fields_parsed_into_params(self):
        # Field values stay lower-case and Title-Case words are never adjacent
        # (the framework name mask rewrites Title-Case pairs even ACROSS
        # newlines before execute() sees the text).
        text = "look up the company profile for company code c-7203\nCountry: jp\nSegment: mobility"
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = from_json(result["speeda_payload"], {})
        assert payload["company_code"] == "c-7203"
        assert {"name": "Country", "values": ["jp"]} in payload["filters"]
        assert {"name": "Segment", "values": ["mobility"]} in payload["filters"]

    def test_code_field_line_resolves_code(self):
        text = "show the company overview\nCode: c-9999"
        result = self.node(_state(text))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["company_id"] == "c-9999"
        # Code/name keys are the identifiers themselves - never research filters.
        assert "filters" not in from_json(result["speeda_payload"], {})

    def test_unresolved_code_left_empty_never_invented(self):
        result = self.node(_state("show the company overview for the flagged record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["company_id"] == ""
        assert from_json(result["speeda_payload"], {}) == {"company_code": "", "detail": "profile"}

    def test_non_code_shaped_hint_left_unresolved(self):
        result = self.node(_state("show the company overview", company_hint="not a valid code!"))
        assert result["company_id"] == ""

    def test_missing_input_errors(self):
        result = self.node(_state(""))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class TestFilterFieldBounds:
    """Filter fields are caller text that ends up in an outbound request, so
    they are capped in number and length and drawn from a printable alphabet.
    A field outside the bounds is DROPPED and counted, never truncated into
    something the caller did not write."""

    def setup_method(self):
        self.node = InferSpeedaFieldsNode()

    def test_over_long_value_is_dropped(self):
        text = "look up company code c-7203\nsegment: " + "x" * 200
        payload = from_json(self.node(_state(text))["speeda_payload"], {})
        assert "filters" not in payload

    def test_value_outside_the_alphabet_is_dropped(self):
        text = "look up company code c-7203\nsegment: <b>mobility</b>"
        payload = from_json(self.node(_state(text))["speeda_payload"], {})
        assert "filters" not in payload

    def test_filter_count_is_capped(self):
        lines = "\n".join(f"field{i}: value{i}" for i in range(20))
        payload = from_json(self.node(_state("look up company code c-7203\n" + lines))["speeda_payload"], {})
        assert len(payload["filters"]) == 10

    def test_dropped_count_is_audited(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.infer_speeda_fields_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        text = "look up company code c-7203\nsegment: <b>mobility</b>\ncountry: jp"
        self.node(_state(text))
        payload = {a[0]: a[1] for a in events}["infer_speeda_fields_complete"]
        assert payload["n_fields"] == 1
        assert payload["n_dropped_fields"] == 1
