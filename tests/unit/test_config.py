# CMN-C2-287 - Unit tests: config/agent.yaml (manifest) + config/config.yaml (runtime).
#
# The manifest is FLAT: the registry reads every key at root level, so an
# `agent:` block would make all of it invisible. Runtime values live in the
# separate config/config.yaml and are read by src/graph/graph.py.

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_ROOT = pathlib.Path(__file__).parents[2]
_MANIFEST_PATH = _ROOT / "config" / "agent.yaml"
_RUNTIME_PATH = _ROOT / "config" / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _manifest():
    return yaml.safe_load(_MANIFEST_PATH.read_text())


def _runtime():
    return yaml.safe_load(_RUNTIME_PATH.read_text())


def test_manifest_is_flat():
    """No nested `agent:` block - the registry reads root-level keys only."""
    data = _manifest()
    assert "agent" not in data
    assert data["id"] == "CMN-C2-287"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["namespace"] == "cmn"
    assert data["enabled"] is True


def test_manifest_entry_point_is_a_single_dotted_path():
    assert _manifest()["class"] == "src.graph.graph.SpeedaCompanyResearchAgent"


def test_manifest_declares_external_entry_trust():
    # Agent-level entry trust, enforced by the outer backbone pre_process gate;
    # inner domain nodes stay ANONYMOUS.
    assert _manifest()["required_trust_level"] == "VERIFIED_EXTERNAL"


def test_manifest_declares_no_compile_time_requirements():
    """requires.secrets lists keys the agent calls ctx.secrets.require() on.

    This template calls ctx.secrets.get() and degrades to the network-free
    transport, so the integration token is an operational prerequisite for a
    live transport, not a compile-time requirement. Declaring an
    unprovisioned key here makes the agent fail at compile time.
    """
    requires = _manifest()["requires"]
    assert requires["secrets"] == []
    assert requires["extras"] == []


def test_manifest_declares_deterministic_generation():
    assert _manifest()["generation_mode"] == "deterministic"


def test_runtime_config_holds_the_integration_section():
    # Forwarded to the inner graph by SpeedaWorkflowGraphNode._parent_config().
    assert _runtime()["speeda"]["base_url"] == "https://api.speeda.com/v1"


def test_runtime_config_call_budget():
    runtime = _runtime()
    assert isinstance(runtime["max_retry"], int)
    assert isinstance(runtime["timeout_s"], (int, float))
    assert "timeout_seconds" not in runtime
