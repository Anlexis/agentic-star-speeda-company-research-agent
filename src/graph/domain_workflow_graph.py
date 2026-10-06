"""AgentCore Platform v1.0 - inner SPEEDA workflow graph (Cat 2 domain workflow).

Instantiated by SpeedaWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_speeda_fields
          -> call_speeda_api -> confirm -> END

Config arrives from the outer graph via _parent_config() under
config["configurable"] and is the parsed contents of config/config.yaml:

    speeda      - integration section (base_url, ...)
    max_retry   - additional attempts for a failed lookup (0 disables retries)
    timeout_s   - wall-clock budget for the whole lookup, retries included

Those three are merged into a single JSON `speeda_config` State field by
_extra_initial_state(), which is how the no-arg CallSpeedaApiNode reads them.
Nodes are registered WITHOUT constructor arguments (SDK v1 nodes are no-arg;
ctor args raise TypeError at graph build).
"""

from typing import Any

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_speeda_fields_node import InferSpeedaFieldsNode
from src.nodes.call_speeda_api_node import CallSpeedaApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State, to_json
from src.services.security import ContextValidationError, finite_in_range

# Documented defaults, used when the runtime config declares nothing.
DEFAULT_MAX_RETRY = 3
DEFAULT_TIMEOUT_S = 30.0

# Bounds for the declared values. A configured number is validated at compile
# time and REJECTED when it is out of range or non-finite: NaN parses happily
# through float() and then compares False against every bound, which would
# disable the budget it is supposed to impose.
MAX_RETRY_RANGE = (0.0, 10.0)
TIMEOUT_S_RANGE = (0.1, 600.0)


class SpeedaWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> call -> confirm."""

    @property
    def name(self) -> str:
        return "speeda_company_research_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        """Reject a declared runtime value that is not finite and in range.

        The `speeda` section stays optional (the client falls back to the
        documented default base_url), and an unusable base_url is handled at
        CallSpeedaApiNode.execute() as a graceful status=error rather than a
        compile-time crash. A declared NUMBER is different: it cannot be
        allowed to degrade quietly, so it fails the build.
        """
        self._call_budget()

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern). All nodes are no-arg.
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode()
        self._nodes["infer_speeda_fields"] = InferSpeedaFieldsNode()
        self._nodes["call_speeda_api"] = CallSpeedaApiNode()
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_speeda_fields")
        self._sg.add_edge("infer_speeda_fields", "call_speeda_api")
        self._sg.add_edge("call_speeda_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: State) -> str:
        """Required by the BaseGraph contract; this topology is linear.

        Annotated with THIS graph's State on purpose. A path callable's
        annotation is the input schema the graph engine projects the state
        through, so annotating a domain router with the framework's base state
        would hide every domain field from it - the router would then branch on
        values that are always absent while unit tests, which call it with a
        plain dict, keep passing.
        """
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def _configurable(self) -> dict[str, Any]:
        configurable = self.config.get("configurable") or {}
        return configurable if isinstance(configurable, dict) else {}

    def _call_budget(self) -> "tuple[int, float]":
        """Return the validated (max_retry, timeout_s) pair for the lookup."""
        configurable = self._configurable()
        try:
            max_retry = finite_in_range(
                "config.yaml max_retry",
                configurable.get("max_retry", DEFAULT_MAX_RETRY),
                minimum=MAX_RETRY_RANGE[0],
                maximum=MAX_RETRY_RANGE[1],
            )
            timeout_s = finite_in_range(
                "config.yaml timeout_s",
                configurable.get("timeout_s", DEFAULT_TIMEOUT_S),
                minimum=TIMEOUT_S_RANGE[0],
                maximum=TIMEOUT_S_RANGE[1],
            )
        except ContextValidationError as exc:
            raise ConfigError(f"[{self.__class__.__name__}] {exc}") from None
        return int(max_retry), timeout_s

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the runtime settings the no-arg inner nodes need.

        State values are JSON strings (msgpack-safe), so the settings travel as
        one serialized `speeda_config` field rather than a nested mapping.
        """
        max_retry, timeout_s = self._call_budget()
        settings: dict[str, Any] = dict(self._configurable().get("speeda") or {})
        settings["max_retry"] = max_retry
        settings["timeout_s"] = timeout_s
        return {"speeda_config": to_json(settings)}

    def get_output(self, state: State) -> dict[str, Any]:
        return {
            "output": state.get("result") or state.get("confirmation"),
            "status": state.get("status"),
            "intent": state.get("intent", ""),
            "company_id": state.get("company_id", ""),
            "record_id": state.get("record_id", ""),
            "record_ref": state.get("record_ref", ""),
            "company_name": state.get("company_name", ""),
            "summary": state.get("summary", ""),
            "confirmation": state.get("confirmation", ""),
            "speeda_payload": state.get("speeda_payload", ""),
            "redaction_flags": state.get("redaction_flags", ""),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
