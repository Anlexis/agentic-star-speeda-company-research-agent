# CMN-C2-287 - Unit tests: CallSpeedaApiNode (inner Step 4, tool call).
#
# Canon: invoked via node(state) (BaseNode.__call__ -> trust gate -> input mask
# -> execute() -> output scan); inner domain node -> caller_trust_level =
# TrustLevel.ANONYMOUS.value. The ONE documented exception: the config-override
# call passes a 2nd (config) argument, which __call__ cannot forward - that
# single test stays a DIRECT execute(state, config=...) call.
#
# The node builds its client locally (nodes are no-arg), so error-path
# transports are exercised by monkeypatching the module's SpeedaClient symbol
# (our own module attribute - never a sys.modules stub of shared.*).
#
# READ-ONLY research domain: profile / industry / financial-highlights lookups
# only - the unknown-intent test proves no mutation verb is ever accepted.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_speeda_api_node import CallSpeedaApiNode
from src.services.speeda_client import SpeedaApiError
from src.schemas.state import to_json


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    monkeypatch.setattr("src.nodes.call_speeda_api_node.emit_trace_event", lambda *a, **k: None)


def _state(**overrides) -> dict:
    state = {
        "speeda_payload": to_json({"company_code": "c-7203", "detail": "profile"}),
        "intent": "lookup_company",
        "company_id": "c-7203",
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "correlation_id": "call-speeda-test",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


class _FakeErrorClient:
    """Stands in for SpeedaClient: lookup raises the documented API error."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_company(self, company_code, api_token):
        raise SpeedaApiError(403, "forbidden by subscription permissions")


class _FakeEmptyClient:
    """Stands in for SpeedaClient: well-formed 2xx response with no record."""

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = True

    def get_company(self, company_code, api_token):
        return {"company": {}}


class _FakeLiveClient:
    """Stands in for SpeedaClient with a LIVE (non-stub) transport."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    uses_stub_transport = False

    def get_company(self, company_code, api_token):
        _FakeLiveClient.captured = {"company_code": company_code, "api_token": api_token}
        return {"company": {"code": company_code, "name": f"company {company_code}"}}


class TestCallSpeedaApiNode:
    def setup_method(self):
        self.node = CallSpeedaApiNode()

    def test_profile_lookup_success_via_default_transport(self):
        # Default transport = deterministic and network-free; no secret provider
        # bound -> the node runs on the documented placeholder.
        result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "c-7203"
        assert result["record_ref"] == "speeda://companies/c-7203"
        assert result["company_id"] == "c-7203"
        assert result["company_name"] == "company c-7203"
        assert "synthetic company profile" in result["summary"]

    def test_industry_lookup_success_via_default_transport(self):
        state = _state(
            intent="lookup_industry",
            speeda_payload=to_json({"company_code": "c-7203", "detail": "industry"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "c-7203"
        assert result["record_ref"] == "speeda://companies/c-7203"
        assert result["summary"].startswith("industry classification: ")
        assert "diversified industrials" in result["summary"]

    def test_financials_summary_success_via_default_transport(self):
        state = _state(
            intent="summarize_financials",
            speeda_payload=to_json({"company_code": "c-7203", "detail": "financials"}),
        )
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "c-7203"
        assert result["summary"].startswith("fy2025 highlights (jpy): revenue ")

    def test_speeda_config_state_field_sets_base_url(self):
        # The inner graph seeds the runtime settings as the JSON speeda_config
        # state field; the network-free transport still serves the call.
        state = _state(speeda_config=to_json({"base_url": "https://speeda.example.test/v1"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_ref"] == "speeda://companies/c-7203"

    def test_config_override_direct_execute_call(self):
        # Documented canon exception: execute(state, config=...) takes a 2nd
        # argument that __call__ cannot forward, so this ONE test calls execute
        # directly (ANONYMOUS node - the trust gate is not the subject here).
        config = {"configurable": {"speeda": {"base_url": "https://speeda.example.test/v1"}}}
        result = self.node.execute(_state(), config=config)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_id"] == "c-7203"

    def test_missing_payload_errors(self):
        result = self.node(_state(speeda_payload=None))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_unresolved_code_errors(self):
        state = _state(company_id="", speeda_payload=to_json({"company_code": "", "detail": "profile"}))
        result = self.node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unresolved company code" in entry for entry in result["error_log"])

    def test_unknown_intent_errors(self):
        # READ-ONLY domain: no mutation verb exists - anything outside the
        # three lookup intents is refused, never executed.
        result = self.node(_state(intent="update_company"))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unknown intent" in entry for entry in result["error_log"])

    def test_empty_company_record_errors(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _FakeEmptyClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        # The reason names the record TYPE and never the company code - it ships
        # to the caller inside post_process's error envelope.
        assert any("no matching company record found" in entry for entry in result["error_log"])
        assert not any("c-7203" in entry for entry in result["error_log"])

    def test_api_error_surfaces_status_error(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _FakeErrorClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in entry for entry in result["error_log"])
        # The upstream message body is NOT interpolated: it is third-party text
        # that can echo request data, and an error log is an output surface.
        assert not any("subscription permissions" in entry for entry in result["error_log"])

    def test_transport_exception_message_is_not_interpolated(self, monkeypatch):
        class _Boom:
            uses_stub_transport = True

            def __init__(self, *a, **k):
                pass

            def get_company(self, company_code, api_token):
                raise RuntimeError("connect failed: /srv/app/secrets/speeda.pem")

        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _Boom)
        result = self.node(_state(speeda_config=to_json({"max_retry": 0})))
        assert result["status"] == AgentStatus.ERROR.value
        joined = "\n".join(result["error_log"])
        assert "RuntimeError" in joined
        assert "/srv/app" not in joined


class TestCallBudget:
    """max_retry / timeout_s are declared in config/config.yaml, reach this node
    through the graph, and change what it does."""

    def setup_method(self):
        self.node = CallSpeedaApiNode()

    def _flaky(self, failures, status=503):
        class _Flaky:
            uses_stub_transport = True
            calls = 0

            def __init__(self, *a, **k):
                pass

            def get_company(self, company_code, api_token):
                _Flaky.calls += 1
                if _Flaky.calls <= failures:
                    raise SpeedaApiError(status, "upstream unavailable")
                return {"company": {"code": company_code, "name": f"company {company_code}"}}

        return _Flaky

    def test_transient_failure_is_retried_within_the_declared_count(self, monkeypatch):
        flaky = self._flaky(failures=2)
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", flaky)
        result = self.node(_state(speeda_config=to_json({"max_retry": 3})))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert flaky.calls == 3

    def test_retries_stop_at_the_declared_count(self, monkeypatch):
        flaky = self._flaky(failures=5)
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", flaky)
        result = self.node(_state(speeda_config=to_json({"max_retry": 1})))
        assert result["status"] == AgentStatus.ERROR.value
        assert flaky.calls == 2

    def test_zero_retries_calls_once(self, monkeypatch):
        flaky = self._flaky(failures=5)
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", flaky)
        result = self.node(_state(speeda_config=to_json({"max_retry": 0})))
        assert result["status"] == AgentStatus.ERROR.value
        assert flaky.calls == 1

    def test_caller_error_is_never_retried(self, monkeypatch):
        flaky = self._flaky(failures=5, status=404)
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", flaky)
        result = self.node(_state(speeda_config=to_json({"max_retry": 3})))
        assert result["status"] == AgentStatus.ERROR.value
        assert flaky.calls == 1

    @pytest.mark.parametrize(
        "field,value",
        [
            ("max_retry", "NaN"),
            ("max_retry", float("inf")),
            ("max_retry", -1),
            ("max_retry", 99),
            ("max_retry", True),
            ("max_retry", "three"),
            ("timeout_s", "NaN"),
            ("timeout_s", float("nan")),
            ("timeout_s", 0),
            ("timeout_s", 1e9),
            ("timeout_s", True),
            ("timeout_s", None),
        ],
    )
    def test_non_finite_or_out_of_range_settings_fail_closed(self, field, value):
        result = self.node(_state(speeda_config=to_json({field: value})))
        assert result["status"] == AgentStatus.ERROR.value
        assert field in result["error_log"][0]

    def test_live_transport_without_secret_refuses_call(self, monkeypatch):
        # With a LIVE transport a missing SPEEDA_TOKEN is a hard error - a real
        # API is never called unauthenticated.
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _FakeLiveClient)
        result = self.node(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("unauthenticated" in entry for entry in result["error_log"])

    def test_live_transport_reads_token_from_ctx_secrets(self, monkeypatch):
        monkeypatch.setattr("src.nodes.call_speeda_api_node.SpeedaClient", _FakeLiveClient)
        _FakeLiveClient.captured = {}
        with bound_secrets(InMemoryProvider({"SPEEDA_TOKEN": "mock-token-for-testing"})):
            result = self.node(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert _FakeLiveClient.captured["api_token"] == "mock-token-for-testing"
        assert _FakeLiveClient.captured["company_code"] == "c-7203"

    def test_audit_emits_lookup_signals_only(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "src.nodes.call_speeda_api_node.emit_trace_event",
            lambda *args, **kwargs: events.append(args),
        )
        self.node(_state())
        payloads = {args[0]: args[1] for args in events}
        # Emit-spy asserts on the payload (args[1]) - presence signals only.
        payload = payloads["call_speeda_api_complete"]
        assert payload["intent"] == "lookup_company"
        assert payload["has_record_id"] is True
        assert payload["stub_transport"] is True
