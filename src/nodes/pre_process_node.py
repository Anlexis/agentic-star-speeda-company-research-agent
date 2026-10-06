"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: this is the node that owns the caller contract. It
screens the request text for injection, validates every caller-supplied
context field against explicit bounds, and serializes the result into a single
JSON string in `validated_input`, which the GraphNode (`main` slot) hands to the
inner SPEEDA workflow graph. Business validation happens inside the inner
graph's ValidateInputNode; the boundary rules live here.

Refusal is enforced by this node, not delegated to the framework input gate:
where that gate is absent or configured off, a payload it would have refused
otherwise reaches the answer path and returns success. The screens below run
on the raw text AND on the sanitized text, because markup stripping deletes
chat-template control tokens while re-assembling directives that were split by
tags - each representation hides an attack from the other.
"""

import json
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.security import (
    MAX_CONTEXT_KEYS,
    ContextValidationError,
    mask_field_name,
    sanitize_query,
    screen_injection,
    validate_context_identifier,
)

# Caller context fields that resolve the research target, in priority order.
# Every one of them is an inert identifier; nothing else is accepted on this
# channel, which is also what keeps a credential-shaped blob from ever
# arriving on it.
_TARGET_FIELDS = ("company_id", "company_hint", "company_code", "ticker")

# Platform-standard key, not a caller-controlled one: shared.bootstrap.
# marketplace_app.run_agent_marketplace unconditionally seeds
# input_context = {"conversation_history": history} on EVERY Marketplace
# invoke (see shared/bootstrap/marketplace_app.py), before any template code
# runs. Rejecting it as an "unsupported field" - which the unknown-field
# check below did until this fix - meant every single real Marketplace
# invoke of this template failed at pre_process, regardless of message
# content. It carries no target-resolution value, so it is recognised and
# ignored rather than added to _TARGET_FIELDS.
_IGNORED_FIELDS = ("conversation_history",)


class PreProcessNode(FunctionNode):
    """Screen and bound the caller request, then serialize it for the inner graph."""

    # The outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner SPEEDA call runs under this same (unelevated)
    # context, so the external gate lives HERE, not on the inner API node. An
    # under-trusted (ANONYMOUS) caller is denied at this gate before any call.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only [C1]

        if not user_input or not user_input.strip():
            return self._refuse("user_input is empty or missing")

        raw_input = user_input.strip()
        sanitized_input = sanitize_query(raw_input)

        violation = screen_injection(raw_input, sanitized_input)
        if violation:
            # Name the class, never the payload.
            return self._refuse(f"request refused by the input screen ({violation})")

        try:
            company_hint = self._resolve_target(input_context)
        except ContextValidationError as exc:
            # The message names the FIELD (drawn from _TARGET_FIELDS) and the
            # expected shape, never the rejected value. An unsupported key is
            # caller-controlled text, so it arrives through mask_field_name(),
            # which describes anything outside the inert identifier alphabet by
            # shape and withholds a credential-shaped name even when it fits.
            return self._refuse(str(exc))

        validated_input = json.dumps({"text": sanitized_input, "company_hint": company_hint})

        # Audit the shaped request - hint presence only, not the raw text.
        emit_trace_event(
            "pre_process_complete",
            {"has_company_hint": bool(company_hint)},
            state,
        )

        return {
            "validated_input": validated_input,
            "company_hint": company_hint,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- caller contract ------------------------------------------------------

    def _resolve_target(self, input_context: object) -> str:
        """Validate the caller context channel and return the target identifier.

        Fails closed on anything outside the declared contract. Unknown keys are
        refused rather than ignored: silently dropping them would let a caller
        believe a filter took effect. Their names are masked in the error - a
        rejected key is caller-controlled text like any other.
        """
        if not input_context:
            return ""
        if not isinstance(input_context, dict):
            raise ContextValidationError("input_context must be an object")
        if len(input_context) > MAX_CONTEXT_KEYS:
            raise ContextValidationError(f"input_context accepts at most {MAX_CONTEXT_KEYS} fields")

        unknown = [k for k in input_context if k not in _TARGET_FIELDS and k not in _IGNORED_FIELDS]
        if unknown:
            names = ", ".join(mask_field_name(k) for k in sorted(unknown, key=str))
            raise ContextValidationError(f"input_context has unsupported field(s): {names}")

        # Validate EVERY supplied field, not only the winner - a caller that
        # sends a malformed lower-priority field gets told, instead of having it
        # silently ignored because a higher-priority field happened to be valid.
        resolved = ""
        for field in _TARGET_FIELDS:
            if field not in input_context:
                continue
            candidate = validate_context_identifier(field, input_context[field])
            if candidate and not resolved:
                resolved = candidate
        return resolved

    @staticmethod
    def _refuse(reason: str) -> dict[str, Any]:
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {reason}"],
        }
