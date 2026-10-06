"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) request into one of lookup_company /
lookup_industry / summarize_financials using a deterministic keyword
heuristic, so the template is testable and runnable without a live model
(see docs/02_design.md). Every intent is
READ-ONLY (research domain - no mutation exists in this template);
low-confidence / unknown falls back to the "lookup_company" default with a
note.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_VALID_INTENTS = ("lookup_company", "lookup_industry", "summarize_financials")

# Deterministic keyword signals (checked in priority order, most specific
# first so a "summarize the financials of the company" style request
# classifies as the financial-highlights summary, not the generic lookup).
_KEYWORDS = (
    (
        "summarize_financials",
        (
            "financial",
            "financials",
            "revenue",
            "earnings",
            "profit",
            "income",
            "performance",
            "highlights",
            "業績",
            "財務",
            "決算",
            "売上",
            "利益",
        ),
    ),
    (
        "lookup_industry",
        (
            "industry",
            "industries",
            "classification",
            "sector",
            "segment",
            "categor",
            "業種",
            "業界",
            "分類",
            "セクター",
        ),
    ),
    (
        "lookup_company",
        (
            "look up",
            "lookup",
            "find",
            "show",
            "get",
            "fetch",
            "retrieve",
            "search",
            "profile",
            "overview",
            "what is",
            "who is",
            "summarize",
            "会社概要",
            "企業概要",
            "照会",
            "検索",
            "参照",
            "確認",
            "概要",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the request into a SPEEDA company-research intent."""

    # Inner domain node, read-only classification of already-redacted text -
    # the external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify_via_keywords(text)

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, " "defaulted to lookup_company (read-only)"]
            intent = "lookup_company"

        # Audit the classification decision - intent label only, never the text.
        emit_trace_event(
            "classify_intent_complete",
            {"intent": intent, "defaulted": bool(note)},
            state,
        )

        result: dict[str, Any] = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        # No signal at all: fall through to the read-only default via the
        # _VALID_INTENTS guard in execute() (returns a sentinel outside the set).
        return "unknown"
