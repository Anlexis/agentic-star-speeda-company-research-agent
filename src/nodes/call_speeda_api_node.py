"""AgentCore Platform v1.0 - inner workflow Step 4: CallSpeedaApi (tool call).

Performs the profile / industry / financial-highlights lookup against the
SPEEDA REST API company endpoints via src/services/speeda_client.py. The
domain is READ-ONLY (research) - this node performs GET-shaped lookups only;
no mutation call exists in this template.

Security posture:
  Trust: required_trust_level = ANONYMOUS. The single external trust gate lives
       on the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner
       node. GraphNode.execute() passes the caller's InvocationContext into the
       inner subgraph UNCHANGED (no trust elevation), so a real external caller
       runs this call under its own VERIFIED_EXTERNAL context; declaring
       INTERNAL here would deny that already-gated external caller before the
       call ever runs. The node therefore stays ANONYMOUS.
  Secrets: the integration token is read via ctx.secrets.get("SPEEDA_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in
       state. While the deterministic NETWORK-FREE stub transport is active a
       missing token is tolerated (a sentinel placeholder is used - it is never
       sent anywhere because no request leaves the process); with a LIVE
       transport injected, a missing token is a hard status=error - a real API
       is never called unauthenticated.
  Errors: a failed lookup surfaces as status=error with a message that names the
       failure class and, for an HTTP failure, its status code. Neither the
       exception text nor the response body is interpolated: both are
       third-party content that can carry paths or echoed request data, and an
       error log is an output surface.

Configuration: this node takes NO constructor arguments (SDK v1 nodes are
no-arg). Runtime settings (base_url, retry count, wall-clock budget) arrive as
the JSON `speeda_config` state field - seeded by the inner graph's
_extra_initial_state() from config/config.yaml - or via the optional
`config["configurable"]["speeda"]` argument for direct invocation. The client is
constructed locally per call (no module-global mutation).
"""

import time
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.security import ContextValidationError, finite_in_range
from src.services.speeda_client import SpeedaApiError, SpeedaClient

_SECRET_KEY = "SPEEDA_TOKEN"
# Placeholder handed to the network-free stub transport when no secret is
# provisioned. Never sent over any network (the stub performs no I/O) and never
# written to state or logs.
_STUB_PLACEHOLDER = "stub-transport-no-credential"

# Settings fall back to these when the runtime config declares nothing. Kept in
# step with src/graph/domain_workflow_graph.py.
_DEFAULT_MAX_RETRY = 3
_DEFAULT_TIMEOUT_S = 30.0
_MAX_RETRY_RANGE = (0.0, 10.0)
_TIMEOUT_S_RANGE = (0.1, 600.0)

# HTTP statuses worth another attempt: a transient upstream failure, not a
# caller error. A 4xx is the caller's request being wrong and never retried.
_RETRYABLE_STATUS_MIN = 500


class CallSpeedaApiNode(FunctionNode):
    """Look up a company profile / industry classification / financial highlights via SPEEDA."""

    # The external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so
    # it must stay ANONYMOUS - declaring INTERNAL would deny a real external
    # caller before the call runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any], config: "dict[str, Any] | None" = None) -> dict[str, Any]:
        payload = from_json(state.get("speeda_payload"), None)
        if not payload:
            return _error("missing speeda_payload")

        intent = state.get("intent", "lookup_company") or "lookup_company"

        # Settings: runtime section from state (graph-seeded), overridable via
        # an explicit config["configurable"]["speeda"] for direct invocation.
        # Merged into a LOCAL dict - module globals are never mutated.
        settings = dict(from_json(state.get("speeda_config"), {}) or {})
        override = ((config or {}).get("configurable") or {}).get("speeda") or {}
        settings.update(override)

        try:
            max_retry = int(
                finite_in_range(
                    "speeda_config max_retry",
                    settings.get("max_retry", _DEFAULT_MAX_RETRY),
                    minimum=_MAX_RETRY_RANGE[0],
                    maximum=_MAX_RETRY_RANGE[1],
                )
            )
            timeout_s = finite_in_range(
                "speeda_config timeout_s",
                settings.get("timeout_s", _DEFAULT_TIMEOUT_S),
                minimum=_TIMEOUT_S_RANGE[0],
                maximum=_TIMEOUT_S_RANGE[1],
            )
        except ContextValidationError as exc:
            # Closed set by construction, and the only caught exception this
            # node renders: finite_in_range() composes its message from a
            # literal field name (both call sites above) and module-constant
            # bounds. The offending VALUE is never in it - see
            # src/services/security.py.
            return _error(str(exc))

        # Client built locally per call; with no injected transport it uses the
        # deterministic NETWORK-FREE stub (documented limitation, docs/02).
        base_url = str(settings.get("base_url", "") or "").strip()
        client = SpeedaClient(base_url=base_url) if base_url else SpeedaClient()

        # Token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        api_token = ctx.secrets.get(_SECRET_KEY)
        if api_token is None:
            if client.uses_stub_transport:
                # Stub limitation: no request leaves the process, so run with a
                # non-credential placeholder (see module docstring).
                api_token = _STUB_PLACEHOLDER
            else:
                return _error(
                    f"secret {_SECRET_KEY} unavailable - " "refusing to call a live transport unauthenticated"
                )

        company_id = state.get("company_id", "") or str(payload.get("company_code", "") or "")
        # READ-ONLY lookups always target an explicit company record.
        if not company_id:
            return _error("unresolved company code - cannot research company")

        if intent not in _LOOKUPS:
            return _error(f"unknown intent '{intent}'")

        try:
            resp = self._lookup(client, intent, company_id, api_token, max_retry, timeout_s)
        # The two closed-set labels an upstream failure is allowed to publish are
        # the HTTP status and the exception CLASS. Both are bound to a local
        # first, so no caught exception appears in an f-string at all: an
        # interpolated exception renders str(exc), and SpeedaApiError composes
        # its message from the upstream response body (see speeda_client).
        except SpeedaApiError as exc:
            status_code = exc.status_code
            return _error(f"SPEEDA API error {status_code}")
        except _BudgetExceeded:
            return _error(f"SPEEDA lookup exceeded the {timeout_s:g}s budget")
        except Exception as exc:  # transport failure - no silent pass
            exc_class = type(exc).__name__
            return _error(f"SPEEDA call failed ({exc_class})")

        shaped = _LOOKUPS[intent][1](resp or {}, company_id, state)
        if shaped is None:
            # The reason names the record TYPE (a closed-set label), never the
            # company code: error_log rides the caller-facing error envelope
            # that post_process builds, so interpolating the code would put the
            # record evidence straight back into it.
            return _error(_MISSING_RECORD[intent])

        record_id, company_name, summary = shaped
        record_ref = f"speeda://companies/{record_id}" if record_id else ""

        # Audit the tool call - intent + presence signals only,
        # never company content or credentials.
        emit_trace_event(
            "call_speeda_api_complete",
            {
                "intent": intent,
                "has_record_id": bool(record_id),
                "stub_transport": client.uses_stub_transport,
            },
            state,
        )

        return {
            "record_id": record_id,
            "record_ref": record_ref,
            "company_id": company_id or record_id,
            "company_name": company_name,
            "summary": summary,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- transport ------------------------------------------------------------

    def _lookup(
        self,
        client: SpeedaClient,
        intent: str,
        company_id: str,
        api_token: str,
        max_retry: int,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Call the endpoint for *intent*, retrying transient upstream failures.

        The retry count and the wall-clock budget both come from the runtime
        config. The budget is checked BEFORE each attempt so a slow upstream
        cannot stretch the call past it by one more request; a caller error
        (4xx) is never retried.
        """
        method = _LOOKUPS[intent][0]
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        for attempt in range(max_retry + 1):
            if attempt and time.monotonic() >= deadline:
                raise _BudgetExceeded()
            try:
                return getattr(client, method)(company_id, api_token) or {}
            except SpeedaApiError as exc:
                if exc.status_code < _RETRYABLE_STATUS_MIN:
                    raise
                last_error = exc
            except Exception as exc:
                last_error = exc
        raise last_error if last_error else _BudgetExceeded()


class _BudgetExceeded(Exception):
    """The wall-clock budget for the lookup ran out before it could succeed."""


def _error(reason: str) -> dict[str, Any]:
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [f"CallSpeedaApiNode: {reason}"],
    }


# -- response shaping (one per read-only intent) -----------------------------


def _shape_profile(resp: dict[str, Any], company_id: str, state: dict[str, Any]) -> "tuple[str, str, str] | None":
    company = resp.get("company") or {}
    if not company:
        return None
    record_id = str(company.get("code", "")) or company_id
    company_name = state.get("company_name", "") or str(company.get("name", ""))
    summary = str(company.get("description", "")) or str(company.get("name", ""))
    return record_id, company_name, summary


def _shape_industry(resp: dict[str, Any], company_id: str, state: dict[str, Any]) -> "tuple[str, str, str] | None":
    industries = resp.get("industries") or []
    if not industries:
        return None
    record_id = str(resp.get("company_code", "")) or company_id
    names = [str(i.get("name", "")) for i in industries if i.get("name")]
    return record_id, state.get("company_name", ""), "industry classification: " + ", ".join(names)


def _shape_financials(resp: dict[str, Any], company_id: str, state: dict[str, Any]) -> "tuple[str, str, str] | None":
    rows = resp.get("financials") or []
    if not rows:
        return None
    record_id = str(resp.get("company_code", "")) or company_id
    latest = rows[0]
    summary = (
        f"fy{latest.get('fiscal_year', '')} highlights ({latest.get('currency', '')}): "
        f"revenue {latest.get('revenue', '')}, "
        f"operating income {latest.get('operating_income', '')}, "
        f"net income {latest.get('net_income', '')}"
    )
    return record_id, state.get("company_name", ""), summary


_LOOKUPS = {
    "lookup_company": ("get_company", _shape_profile),
    "lookup_industry": ("get_industry", _shape_industry),
    "summarize_financials": ("get_financials", _shape_financials),
}

# Closed-set not-found reasons. Each names the record TYPE the lookup asked for
# and NOTHING drawn from the request: these reasons ship to the caller inside
# post_process's error envelope, so a reason carrying the company code would
# disclose the very record the caller is being told was not found.
_MISSING_RECORD = {
    "lookup_company": "no matching company record found",
    "lookup_industry": "no industry classification found for the requested company",
    "summarize_financials": "no financial highlights found for the requested company",
}
