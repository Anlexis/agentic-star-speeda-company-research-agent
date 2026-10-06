"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only - no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import json
import os
import secrets
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.graph.graph import SpeedaCompanyResearchAgent
from src.services.security import (
    MAX_CONTEXT_KEYS,
    ContextValidationError,
    mask_field_name,
    validate_context_identifier,
)

app = FastAPI(title="Agent")

agent = SpeedaCompanyResearchAgent()
agent.compile()
agent.provision_secrets(secrets_factory(namespace="cmn", agent_name="SpeedaCompanyResearchAgent"))

# Adapter-level caps. The request body is bounded before anything downstream
# sees it, so an oversized payload is refused here rather than consuming the
# pipeline. The research request itself is capped again (and lower) by the
# sanitizer in pre_process.
MAX_INPUT_CHARS = 32_000
MAX_CONTEXT_BYTES = 4_096

# The research target travels on this channel; every field is an inert
# identifier. Validating them HERE, before invoke(), is what turns a malformed
# or hostile value into an actionable 400 naming the field. Left to the graph,
# a credential-shaped value is refused by the framework's own output scan on the
# very first node - correctly, but with an opaque error the caller cannot act on,
# because that node returns the request context verbatim in its result.
_CONTEXT_FIELDS = ("company_id", "company_hint", "company_code", "ticker")


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    input_context: dict[str, Any] = Field(default_factory=dict)


def _validated_context(raw: dict[str, Any]) -> dict[str, str]:
    """Return the bounded caller context, or raise HTTP 400 naming the field."""
    if not raw:
        return {}
    if len(raw) > MAX_CONTEXT_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"input_context accepts at most {MAX_CONTEXT_KEYS} fields.",
        )
    if len(json.dumps(raw, default=str).encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"input_context exceeds {MAX_CONTEXT_BYTES} bytes.",
        )
    unknown = [k for k in raw if k not in _CONTEXT_FIELDS]
    if unknown:
        names = ", ".join(mask_field_name(k) for k in sorted(unknown, key=str))
        raise HTTPException(status_code=400, detail=f"input_context has unsupported field(s): {names}")
    cleaned: dict[str, str] = {}
    for field in _CONTEXT_FIELDS:
        if field not in raw:
            continue
        try:
            value = validate_context_identifier(field, raw[field])
        except ContextValidationError as exc:
            # The message names the field and the expected shape, never the value.
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if value:
            cleaned[field] = value
    return cleaned


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    if len(req.input) > MAX_INPUT_CHARS:
        raise HTTPException(status_code=400, detail=f"input exceeds {MAX_INPUT_CHARS} characters.")
    input_context = _validated_context(req.input_context)

    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted. This
    # adapter is the entry-point auth boundary - a deployment-level caller
    # credential, not an agent secret, so ctx.secrets does not apply (no
    # InvocationContext exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose - do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=input_context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "SpeedaCompanyResearchAgent"}
