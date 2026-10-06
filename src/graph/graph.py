"""AgentCore Platform v1.0 - CMN-C2-287 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in SpeedaWorkflowGraphNode (`main`
slot), which wraps the inner SpeedaWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.

Configuration lives in TWO files with different jobs:
  config/agent.yaml   - the static manifest the registry reads (identity, entry
                        point, trust level, compile-time requirements). It is a
                        FLAT document: there is no `agent:` block and no runtime
                        values in it.
  config/config.yaml  - the runtime parameters (max_retry, timeout_s, and the
                        `speeda:` integration section). This is the file the code
                        below reads; reading the manifest for runtime values
                        returns nothing and degrades every declared setting to a
                        default without failing.
"""

from pathlib import Path
from typing import Any, ClassVar

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode, _REASON_WORKFLOW_FAILED
from src.schemas.state import State

# Repo-root runtime config: src/graph/graph.py -> parents[2] = repo root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def load_runtime_config() -> dict[str, Any]:
    """Read config/config.yaml, or an empty mapping if it is absent/unreadable.

    An absent file is a legitimate deployment (every value has a documented
    default); a malformed one is not silently different from an absent one -
    the values that ARE present are validated where they are consumed, and an
    invalid one raises there rather than degrading.
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


class SpeedaWorkflowGraphNode(GraphNode):
    """Wraps the inner SPEEDA workflow graph; assigned to the `main` slot.

    No constructor arguments (SDK v1 nodes are no-arg) - configuration reaches
    the subgraph via _parent_config(), which loads the runtime config file.
    """

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def get_subgraph(self) -> Any:
        from src.graph.domain_workflow_graph import SpeedaWorkflowGraph

        return SpeedaWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string);
        # structured params travel as JSON and the first inner node parses them
        # back. This is the caller-data bridge for the nested graph: the SDK's
        # GraphNode invokes the subgraph without forwarding input_context, so a
        # validated caller value that is not carried in this envelope never
        # reaches an inner node.
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # Map only the keys this node changes back into the outer state.
        return {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "company_id": sub_result.get("company_id", ""),
            "record_id": sub_result.get("record_id", ""),
            "record_ref": sub_result.get("record_ref", ""),
            "company_name": sub_result.get("company_name", ""),
            "summary": sub_result.get("summary", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "speeda_payload": sub_result.get("speeda_payload", ""),
            "redaction_flags": sub_result.get("redaction_flags", ""),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Forward the runtime config to the inner graph under config["configurable"].

        The whole runtime document is forwarded, so a value added to
        config/config.yaml reaches the inner graph without another change here.
        """
        return {"configurable": load_runtime_config()}


class SpeedaCompanyResearchAgent(AgentBaseGraph):
    """CMN-C2-287 outer graph - SPEEDA Company Research Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in SpeedaWorkflowGraphNode (`main` slot); SPEEDA
    settings flow from config/config.yaml via _parent_config().
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The backbone reads max_retry from self.config, so a graph built with
        # no config runs on the framework default and the declared value is
        # dead. Default to the runtime config file instead of {}.
        super().__init__(config if config is not None else load_runtime_config())

    @property
    def name(self) -> str:
        return "cmn_c2_287"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = SpeedaWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
    # The one conditional edge in this agent is the framework's own
    # add_conditional_edges("main", self.route); its path callable is
    # AgentBaseGraph.route, which carries no parameter annotation and therefore
    # receives the whole state. No domain path callable is registered here - see
    # SpeedaWorkflowGraph.route for the inner graph's annotation.
    #
    # Routing ERROR to post_process would NOT reach PostProcessNode.execute():
    # FunctionNode.__call__ (see framework.nodes.base_node) skips execute()
    # outright whenever the INCOMING state.status is already ERROR - a
    # blanket short-circuit that applies to every FunctionNode regardless of
    # which node routing sends it to. Confirmed by direct trace: routing
    # main's ERROR to post_process still produced output=None: post_process
    # ran (visible in node_history) but its execute() was never entered
    # (PostProcessNode._contain(), see post_process_node.py, never fired).
    # So the caller-facing envelope has to be synthesized in get_output()
    # instead - see the override below.
    def get_output(self, state) -> dict:
        """Supply the closed-set error reason on the path post_process can
        never reach.

        AgentBaseGraph.get_output() projects formatted_output/result with no
        fallback. On an upstream (main) failure, post_process's execute()
        never runs (see the note on add_edges() above), so formatted_output
        stays unset and the caller gets output=None with zero explanation -
        even though PostProcessNode already defines the correct closed-set
        answer for exactly this case (_REASON_WORKFLOW_FAILED). Reuse that
        same constant here rather than a second literal, so the two stay in
        sync. Only fires when post_process did NOT already supply an
        envelope (the output-gate-violation containment path, which DOES
        reach execute() because state.status is still SUCCESS when
        post_process's __call__ begins, is untouched).
        """
        output = super().get_output(state)
        if state.get("status") == AgentStatus.ERROR.value and not output.get("output"):
            output["output"] = {"reason": _REASON_WORKFLOW_FAILED}
        return output
