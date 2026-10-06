# Template Design Specification — CMN-C2-287 SPEEDA Company Research Agent

## Position in AgentCore Architecture

- **Agent Class**: `SpeedaCompanyResearchAgent` (`src/graph/graph.py`)
- **L1 Base**: `AgentBaseGraph`
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Agent type**: ToolCallingAgent — classify intent -> extract company-research
  fields -> build a SPEEDA REST API request -> call the tool -> format the
  confirmation. No retrieval, no autonomous loop. The domain is **READ-ONLY**
  (research): every intent is a lookup — no mutation endpoint exists anywhere in
  this template.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

| Layer | Class |
|---|---|
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Trust | Inherits/Overrides |
|------|---------------|-------------|--------------|-------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | framework default | InitializeNode (default) |
| pre_process | owns the caller contract: injection screen, `input_context` bounds, markup/length sanitize, serialize into `validated_input` (JSON) | user_input, input_context | validated_input, company_hint | **VERIFIED_EXTERNAL** (the single external gate) | PreProcessNode (FunctionNode) |
| main | run the inner SPEEDA workflow subgraph | validated_input | result, intent, company_id, record_id, record_ref, company_name, summary, confirmation, speeda_payload | GraphNode (caller ctx forwarded unchanged) | SpeedaWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; module-level `_security_gate_output()` — pattern scan, precision grid, re-scan | inner-result fields | formatted_output | ANONYMOUS | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | framework default | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes. **Every inner domain node declares
`required_trust_level = TrustLevel.ANONYMOUS`** — the caller's
`InvocationContext` is forwarded into the subgraph unchanged, so the single
external trust gate stays on the backbone `pre_process`.

| Inner node | Step | Responsibility | Output | Trust |
|------|------|---------------|--------|-------|
| validate_input | 1 ValidateInput | empty/non-request guard; injection screen (this graph is independently invocable, so it owns its own refusal); deterministic (regex) flag-and-redact of email/token-like strings before logging | validated_input, company_hint, redaction_flags | ANONYMOUS |
| classify_intent | 2 ClassifyIntent | deterministic keyword classification -> lookup_company / lookup_industry / summarize_financials (all READ-ONLY); low-confidence -> lookup_company default | intent | ANONYMOUS |
| infer_speeda_fields | 3 InferSpeedaFields | extract company code / display name / bounded "Key: value" filter fields; assemble the SPEEDA REST API request parameters per intent; an unresolved company code is left empty (never invented) | company_name, company_id, speeda_payload | ANONYMOUS |
| call_speeda_api | 4 CallSpeedaApi | GET /companies/{code} (profile) / GET .../industries (classification) / GET .../financials (highlights) via `SpeedaClient`, within the configured retry count and wall-clock budget; token via ctx.secrets; 4xx/5xx -> status=error | record_id, record_ref, company_id, company_name, summary | ANONYMOUS |
| confirm | 5 Confirm | format intent + record id + reference + summary into a human-readable confirmation | confirmation, result | ANONYMOUS |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max_retry) ^
Inner (inside main / SpeedaWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_speeda_fields
              -> call_speeda_api -> confirm -> END
```

The only conditional edge in the agent is the framework's own
`add_conditional_edges("main", self.route)`, whose path callable is
`AgentBaseGraph.route`. It carries no parameter annotation, so the graph engine
hands it the whole state. The inner graph's own `route()` is annotated with
**this graph's `State`**, not the framework base state: a path callable's
annotation is the input schema the state is projected through, so annotating a
domain router with the base state would hide every domain field from it — the
router would branch on values that are always absent, while unit tests calling
it with a plain dict kept passing.

Structured parameters travel as a JSON string: `pre_process` serializes
`{"text", "company_hint"}` into `validated_input`,
`SpeedaWorkflowGraphNode.extract_input()` hands that JSON to the subgraph, and
the first inner node (`validate_input`) parses it back. This envelope is the
caller-data bridge for the nested graph: `GraphNode` invokes the subgraph
**without forwarding `input_context`**, so a validated caller value that is not
carried in the envelope never reaches an inner node.

### State Definition (`src/schemas/state.py`)

All domain fields are declared `NotRequired[...]` (fields are absent until their
producer node writes them). Dict/list payloads are stored as JSON strings
(`Optional[str]`) via the module helpers `to_json` / `from_json`, used by every
producer and consumer.

| Field | Type | Purpose | Producer |
|-------|------|---------|----------|
| company_hint | NotRequired[str] | caller-supplied company code/ticker hint; never inferred | pre_process / validate_input |
| company_id | NotRequired[str] | resolved SPEEDA company code | infer_speeda_fields |
| redaction_flags | NotRequired[Optional[str]] | JSON list of patterns redacted before logging | validate_input |
| company_name | NotRequired[str] | company display name / record label | infer_speeda_fields / call_speeda_api |
| speeda_payload | NotRequired[Optional[str]] | JSON — assembled SPEEDA REST API request parameters | infer_speeda_fields |
| speeda_config | NotRequired[Optional[str]] | JSON — runtime settings (`base_url`, `max_retry`, `timeout_s`) seeded by the inner graph's `_extra_initial_state()` | inner graph |
| record_id | NotRequired[str] | company code / record id returned by SPEEDA | call_speeda_api |
| record_ref | NotRequired[str] | human-readable record reference (`speeda://companies/<code>`) | call_speeda_api |
| summary | NotRequired[str] | compact research summary (profile / industry / financial highlights) | call_speeda_api |
| confirmation | NotRequired[str] | human-readable confirmation | confirm |

`intent`, `result`, `validated_input`, `formatted_output` are inherited from
`AgentState` and are **not** re-declared.

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — the SPEEDA token is accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Configuration

Two files, with different jobs:

| File | Contents | Read by |
|---|---|---|
| `config/agent.yaml` | the static manifest: identity, entry point, trust level, compile-time requirements. A **flat** document — no `agent:` block, no runtime values | the platform registry |
| `config/config.yaml` | runtime parameters: `max_retry`, `timeout_s`, and the `speeda:` integration section | `src/graph/graph.py` |

`load_runtime_config()` reads `config/config.yaml`;
`SpeedaCompanyResearchAgent.__init__` defaults its own config to it (the backbone
reads `max_retry` from `self.config`, so a graph built with an empty config runs
on the framework default and the declared value is dead), and
`SpeedaWorkflowGraphNode._parent_config()` forwards the whole document to the
inner graph under `config["configurable"]`. The inner graph's
`_extra_initial_state()` merges the `speeda` section with the validated
`max_retry` / `timeout_s` pair into the JSON `speeda_config` State field, which is
how the no-arg `CallSpeedaApiNode` reads them. Nodes take **no constructor
arguments**.

`max_retry` and `timeout_s` are validated at compile time: a value that is not a
finite number, or is outside its range (0–10 attempts, 0.1–600 seconds), raises
`ConfigError` rather than degrading. `float("nan")` parses without error and then
compares False against every bound, so an unchecked non-finite value silently
disables the budget it is supposed to impose.

## Caller contract

The research request text is `input`; the research target travels on
`input_context` and is restricted to **inert identifiers**:

| Field | Shape | Notes |
|---|---|---|
| `company_id`, `company_hint`, `company_code`, `ticker` | `[A-Za-z0-9][A-Za-z0-9_-]{0,19}` | first present field wins, in that order; every supplied field is validated, not just the winner |

Bounds are enforced twice — at the HTTP adapter (`src/api/server.py`, which
answers 400 with the field name) and again in `PreProcessNode` for callers that
arrive through the platform rather than the adapter. Unknown fields are refused
rather than ignored, and an unrecognised field NAME is reported by shape, never
echoed. Rejected values are never echoed into an error message.

Keeping this channel inert also avoids a failure mode that free text there would
create: the framework's initialize node returns `input_context` verbatim in its
result, and the framework's own output scan runs over every value of every
result — so a credential-shaped string on that channel fails the very first
node, before any template code runs, with an error the caller cannot act on. No
value matching the identifier shape above can trigger that.

## Security Design

- **Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the tool-calling `CallSpeedaApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring an inner node `INTERNAL`
  would deny a legitimate external caller before the call runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary
  (`INVOKE_AUTH_TOKEN` -> VERIFIED_EXTERNAL elevation).
- **Injection screen** — `src/services/security.py` screens the request text for
  chat-template control tokens (`<|...|>`, `[INST]`, `<<SYS>>`), instruction
  override, role hijack and prompt exfiltration, and refuses fail-closed. It runs
  on the **raw text and on the markup-stripped text**, because each hides an
  attack from the other: stripping markup deletes `<|im_start|>` outright and
  forwards the directive that followed it as ordinary text, while a directive
  split by tags (`ig<b>nore all previous instructions`) only becomes visible once
  the tags are gone. Sanitizing is not refusal. The refusal is enforced by the
  template's own nodes, not delegated to the framework input gate, so it holds
  where that gate is absent or configured off. The patterns are anchored and
  require an explicit instruction object, so ordinary research wording ("show the
  company overview", "show me the rules for industry classification", "the
  company disregards prior guidance in its filings") is unaffected.
- **Input flag-and-redact** — `ValidateInputNode.execute()` runs a deterministic
  (regex, NOT model-based) scan for email addresses and access-token-like strings
  (`eyJ...`, `secret_...`, `sk-...`, `AKIA...`, `Bearer ...`) and redacts them
  before any logging. A company-research request legitimately names a company, so
  this is flag-and-redact for safe logging, not a hard reject.
- **Secrets** — the integration token is read via
  `ctx.secrets.get("SPEEDA_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State. A missing token is tolerated **only**
  while the network-free default transport is active (no live call is made); with
  a live transport injected, a missing token is a hard `status=error`.
  `config/agent.yaml requires.secrets` is deliberately EMPTY: the manifest's
  `requires.secrets` declares keys the agent calls `ctx.secrets.require()` on,
  and declaring a key that is not provisioned makes the agent fail at compile
  time. This template uses `ctx.secrets.get()` and degrades to the network-free
  transport, so the token is an operational prerequisite for a live transport,
  not a compile-time requirement.
- **Output gate** — the module-level `_security_gate_output()` in
  `src/nodes/post_process_node.py`, called from `PostProcessNode.execute()`. No
  node defines `_extra_security_gate_input/_output` instance methods (the
  framework gate methods are final / auto-wrapped — domain checks live inline or
  in module-level helpers). Two independent layers, in this order:
  1. a credential- and personal-identifier pattern scan over **every string**
     in the response — nested values and mapping **keys** alike. Text riding one
     level down is on the external surface just as much as a top-level string,
     and a key carries text exactly as a value does. The framework's own
     `detect_credentials_in_value` runs alongside the local pattern as the
     **floor**: it knows `AKIA`, `sk_live_` and connection strings that the
     local pattern does not, while the local pattern reads keys, which it does
     not. Union, never replacement;
  2. the external precision grid (below).

  The scan runs **before** the grid and again after it, because snapping a digit
  run can destroy the shape a pattern scan matches on.

  **Every** non-success return — a gate violation **and** a pre-existing
  inner-workflow error arriving from the subgraph — goes through one
  module-level `_contain()` helper: `status=error`, **every output-bearing field
  cleared** (`result`, `confirmation`, `summary`, `company_name`,
  `speeda_payload`, `intent`, `record_id`, `record_ref`, `company_id`), and a
  truthy replacement `formatted_output`. Raising, or returning an error while
  leaving those fields in place, is not containment:
  `AgentBaseGraph.get_output()` projects `formatted_output or result` with **no
  status check**, so the un-gated inner answer would ship inside the error
  envelope.

  **The caller-visible error carries closed-set labels only:**

  ```
  {"reason": "<one of ERROR_REASONS>"}
  ```

  `reason` is one of `speeda_workflow_failed` / `output_withheld_by_gate`,
  chosen by the module and never derived from state. It is a constant, which
  keeps the mapping **truthy** — a falsy `formatted_output` would re-open the
  `or result` projection this containment exists to prevent.

  Nothing else is in it. `error_log` is **not** projected, and neither are the
  gate's violation entries: both are node-authored strings that can embed an
  upstream SPEEDA response body, an identifier, a name or caller-derived text,
  and truncating them to a first line is not a closed set. `error_log` stays the
  **internal** channel — the state reducer appends to it and the audit trail
  needs it. Inner entries are never re-emitted from `post_process` (the reducer
  appends, so re-emitting duplicates every line); gate violations are written
  there naming the offending **path**, with a credential-shaped mapping key
  withheld from the label rather than quoted by the entry that reports it. The
  `post_process_error_contained` / `post_process_output_blocked` audit events
  carry the reason code, the intent label and a **count** — the count belongs
  there, not in the envelope.

  **No error envelope names a record** either. `record_id` / `record_ref` are
  this agent's lookup evidence — the gate *refuses* a SUCCESS that lacks them —
  so returning them under an ERROR status would tell a caller being informed of
  failure that a company record was nonetheless resolved, and which company.

  **Upstream reasons are closed-set labels too**, because `error_log` is the
  audit record and a future reader of it should find no third-party text.
  `call_speeda_api` reports the record **type** for a not-found, the **HTTP
  status** for an API error, and the **exception type** for a transport failure
  — never the company code, the upstream body or the request URL. The status and
  the class name are bound to locals first, so no caught exception is ever
  interpolated into an f-string: `SpeedaApiError` composes its own message from
  the upstream response body, and `str(exc)` on it would republish that body.
- **Audit** — every node's `execute()` emits exactly one positional
  `emit_trace_event("<node>_complete", {small non-identifying payload}, state)` on
  its success path (intent / presence signals only — never request text, company
  content, or credentials); a blocked response emits
  `post_process_output_blocked`. `__call__()` is never overridden.

  | Node | Event |
  |------|-------|
  | pre_process | `pre_process_complete` |
  | validate_input | `validate_input_complete` |
  | classify_intent | `classify_intent_complete` |
  | infer_speeda_fields | `infer_speeda_fields_complete` |
  | call_speeda_api | `call_speeda_api_complete` |
  | confirm | `confirm_complete` |
  | post_process | `post_process_complete` / `post_process_output_blocked` |

## External output schema — the precision grid

The response reports **aggregates**: every monetary figure it renders is
expressed in units of 1,000. `formatted_output.schema_note` states this in the
response itself. The renderer produces figures on the grid; the output gate
independently ENFORCES it, so a rendering regression cannot put a
full-precision figure on the external surface. An off-grid value is snapped and
counted in the audit payload.

A monetary value is identified by FORM and by CURRENCY CONTEXT, never by
magnitude — a magnitude exemption lets an exact small figure through:

| Form | Example |
|---|---|
| comma-grouped number | `1,234,567` |
| run of 5+ digits | `revenue 12345678` |
| 1–4 digits in currency context | `JPY 9999`, `9999 JPY`, `¥9999`, `9999円`, `JPY-9999`, `JPY  +9999`, `JPY\n9999` |

Grammar notes, each of which exists because of a specific failure:

- **Group-based, no lookbehinds.** Python lookbehinds are fixed-width, which
  silently caps a "spaced" marker at one whitespace character. The marker, the
  delimiter and the value are matched as groups, and all three are preserved on
  the snap.
- **The comma-grouped alternative comes first.** Matching is leftmost-first, so
  without it `JPY 1,234` matches as marker + `1` and the snap corrupts the number
  to `JPY 0,234`.
- **The delimiter stays inside one line** (`[ \t]*(?:\n[ \t]*)?`). A `\s*`
  delimiter spans a paragraph break, so a 3-letter uppercase word ending a line
  binds to the number opening the next block: `Currency: JPY\n\n3. Cash Position`
  becomes `0. Cash Position` — the gate rewriting document structure.
- **Each value alternative absorbs its own decimal fraction**, written
  `(?:\.\d+|(?!\.\d))`. Without absorption the fraction of `8.512345` is a
  standalone 5+-digit run and gets rewritten (`8.512,000`), and a decimal amount
  snaps its integer part while the fraction dangles (`JPY 1234.56` ->
  `JPY 1,000.56`). Written as an optional `(?:\.\d+)?` the engine can backtrack
  out of the fraction and re-match the integer alone whenever what follows fails
  the trailing guard, so `JPY 1234.56m` regains the same defect; the `(?!\.\d)`
  arm has no optional branch to backtrack through.
- **Identifier guards widened to this template's render alphabet.** The generic
  guard class `[A-Za-z0-9-]` is not enough here: the identifiers this agent
  renders are also delimited by `_`, `/`, `:`, `=` and quotes
  (`speeda://companies/1234567`, `id=1234567`, `'1234567'`, `sku_48210`). Without
  those characters in the class, a company code that happens to be a 5+-digit run
  is read as a monetary figure and rewritten — the agent would report a record id
  that does not exist. `.` is in the LEADING guard only; in the trailing guard it
  would let an amount ending a sentence escape the grid.
- **An ATTACHED 3-letter marker must be a currency code.** `SKF-6205`,
  `STU-1234` and `JPY-9999` are the same shape, so form alone cannot separate an
  identifier from an attached negative amount. The attached case is therefore
  restricted to a list of currency codes — a closed, stable set, unlike a list of
  identifiers, which could never be complete. A **separated** marker is not
  restricted: `SKF 6205` still snaps, on the same fail-safe rule as the rest of
  the grammar.
- **Identifier fields are outside the grid.** `record_id`, `record_ref`,
  `company_name` and `intent` are validated identifier shapes that never express
  an amount; rewriting a digit run inside one would corrupt the record reference
  the caller needs. Every other field is gridded.

Structural tokens stay byte-identical: fiscal years (`fy2025`), bare years,
page references (`p.21`), versions (`v12`), horizons (`90d`), embedded acronyms
(`STAR 2026`), industry codes (`ind-a1b2`), and any already-on-grid amount
(`JPY 1,000`).

## Implementation note — model-backed synthesis

The pipeline is fully deterministic: intent classification (`ClassifyIntentNode`)
uses a keyword heuristic and field inference (`InferSpeedaFieldsNode`) uses
regex/line-structure extraction, so the template runs and tests without a model
backend. **No model client is constructed anywhere** and no `system_prompt` is
read (no dead config). Model-backed synthesis (richer intent classification,
free-text-to-field mapping, natural-language research narratives) is a documented
follow-up: `_parent_config()` forwards the whole runtime document, so any section
added to `config/config.yaml` reaches the inner graph without a code change.

## Limitation — SPEEDA client (documented)

`src/services/speeda_client.py` ships a **deterministic, network-free stub** as
its default transport: it returns the documented SPEEDA response shapes (a
`company` object for profile lookups; an `industries` list for classification
lookups; a `financials` list for highlights — synthetic values derived from the
request) so the pipeline is runnable and testable without a live SPEEDA
subscription or an HTTP client library. It does **not** perform a live SPEEDA
call. To go live, inject a real `get` transport at construction; the method
contracts and parameter shapes follow the SPEEDA REST API, so no business-logic
change is required. (The stub also runs without a live credential — see Secrets
above; a live transport requires `SPEEDA_TOKEN`.) The client is read-only by
design: it exposes no create/update/delete method, so the template cannot mutate
the external system.

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallSpeedaApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] Trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallSpeedaApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] Secrets — `ctx.secrets.get("SPEEDA_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`
- [x] `emit_trace_event()` — one positional call per node on the success path; framework lifecycle events (node_start/node_complete/node_error) NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `SpeedaWorkflowGraph` (`BaseGraph`) via `SpeedaWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: `SpeedaWorkflowGraphNode._parent_config()` forwards the
  whole `config/config.yaml` document under `config["configurable"]`.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Import Isolation Confirmation
- [x] Template imports `framework/` and `shared/` only; no platform-SDK import anywhere
- [x] `src/services/speeda_client.py` has no framework imports (pure service
      layer, stdlib only)
- [x] `src/services/security.py` imports exactly one L1 symbol,
      `framework.security.credential_detector.detect_credentials`, so that
      `withhold_credentials()` satisfies the framework's own detector — the gate
      that raises on a node result — rather than a second, narrower local copy
      of its pattern list. Everything else in the module is stdlib.

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat; 5 domain steps live in the inner graph |
| Model dependency | model client in the pipeline | deterministic, model-backed synthesis as a documented follow-up | deterministic | template runs/tests without a backend; no dead prompt/config reads |
| SPEEDA client | live HTTP call | injectable transport + documented network-free default | injectable + network-free default | never fake a live call; document the limitation; go-live is a transport injection, no logic change |
| Node configuration | ctor-arg dependency injection | no-arg nodes + config forwarding via `_parent_config()` -> `configurable` -> state | no-arg nodes | nodes are no-arg (ctor args raise at graph build); the config file stays the single source |
| Caller context channel | free-text fields | inert identifiers only | inert identifiers | free text there is caller-controlled output injection, and it is what makes a credential-shaped value fail the first framework node |
| Research target | infer company code from NL freely | caller-supplied/explicit code only; unresolved left empty | explicit only | never research the wrong company record; unresolved code -> status=error, not invented |
| Write capability | expose create/update endpoints | READ-ONLY client (lookups only) | READ-ONLY | research domain — no mutation exists; the client exposes no write method by design |
| Attached currency marker | any 3-letter uppercase word | currency codes only | currency codes only | `SKF-6205` and `JPY-9999` are the same shape; a closed currency list resolves the ambiguity where an identifier list never could |
| Output gate on violation | raise / return error | return error AND clear output fields | clear output fields | `get_output()` falls back to `state["result"]`, so anything less ships the un-gated answer inside the error envelope |
| Error envelope contents | echo the record so the caller can correlate | fixed reason code, no record evidence | fixed reason code | `record_id`/`record_ref` are the lookup evidence the SUCCESS gate demands — naming them under an ERROR status discloses that the record WAS resolved, and which one. Correlation stays server-side via `correlation_id` |
| Error diagnostics to the caller | summarised `error_log` / gate violations under an `error` key | reason code alone; `error_log` stays internal | reason code alone | a summarised line is still an arbitrary node-authored string and can embed an upstream response body, a name or an identifier — truncation is not a closed set. Diagnostics stay on `error_log` and in the audit events, which `get_output()` does not project |
| Not-found reason | name the code that was not found | name the record TYPE only | record type only | `error_log` is the audit record of the run; a reason carrying the code writes the record evidence into it, and one `str(exc)` away from the caller |
| Default intent | summarize_financials | lookup_company | lookup_company | low-confidence classification defaults to the cheapest, most general lookup |
