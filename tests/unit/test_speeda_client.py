# CMN-C2-287 - Unit tests: SpeedaClient service (SPEEDA REST API shape)
# Pure service layer (stdlib-only, no framework imports) - plain function tests.
#
# READ-ONLY research domain: the client exposes GET-shaped lookups ONLY -
# test_client_has_no_mutation_methods proves no mutation surface exists.

import pytest

from src.services.speeda_client import SpeedaApiError, SpeedaClient


def test_get_company_success_with_injected_get():
    captured = {}

    def get(url, headers, params):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return 200, {"company": {"code": "c-7203", "name": "company c-7203"}}

    client = SpeedaClient("https://speeda.example.test/v1/", get=get)
    resp = client.get_company("c-7203", "tok123")
    assert resp["company"]["code"] == "c-7203"
    # Trailing slash trimmed - no double slash in the endpoint URL.
    assert captured["url"] == "https://speeda.example.test/v1/companies/c-7203"
    # SPEEDA REST API auth: the per-call token travels as a Bearer header.
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["params"]["company_code"] == "c-7203"


def test_get_industry_success_with_injected_get():
    captured = {}

    def get(url, headers, params):
        captured["url"] = url
        captured["params"] = params
        return 200, {"company_code": "c-7203", "industries": [{"code": "i1", "name": "mobility"}]}

    client = SpeedaClient("https://speeda.example.test/v1", get=get)
    resp = client.get_industry("c-7203", "tok")
    assert resp["industries"][0]["name"] == "mobility"
    assert captured["url"] == "https://speeda.example.test/v1/companies/c-7203/industries"
    assert captured["params"]["_speeda_op"] == "industry"


def test_get_financials_success_with_injected_get():
    captured = {}

    def get(url, headers, params):
        captured["url"] = url
        captured["params"] = params
        return 200, {"company_code": "c-7203", "financials": [{"fiscal_year": "2025"}]}

    client = SpeedaClient("https://speeda.example.test/v1", get=get)
    resp = client.get_financials("c-7203", "tok")
    assert resp["financials"][0]["fiscal_year"] == "2025"
    assert captured["url"] == "https://speeda.example.test/v1/companies/c-7203/financials"
    assert captured["params"]["_speeda_op"] == "financials"


def test_non_2xx_raises_speeda_api_error():
    def get(url, headers, params):
        return 404, {"errors": ["company not found"]}

    client = SpeedaClient("https://speeda.example.test/v1", get=get)
    with pytest.raises(SpeedaApiError) as exc:
        client.get_company("c-0000", "tok")
    assert exc.value.status_code == 404
    assert "company not found" in str(exc.value)


def test_non_2xx_message_field_fallback():
    def get(url, headers, params):
        return 500, {"message": "internal error"}

    client = SpeedaClient("https://speeda.example.test/v1", get=get)
    with pytest.raises(SpeedaApiError) as exc:
        client.get_financials("c-7203", "tok")
    assert exc.value.status_code == 500
    assert "internal error" in str(exc.value)


def test_default_stub_transport_profile_shape():
    # No transport injected -> deterministic, network-free default transport.
    client = SpeedaClient()
    assert client.uses_stub_transport is True
    resp = client.get_company("c-7203", "tok")
    assert resp.get("_stub") is True
    company = resp["company"]
    assert company["code"] == "c-7203"
    assert company["name"] == "company c-7203"
    assert "network-free default transport" in company["description"]


def test_default_stub_transport_industry_shape():
    client = SpeedaClient()
    resp = client.get_industry("c-7203", "tok")
    assert resp.get("_stub") is True
    assert resp["company_code"] == "c-7203"
    assert resp["industries"]
    assert resp["industries"][0]["name"] == "diversified industrials"


def test_default_stub_transport_financials_shape():
    client = SpeedaClient()
    resp = client.get_financials("c-7203", "tok")
    assert resp.get("_stub") is True
    assert resp["company_code"] == "c-7203"
    row = resp["financials"][0]
    assert row["fiscal_year"] == "2025"
    assert row["currency"] == "jpy"
    assert isinstance(row["revenue"], int)
    assert isinstance(row["operating_income"], int)
    assert isinstance(row["net_income"], int)


def test_stub_transport_is_deterministic():
    a = SpeedaClient().get_financials("c-7203", "tok")
    b = SpeedaClient().get_financials("c-7203", "tok")
    assert a == b


def test_injected_transport_disables_stub_flag():
    client = SpeedaClient(get=lambda url, headers, params: (200, {"company": {}}))
    assert client.uses_stub_transport is False


def test_client_has_no_mutation_methods():
    """READ-ONLY domain proof: the public client surface is exactly the three
    GET-shaped lookups - no create/update/delete/post/patch method exists."""
    public_callables = sorted(
        name for name in dir(SpeedaClient) if not name.startswith("_") and callable(getattr(SpeedaClient, name))
    )
    assert public_callables == ["get_company", "get_financials", "get_industry"]
    for verb in ("create", "update", "delete", "post", "put", "patch", "write"):
        assert not any(name.startswith(verb) for name in dir(SpeedaClient)), verb


def test_token_never_persisted_on_instance():
    """The per-call token is never stored on the client instance."""
    client = SpeedaClient(
        "https://speeda.example.test/v1", get=lambda url, headers, params: (200, {"company": {"code": "c-1"}})
    )
    client.get_company("c-1", "tok123")
    assert "tok123" not in str(vars(client))
