"""AgentCore Platform v1.0 - SPEEDA REST API client (read-only research).

Service layer: a thin wrapper around the SPEEDA (Uzabase business-intelligence
SaaS) REST API company-research endpoints. Contains NO business logic, NO
routing, and NO credentials - the integration token is passed in per call by
the node (which reads it via ctx.secrets). This module imports no
framework/SDK internals - pure stdlib.

The domain is READ-ONLY: company profile lookup, industry classification
lookup, and financial-highlights retrieval. No mutation endpoint exists on
this client by design.

DEFAULT TRANSPORT (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented SPEEDA response shapes (a ``company`` object for profile
    lookups; an ``industries`` list for classification lookups; a
    ``financials`` list for highlights) so the pipeline is runnable and
    testable without a live SPEEDA subscription or the ``requests`` package -
    it does NOT perform a live SPEEDA call. The rule it follows: never fake a
    live call, and state the limitation where a reader will see it.

    To perform real SPEEDA calls, inject a live transport (requests-based
    ``get``) at construction time; the method contracts and parameter shapes
    follow the SPEEDA REST API, so no business-logic change is needed to go
    live. A live transport also requires a real integration token (see
    CallSpeedaApiNode - the stub runs without one because no request ever
    leaves the process).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, params) -> (status_code, response_dict)
Transport = Callable[[str, dict[str, Any], dict[str, Any]], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.speeda.com/v1"


class SpeedaApiError(Exception):
    """Raised when the SPEEDA REST API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"SPEEDA API error {status_code}: {message}")


class SpeedaClient:
    """SPEEDA REST API company-research client (read-only).

    Args:
        base_url: SPEEDA API base URL (default https://api.speeda.com/v1).
        get: optional injected transport (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE v1 stub is used
            (see the module docstring - it returns the documented shape without
            a live SPEEDA call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._get is None

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_token: str) -> dict[str, str]:
        """Build the SPEEDA REST API auth headers.

        api_token is supplied per-call by the node (from ctx.secrets); it is
        never persisted on the instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_token}",
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(self, url: str, headers: dict[str, Any], params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Deterministic, network-free stub - returns the documented SPEEDA shape.

        NOT a live call. Synthetic values are derived from the request so the
        response is stable and inspectable. All synthetic display strings are
        lowercase, so the framework's name mask does not rewrite them. See the
        module docstring for the limitation and how to inject a live transport.
        """
        seed = url + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        code = str(params.get("company_code", "")) or f"c-{digest[:8]}"
        op = params.get("_speeda_op", "profile")
        if op == "industry":
            # Documented industry-classification shape: {"company_code", "industries": [...]}
            return 200, {
                "company_code": code,
                "industries": [
                    {"code": f"ind-{digest[:4]}", "name": "diversified industrials"},
                ],
                "_stub": True,  # marks the network-free default transport response
            }
        if op == "financials":
            # Documented financial-highlights shape: {"company_code", "financials": [...]}
            return 200, {
                "company_code": code,
                "financials": [
                    {
                        "fiscal_year": "2025",
                        "currency": "jpy",
                        "revenue": int(digest[:6], 16),
                        "operating_income": int(digest[6:11], 16),
                        "net_income": int(digest[11:16], 16),
                    }
                ],
                "_stub": True,  # marks the network-free default transport response
            }
        # profile (default) - documented company-profile shape: {"company": {...}}
        return 200, {
            "company": {
                "code": code,
                "name": f"company {code}",
                "industry": "diversified industrials",
                "country": "jp",
                "description": f"synthetic company profile for {code} (network-free default transport)",
            },
            "_stub": True,  # marks the network-free default transport response
        }

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API (read-only) ----------------------------------------------

    def get_company(self, company_code: str, api_token: str) -> dict[str, Any]:
        """GET /companies/{code} - look up a company's profile record.

        Returns the parsed response dict (containing ``company``). Raises
        SpeedaApiError on non-2xx.
        """
        url = f"{self._base_url}/companies/{company_code}"
        transport = self._resolve(self._get)
        status, body = transport(url, self._headers(api_token), {"_speeda_op": "profile", "company_code": company_code})
        if not (200 <= status < 300):
            raise SpeedaApiError(status, _err_message(body))
        return body

    def get_industry(self, company_code: str, api_token: str) -> dict[str, Any]:
        """GET /companies/{code}/industries - look up a company's industry classification.

        Returns the parsed response dict (containing ``industries``). Raises
        SpeedaApiError on a non-2xx status.
        """
        url = f"{self._base_url}/companies/{company_code}/industries"
        transport = self._resolve(self._get)
        status, body = transport(
            url, self._headers(api_token), {"_speeda_op": "industry", "company_code": company_code}
        )
        if not (200 <= status < 300):
            raise SpeedaApiError(status, _err_message(body))
        return body

    def get_financials(self, company_code: str, api_token: str) -> dict[str, Any]:
        """GET /companies/{code}/financials - retrieve a company's financial highlights.

        Returns the parsed response dict (containing ``financials``). Raises
        SpeedaApiError on a non-2xx status.
        """
        url = f"{self._base_url}/companies/{company_code}/financials"
        transport = self._resolve(self._get)
        status, body = transport(
            url, self._headers(api_token), {"_speeda_op": "financials", "company_code": company_code}
        )
        if not (200 <= status < 300):
            raise SpeedaApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from a SPEEDA error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)
