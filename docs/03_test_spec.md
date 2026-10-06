# Test Specification - CMN-C2-287 SPEEDA Company Research Agent

## Test Strategy

- Test types: Unit (per node + service + config + inner graph) / Proof-of-Boundary
  (full outer-graph invoke, the HTTP entry, import isolation, state safety, server
  boot, HITL stub). `tests/integration/` is an empty package; end-to-end backbone
  coverage lives in the boundary suite, which drives the real compiled graph and
  the real ASGI application.
- The SPEEDA call is exercised through the deterministic, network-free default
  transport and through injected fake clients; no live SPEEDA call is made.
- **READ-ONLY research domain**: every intent (lookup_company / lookup_industry /
  summarize_financials) is a GET-shaped lookup. The suite proves the read-only
  posture explicitly - the client's public surface is exactly the three `get_*`
  lookups (no create/update/delete/post/patch method exists) and any non-lookup
  intent is refused by CallSpeedaApiNode ("unknown intent"), never executed.
- **Trust-gate routing canon**: per-node unit tests invoke the node as
  `node(state)` - `BaseNode.__call__` routes the full security pipeline (trust
  gate -> input mask -> `execute()` -> output scan) - never bare
  `node.execute(state)`. State builders set `caller_trust_level =
  TrustLevel.VERIFIED_EXTERNAL.value` for PreProcessNode (the single external
  gate) and `TrustLevel.ANONYMOUS.value` for every other node. The trust-rejection
  test asserts on the RETURNED error dict (`status == AgentStatus.ERROR.value`,
  "trust gate denied" in `error_log`, execute-only keys absent) - `__call__` never
  raises for a trust denial.
- **Two documented `execute()`-direct exceptions**, both deliberate:
  1. `CallSpeedaApiNode.execute(state, config=...)` takes a second argument
     `__call__` cannot forward;
  2. **the input-screen and caller-contract refusals** are asserted on
     `execute()` with no framework wrapper in front. A refusal that holds only
     because a framework gate happens to be active is not a guarantee the
     template owns; where that gate is absent or configured off, the payload
     would reach the answer path and return success.
- Assertion contract: the invoke surface is `result["output"]` / `status` /
  `trace_id` / `correlation_id` / `node_history` (never `formatted_output` at the
  invoke surface); status is compared to `AgentStatus.SUCCESS`/`.value` (lowercase
  `success`/`error`); the outer graph is called as `invoke(user_input=..., ctx=...,
  input_context=...)`; identifiers may be masked (`[MASKED]`) so record evidence is
  asserted by presence, not raw repr; audit spies assert on `call.args[1]` (the
  event payload), never the whole-call repr.
- Framework pipeline behaviours encoded by the suite: `__call__` short-circuits on
  an incoming errored state (execute() is skipped; error status/error_log pass
  through); the framework's input mask rewrites Title-Case bigrams (across
  newlines), emails, and digit groups in `user_input`/`validated_input` to
  `[MASKED]` before `execute()` sees the text - positive payloads therefore carry
  no personal data (lowercase synthetic company names, codes like `c-7203`), and
  the intentional-personal-data tests assert the `[MASKED]` path.
- Domain audit events are muted per module via an autouse fixture patching
  `src.nodes.<mod>.emit_trace_event` (never a `sys.modules` stub of `shared.*`).

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_trust_gate.py | trust boundary: ANONYMOUS caller on the VERIFIED_EXTERNAL pre_process gate; trust-order monotonicity (INTERNAL passes; inner nodes accept ANONYMOUS and the forwarded unelevated VERIFIED_EXTERNAL); trust-posture declarations | denial RETURNS an error dict (`status == AgentStatus.ERROR.value`, "trust gate denied" in error_log, execute-only keys absent); VERIFIED_EXTERNAL/INTERNAL pass; every inner node declares ANONYMOUS |
| U-02 | test_pre_process_node.py | serialize NL request + target into `validated_input` (JSON); markup strip; hint priority company_id > company_hint > company_code > ticker; **caller-context bounds** (identifier shape, unknown/oversized fields, hostile field names, every supplied field validated); **injection screen** on `execute()` directly, attacks refused and ordinary research wording unaffected | hint resolved by priority; `<script>` stripped; empty/missing -> error; malformed context -> error naming the field and never echoing the value; each attack class -> error, nothing carried forward |
| U-03 | test_validate_input_node.py | empty/short guard; JSON-shaped input; framework `[MASKED]` path for emails; node-level token flag-and-redact (`secret_*`); the inner graph's own injection screen | email -> `[MASKED]` before execute; token -> `[REDACTED]` + `redaction_flags=["token"]` (JSON string); empty/short -> error; attacks refused on `execute()`; audit payload carries flags only |
| U-04 | test_classify_intent_node.py | intent = lookup_company / lookup_industry / summarize_financials (keyword, most-specific-first priority, read-only default) | correct intent per keyword; no-signal defaults to lookup_company with a non-fatal note; empty -> error; audit emits the intent label only |
| U-05 | test_infer_speeda_fields_node.py | company-code resolution (text > code-shaped hint > `Code:` field; never invented); quoted name; `Key: value` filter fields and their **bounds** (count cap, length cap, alphabet, dropped-count audited); GET parameter set per intent | `{company_code, detail}` (+`filters`) per intent; unresolved code left `""`; empty input -> error; out-of-bounds field dropped, never truncated |
| U-06 | test_call_speeda_api_node.py | profile/industry/financials lookups via the network-free transport; `speeda_config` state field + `execute(state, config=...)` override; API error / empty record / unresolved code / unknown intent / missing payload; secret posture (a live transport refuses to run unauthenticated; token via `ctx.secrets`, never env/state); **call budget** (retry count honoured, 4xx never retried, non-finite/out-of-range settings fail closed); upstream messages and exception text never interpolated into the error log | record_id/record_ref on success; 403 surfaces as a status code with no upstream body; empty `company` -> "no matching company record found" (record TYPE only, never the code); non-lookup intent -> "unknown intent"; live+no-secret -> "unauthenticated"; live+bound secret -> token passed to the client; retries stop at the declared count |
| U-07 | test_confirm_node.py | human-readable confirmation per intent verb; summary + ref/id formatting; name fallback | "Retrieved company profile / Retrieved industry classification / Summarized financial highlights ... ref=... id=..."; missing evidence -> error |
| U-08 | test_post_process_node.py | `formatted_output` shaping; request described not echoed; grid applied to the narrative; errored state passes through `__call__` (short-circuit); the error envelope publishes the reason code and nothing else; inner `error_log` entries not re-emitted; record-evidence gate; **containment** (a violation clears every output-bearing field and the violations stay on `error_log`); the module-level gate helper as a plain function incl. nested credential scan and scan-before-grid ordering | success shape with `schema_note`; filter values absent from the response; error status/error_log preserved with no success shape fabricated; SUCCESS without record_id/record_ref blocked and `result`/`formatted_output` cleared |
| U-09 | test_speeda_client.py | client: get_company / get_industry / get_financials; `Authorization: Bearer` header; `SpeedaApiError` on non-2xx (`errors` join + `message` fallback); default-transport shapes (`company` / `industries` / `financials`, deterministic); `uses_stub_transport`; **read-only surface proof** (no mutation methods); token never persisted on the instance | correct URLs/headers/params; 404/500 raise; deterministic shapes; public callables == exactly the three `get_*` lookups |
| U-10 | test_config.py | `config/agent.yaml` (flat manifest) + `config/config.yaml` (runtime) | manifest is flat (no `agent:` block), id CMN-C2-287, Cat 2, CMN, ToolCallingAgent, namespace cmn, single dotted entry point, VERIFIED_EXTERNAL, `requires.secrets == []` and `requires.extras == []`, `generation_mode: deterministic`; runtime holds `speeda.base_url`, `max_retry`, `timeout_s` (and no `timeout_seconds`) |
| U-11 | test_domain_workflow_graph.py | inner `SpeedaWorkflowGraph`: identity, `_extra_initial_state()` settings injection as JSON, **runtime-config validation** (non-finite / out-of-range `max_retry`/`timeout_s` raise `ConfigError` at compile), `route()` error short-circuit **and its State annotation**, `get_output` contract, compile, direct inner invoke | name/state_schema correct; settings forwarded as a JSON string with documented defaults; invalid numbers fail the build; `route` annotated with this graph's `State`; inner invoke runs validate -> classify -> infer -> call -> confirm to SUCCESS with record evidence |
| U-12 | test_security_screens.py | `src/services/security.py`: injection screen (control-token class, instruction override, role hijack, prompt exfiltration) both directions; sanitizer; identifier validation and non-echoing errors; hostile field-name masking incl. a credential-shaped name inside the inert alphabet; the outbound `walk_strings` (values, list indices and mapping KEYS) and `withhold_credentials` (local pattern, framework-only shape, ordinary label untouched); the finite+bounded number parser (NaN / ±Infinity / bool / non-numeric / out-of-range) | each attack class detected; domain wording untouched; both representations proven necessary; identifiers inert; keys scanned as text; a credential-shaped fragment replaced by `<withheld>` under both detectors; every non-finite input rejected fail-closed with the field named |
| U-13 | test_output_precision_gate.py | the external precision grid, 19 leak forms and 24 structural tokens | every leak form snaps onto the grid with marker/delimiter/sign preserved; every structural token (identifiers, decimals, years, page/version tags, paragraph breaks) byte-identical; no magnitude exemption; identifier fields skipped; nested values walked; pattern scan finds top-level, nested and list-borne credentials |
| U-14 | test_framework_compliance_tc06_tc07.py | the framework input/output gates are non-bypassable | overriding either default gate raises `TypeError` at class definition |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: no direct platform-SDK import |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + external trust | test_pb_invoke_order.py | `_VALID_PAYLOAD` byte-equal to `deploy/invoke_payload.json` "input" (asserted); VERIFIED_EXTERNAL caller yields `status=success` with `node_history == [InitializeNode, PreProcessNode, SpeedaWorkflowGraphNode, PostProcessNode, FinalizeNode]`, record evidence, confirmation and `schema_note` in `result["output"]`; ANONYMOUS caller denied at pre_process (error, no post_process, no output); blank input -> error, not crash |
| PB-8 | HTTP caller contract | test_pb_http_invoke.py | the real ASGI app driven end to end: Bearer auth boundary (401 on absent/wrong token, generic body); caller data on `input_context` reaches the inner graph and two targets give two different answers; all three read-only intents reachable; financial figures on the grid and record identifiers untouched; filter values not echoed; malformed/unknown/oversized context refused at the adapter with the field named and the value never echoed; unresolved code errors rather than inventing a record; each injection form refused with nothing published; a blocked response carries no released text, no traceback and no source paths; a sentinel seeded in a gate violation and a sentinel seeded in an inner-node reason both appear **nowhere** in the invoke body (walked through nested keys and values), and a blocked response's `output` is exactly `{"reason": "output_withheld_by_gate"}` |
| PB-9 | Error-path containment at the output boundary | test_error_envelope_no_record_evidence.py | on EVERY non-success path (inner-workflow error, missing record evidence, credential-shaped value, personal-identifier value) `post_process` returns a **truthy** envelope that is exactly `{"reason": <member of ERROR_REASONS>}` — a falsy value would re-open the framework's `formatted_output or result` projection, and any further key is node-authored text — carrying no company code, `speeda://` reference, company name or research summary; a sentinel seeded in `error_log` appears **nowhere** in the returned mapping, walked through nested keys and values; inner `error_log` entries are not re-emitted (the state reducer appends); the delta CLEARS every output-bearing field (`result`, `confirmation`, `summary`, `company_name`, `speeda_payload`, `intent`, `record_id`, `record_ref`, `company_id`); gate violations remain on `error_log`, with a credential-shaped mapping KEY withheld from the label that reports it (and an ordinary key still named, so the label stays actionable); the framework detector is proved to be the FLOOR beside the local pattern, not a replacement; `mask_field_name` withholds a credential-shaped context field name even when it fits the inert identifier alphabet; the reasons `call_speeda_api` writes carry closed-set labels only (record type, HTTP status, exception type — never the company code, the upstream body or the request URL); a success-path control proves the containment did not empty the clean path |
| PB-7 | HITL interrupt propagation *(conditional)* | test_pb7_hitl_interrupt_propagation.py | **Auto-waived - non-HITL** (`config/agent.yaml` declares no `hitl.enabled: true`): module-level skipif; stub bodies are real AssertionErrors so enabling HITL without implementing PB-7 fails loudly |
| PB (boot) | Server entry point | test_server_boot.py | importing `src.api.server` does not raise (construct + compile + provision_secrets at import); the agent constructs + compiles via the supported path; `/invoke` + `/health` routes exposed |

> PB-1 (audit emission) is covered inside the unit suite via the emit-spy tests
> (validate / classify / infer / call nodes assert on the event payload,
> `call.args[1]`). PB-3 (a live external service) is not exercised here - the
> default transport is the documented network-free one.

## Test Execution Summary

- Runner: `python -m pytest tests/ -q` against the framework wheel the pipeline
  installs (`agenticstar-agentcore==1.0.2`).
- Total tests: 350
- Pass: 348 / Fail: 0 / Skip: 2 (PB-7 A/B - auto-waived, non-HITL)
