# SPEEDA Company Research Agent

AI agent for researching companies with SPEEDA data, built with Agentic Star.

> **Category**: Cat 2 (Domain-specific pipeline)
> **Industry**: Common
> **Template ID**: CMN-C2-287

## Overview

Turns a plain-language company research question — "look up the profile for company code
c-7203", "what industry is it classified under?", "summarize its financial highlights" — into a
read-only lookup against the SPEEDA business-intelligence REST API, and returns the retrieved
record together with a human-readable confirmation of exactly which company record was read.

The pipeline is deterministic: it classifies the request into one of three read-only intents,
extracts the target company code and any filter fields, builds the corresponding API request,
performs the lookup, and formats the answer. No model call is involved. The target company is
never guessed — a code the caller did not supply and the text does not contain is left
unresolved and reported as an error rather than substituted, so the agent cannot quietly research
the wrong company. The client exposes lookups only; there is no create, update or delete path.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the installed framework version does not match, graph
compilation and start-up preflight fail and the agent refuses to start rather than coming up in a
partially working state. This is intentional — a half-running agent is worse than one that
refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

### Calling the agent

The request text goes in `input`; the company to research is supplied out-of-band on the
`input_context` channel, which is restricted to short inert identifiers:

```json
{
  "input": "summarize the financial highlights",
  "input_context": {"company_id": "c-7203"}
}
```

`company_id`, `company_hint`, `company_code` and `ticker` are accepted, in that order of
priority. Values must match `[A-Za-z0-9][A-Za-z0-9_-]{0,19}` — anything longer, or carrying
free text, is refused with an error naming the field rather than being passed through.

### External connection

`src/services/speeda_client.py` ships a deterministic, network-free default transport so the
pipeline is runnable and testable without a live subscription. It returns the documented response
shapes; it does **not** perform a live call. To go live, inject a real `get` transport at
construction time and provision the `SPEEDA_TOKEN` secret — the method contracts and parameter
shapes follow the REST API, so no business logic changes.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design specification and test specification
```

See `docs/02_design.md` for the architecture and security design, and `docs/03_test_spec.md` for
the test contract.

## Customising

1. Adjust `config/config.yaml` for your own environment (API base URL, retries, timeout).
2. Inject a live transport into `SpeedaClient` and provision the integration token.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
