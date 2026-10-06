# CMN-C2-287 - Unit tests: inner SpeedaWorkflowGraph (BaseGraph) contract.
#
# The compiled outer path is exercised end-to-end by
# tests/proof_of_boundary/test_pb_invoke_order.py; this module unit-checks the
# inner graph's identity, runtime-config validation and forwarding, routing,
# output contract, and a direct inner invoke on the network-free transport.

import pytest

from framework.errors import ConfigError

from langgraph.graph import END

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import SpeedaWorkflowGraph
from src.schemas.state import State, from_json


def _graph(config=None):
    return SpeedaWorkflowGraph(config=config or {})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "speeda_company_research_workflow"
    assert g.state_schema is State


def test_extra_initial_state_injects_settings_as_json():
    g = _graph(
        {"configurable": {"speeda": {"base_url": "https://speeda.example.test/v1"}, "max_retry": 2, "timeout_s": 12}}
    )
    extra = g._extra_initial_state()
    # State values are JSON strings, not native dicts (msgpack-safe).
    assert isinstance(extra["speeda_config"], str)
    assert from_json(extra["speeda_config"], {}) == {
        "base_url": "https://speeda.example.test/v1",
        "max_retry": 2,
        "timeout_s": 12.0,
    }


def test_extra_initial_state_carries_documented_defaults_without_a_speeda_section():
    settings = from_json(_graph()._extra_initial_state()["speeda_config"], {})
    assert settings == {"max_retry": 3, "timeout_s": 30.0}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), -1, 99, True, "three"])
def test_invalid_max_retry_fails_the_build(value):
    """A declared number that is not finite and in range must not degrade
    quietly: NaN parses through float() and then compares False against every
    bound, disabling the budget it configures."""
    with pytest.raises(ConfigError):
        _graph({"configurable": {"max_retry": value}}).compile()


@pytest.mark.parametrize("value", ["NaN", "Infinity", float("nan"), 0, 1e9, True, "soon"])
def test_invalid_timeout_fails_the_build(value):
    with pytest.raises(ConfigError):
        _graph({"configurable": {"timeout_s": value}}).compile()


def test_route_error_ends_graph():
    g = _graph()
    assert g.route({"status": AgentStatus.ERROR.value}) == END
    assert g.route({"status": AgentStatus.SUCCESS.value}) == "confirm"


def test_route_is_annotated_with_this_graphs_state():
    """A path callable's annotation is the schema the state is projected
    through, so a domain router annotated with the framework base state would
    see every domain field as absent while unit tests kept passing."""
    import typing

    hints = typing.get_type_hints(SpeedaWorkflowGraph.route)
    assert hints["state"] is State


def test_get_output_surfaces_record_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"record_id": "c-7203", "record_ref": "speeda://companies/c-7203", "confirmation": "ok"},
            "status": AgentStatus.SUCCESS.value,
            "intent": "lookup_company",
            "company_id": "c-7203",
            "record_id": "c-7203",
            "record_ref": "speeda://companies/c-7203",
            "company_name": "company c-7203",
            "summary": "synthetic company profile for c-7203",
            "confirmation": "ok",
            "speeda_payload": "{}",
            "redaction_flags": "[]",
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == AgentStatus.SUCCESS.value
    assert out["intent"] == "lookup_company"
    assert out["record_ref"] == "speeda://companies/c-7203"
    assert out["company_name"] == "company c-7203"
    assert out["summary"] == "synthetic company profile for c-7203"
    assert out["confirmation"] == "ok"
    assert out["output"] == {"record_id": "c-7203", "record_ref": "speeda://companies/c-7203", "confirmation": "ok"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == AgentStatus.ERROR.value
    assert out["error_log"] == ["boom"]


def test_inner_graph_compiles():
    g = _graph()
    g.compile()
    assert g._compiled is not None


def test_inner_invoke_lookup_on_the_default_transport():
    """Direct inner invoke (default ANONYMOUS ctx - every inner node is
    ANONYMOUS): validate -> classify -> infer -> call(stub) -> confirm."""
    g = _graph({"configurable": {"speeda": {"base_url": "https://api.speeda.com/v1"}}})
    g.compile()
    result = g.invoke(user_input="look up the company profile for company code c-7203 and show the record on file.")
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["record_id"] == "c-7203"
    assert result["record_ref"] == "speeda://companies/c-7203"
    assert result["intent"] == "lookup_company"
    assert result["confirmation"]
    assert result["summary"]
    history = result.get("node_history", [])
    assert history == [
        "ValidateInputNode",
        "ClassifyIntentNode",
        "InferSpeedaFieldsNode",
        "CallSpeedaApiNode",
        "ConfirmNode",
    ]
