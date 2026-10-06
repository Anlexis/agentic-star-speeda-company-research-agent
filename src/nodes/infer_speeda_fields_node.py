"""AgentCore Platform v1.0 - inner workflow Step 3: InferSpeedaFields.

Extracts the company code, display name, and "Key: value" filter fields from
the (redacted) request and assembles a validated SPEEDA REST API request
parameter set for the classified intent. The company code is taken only from
an explicit code in the text or the caller-supplied company_hint - an
unresolved code is left empty rather than invented (risk mitigation: never
research the wrong company record; the executor surfaces the miss as
status=error). Deterministic - no model call (docs/02_design.md).
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# A SPEEDA company code / ticker: short alphanumeric identifier (no spaces).
_CODE_SHAPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
# Explicit code mention in the request text, EN or JA
# ("company code c-7203" / "ticker symbol 7203" / "企業コード c-7203").
_CODE_IN_TEXT_RE = re.compile(
    r"(?:company|ticker|corporate|stock)\s+(?:code|id|number|symbol)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})"
    r"|(?:企業コード|会社コード|証券コード|銘柄コード)\s*[:：#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{0,19})",
    re.IGNORECASE,
)
# Quoted display name: named "Foo" / called "Foo". Curly quotes as \u escapes so
# the source stays pure ASCII (push-safe).
_NAME_QUOTED_RE = re.compile(r'(?:named|called|for)\s+["“]([^"”\n]+)["”]', re.IGNORECASE)
# "Key: value" filter-field lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana + CJK unified ideographs, as \u escapes (push-safe;
# the re module resolves \uXXXX inside raw patterns).
_KV_RE = re.compile(r"^\s*([A-Za-z\u3040-\u30ff\u4e00-\u9fff][\w \-\u3040-\u30ff\u4e00-\u9fff]{0,40})[:：]\s*(.+?)\s*$")
# Keys that are the code/name themselves, not research filter fields.
_CODE_KEYS = ("code", "company code", "ticker", "id")
_NAME_KEYS = ("name", "company name", "company")

# Bounds on the filter fields lifted out of the request text. They are caller
# text that ends up in an outbound API request, so they are capped in number and
# in length and restricted to a printable domain alphabet - no markup, no
# quotes, no control characters. A field outside those bounds is DROPPED and
# counted; it is never truncated into something the caller did not write, and
# the dropped value is never echoed.
_MAX_FILTERS = 10
_MAX_FILTER_VALUE_CHARS = 64
_FILTER_VALUE_RE = re.compile(r"^[\w ,.\-/&()\u3040-\u30ff\u4e00-\u9fff]{1,%d}$" % _MAX_FILTER_VALUE_CHARS)


class InferSpeedaFieldsNode(FunctionNode):
    """Extract entities and assemble the SPEEDA REST API request parameters."""

    # Inner domain node - derives fields from already-validated text; the
    # external trust gate lives on the outer backbone pre_process.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "lookup_company") or "lookup_company"
        company_hint = state.get("company_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferSpeedaFieldsNode: missing validated_input"],
            }

        fields, dropped_fields = self._parse_fields(text)
        company_id = self._resolve_code(text, company_hint, fields)
        company_name = self._resolve_name(text, fields)

        # READ-ONLY research domain: every intent maps to a GET request
        # parameter set (no mutation payload exists in this template).
        if intent == "summarize_financials":
            payload = self._build_params(company_id, "financials", fields)
        elif intent == "lookup_industry":
            payload = self._build_params(company_id, "industry", fields)
        else:  # lookup_company (read-only default)
            payload = self._build_params(company_id, "profile", fields)

        # Audit the assembled parameter shape - field signals only, not content.
        emit_trace_event(
            "infer_speeda_fields_complete",
            {
                "intent": intent,
                "has_company_id": bool(company_id),
                "n_fields": len(fields),
                "n_dropped_fields": dropped_fields,
            },
            state,
        )

        return {
            "company_id": company_id,
            "company_name": company_name,
            "speeda_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _resolve_code(self, text: str, company_hint: str, fields: "list[tuple[str, str]]") -> str:
        """Explicit code only: text mention > code-shaped hint > 'Code:' field. Never invented."""
        m = _CODE_IN_TEXT_RE.search(text)
        if m:
            return m.group(1) or m.group(2) or ""
        hint = company_hint.strip()
        if hint and _CODE_SHAPE_RE.match(hint):
            return hint
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS and _CODE_SHAPE_RE.match(value.strip()):
                return value.strip()
        return ""  # unresolved - left empty, never invented

    def _resolve_name(self, text: str, fields: "list[tuple[str, str]]") -> str:
        m = _NAME_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:100]
        for key, value in fields:
            if key.strip().lower() in _NAME_KEYS:
                return value.strip()[:100]
        return ""

    def _parse_fields(self, text: str) -> "tuple[list[tuple[str, str]], int]":
        """Return the bounded [(key, value), ...] filter fields plus a dropped count."""
        fields: list[tuple[str, str]] = []
        dropped = 0
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(2).strip()
            if not _FILTER_VALUE_RE.match(value):
                dropped += 1
                continue
            if len(fields) >= _MAX_FILTERS:
                dropped += 1
                continue
            fields.append((key, value))
        return fields, dropped

    # -- parameter assembly (SPEEDA REST API read-only request shape) ----------

    def _build_params(self, company_id: str, detail: str, fields: "list[tuple[str, str]]") -> dict[str, Any]:
        params: dict[str, Any] = {"company_code": company_id, "detail": detail}
        filters = []
        for key, value in fields:
            if key.strip().lower() in _CODE_KEYS + _NAME_KEYS:
                continue
            filters.append({"name": key, "values": [value]})
        if filters:
            params["filters"] = filters
        return params
