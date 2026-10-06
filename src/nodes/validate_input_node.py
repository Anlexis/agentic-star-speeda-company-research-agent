"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-request input, screens it for injection, and runs a
deterministic (regex, NOT LLM) scan for email addresses / token-like strings,
which are flag-and-redacted before anything is logged. A company-research
request legitimately names a company (the framework's own input mask in
BaseNode.__call__ additionally masks emails/phones/names in user_input /
validated_input), so this is flag-and-redact for safe logging, not a hard
reject. The hard rejects are the empty/non-request guard and the injection
screen.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json
from src.services.security import sanitize_query, screen_injection

# Deterministic patterns: email addresses and bearer/JWT/API-token-like
# strings that might appear in a pasted request. Flagged + redacted before logging.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(
    r"\b(?:eyJ[A-Za-z0-9_-]{6,}|secret_[A-Za-z0-9]{6,}|sk-[A-Za-z0-9]{6,}|AKIA[A-Z0-9]{16})\b"
    r"|Bearer\s+[A-Za-z0-9._-]{16,}"
)
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real request rather than noise.
_MIN_LEN = 3


class ValidateInputNode(FunctionNode):
    """Validate, screen and flag-and-redact the inbound company-research request."""

    # Inner domain node - the external trust gate lives on the outer backbone
    # pre_process (VERIFIED_EXTERNAL); the caller context is forwarded unchanged.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        company_hint = state.get("company_hint", "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                company_hint = obj.get("company_hint", company_hint)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-request input"],
            }

        # Screen again here, not only at the outer boundary: this graph is
        # independently invocable, so its own first node has to own the refusal
        # rather than assume an upstream node already ran. Both the raw text and
        # the markup-stripped text are screened - stripping deletes chat-template
        # control tokens and re-joins directives that were split by tags, so each
        # form hides an attack the other one catches.
        violation = screen_injection(text, sanitize_query(text))
        if violation:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ValidateInputNode: request refused by the input screen ({violation})"],
            }

        # Deterministic flag-and-redact (before any logging).
        # Local list per invocation - never a module-global (no cross-invoke leak).
        flags: list[str] = []
        redacted = text
        if _EMAIL_RE.search(redacted):
            flags.append("email")
            redacted = _EMAIL_RE.sub(_REDACTION, redacted)
        if _TOKEN_RE.search(redacted):
            flags.append("token")
            redacted = _TOKEN_RE.sub(_REDACTION, redacted)

        # Audit the scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "validate_input_complete",
            {"has_company_hint": bool(company_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "company_hint": company_hint,
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
