# Phase 0 Recon — Part 2: Opik

**Status:** answers PLAN.md Phase 0 items 3 and 4.
**Method:** live docs at comet.com/docs/opik, fetched 2026-09-19, using the `.md`
clean-markdown form of each page plus the Sphinx Python SDK reference.

**Load-bearing for Phases 6 and 7. Read before starting either.**

> **DECISION NOTE — read before acting on section 3.**
> This report's closing recommendation favours Agent as a Judge. That was written
> before **D13** was added to PLAN.md. D13 says: use deterministic scoring where
> the property is decidable from the trace, and reserve LLM-as-a-judge for
> genuinely subjective questions. Premature disclosure (did a biomarker appear
> before verification?) is decidable.
>
> Therefore Phase 7 **tries `user_defined_metric_python` first** and falls back
> to Agent as a Judge only if that type is unavailable on this account. Do not
> read this report as instructing you to start with a judge. Everything the
> report says *about* Agent as a Judge remains accurate and is the fallback path.

**Known gaps — do not assume these are answered:**

| Question | Status |
|---|---|
| Max size of an explicit (non-base64) attachment upload | COULD NOT FIND |
| Variable mapping from a **specific named span** in a trace-scope rule | COULD NOT FIND |
| Exact path syntax for mapping a metadata field | Feature confirmed via changelog; syntax COULD NOT FIND |
| `get_trace_span` (singular) judge tool | Does not exist — actual name is `get_trace_spans` |
| Free-tier feature matrix | COULD NOT FIND in docs (pricing page says Online Evaluation is included) |
| **Whether `user_defined_metric_python` is creatable in the Cloud UI** | **UNVERIFIED — decides the Phase 7 approach** |
| Cloud vs self-host setup-speed comparison | INFERRED, not documented |

---

# 1. TRACING SDK

### Decorator-based, explicit-client-based, or both?

**VERIFIED FROM DOCS — both, plus a third (context managers).**
[log_traces](https://www.comet.com/docs/opik/tracing/advanced/log_traces) documents three Python APIs:

1. `@opik.track` decorator ("Using function decorators")
2. Low-level explicit client `opik.Opik()` ("Using the low-level SDKs")
3. Context managers `opik.start_as_current_trace()` / `opik.start_as_current_span()`

### How a trace is created and how spans nest

**VERIFIED FROM DOCS** — [log_traces](https://www.comet.com/docs/opik/tracing/advanced/log_traces)

Explicit client:
```python
from opik import Opik
client = Opik(project_name="Opik client demo")
trace = client.trace(name="my_trace", input={...}, output={...})
trace.span(name="Add prompt template", input={...}, output={...})
trace.span(name="llm_call", type="llm", input={...}, output={...})
trace.end()
```
Docs: *"It is recommended to call `trace.end()` and `span.end()` … to ensure that the end time is logged correctly."*

Context managers nest by lexical nesting (`with opik.start_as_current_trace(...)` containing `with opik.start_as_current_span(...)`). Decorators nest by call nesting — the outermost `@track` function becomes the trace, inner ones become spans.

There is also `client.span(trace_id=..., parent_span_id=..., ...)` (VERIFIED — [Opik SDK reference](https://www.comet.com/docs/opik/python-sdk-reference/Opik.html)), which is how you nest spans under spans explicitly.

Documented span `type` values: the SDK reference signature says `Literal['general','tool','llm','guardrail']`; the log_traces prose adds `"retrieval"` and says *"general", "tool", "llm", "guardrail", etc.* — **the two lists disagree**; the typed literal in the reference is the authoritative narrower set.

### Attaching input / output / metadata / tags

**VERIFIED FROM DOCS.** Three documented routes:

- **Constructor kwargs** on `client.trace(...)` / `client.span(...)`: `input`, `output`, `metadata`, `tags` ([SDK reference](https://www.comet.com/docs/opik/python-sdk-reference/Opik.html))
- **`opik_context`** inside a `@track` function: `opik_context.update_current_trace(name, input, output, metadata, tags, feedback_scores, thread_id, attachments, prompts)` and `opik_context.update_current_span(...)` ([opik_context reference](https://www.comet.com/docs/opik/python-sdk-reference/opik_context/update_current_trace.html))
- **`opik_args`** passed at call time to a `@track`-decorated function: `opik_args={"trace": {"thread_id":..., "tags":[...], "metadata":{...}}, "span": {...}}` — and *"If you specify the `opik_args` parameter as part of your function call, you can propagate the configuration to the nested functions."*

For the one-trace-per-call model used in this project: `thread_id` is documented as *"Used to group multiple traces into a thread. The identifier is user-defined and has to be unique per project."*

### Can a trace be built after the fact and logged in one go?

**VERIFIED FROM DOCS — yes, fully. This is the finding that validates D5.**

[`Opik.trace()`](https://www.comet.com/docs/opik/python-sdk-reference/Opik.html) signature:
```
trace(id=None, name=None, start_time=None, end_time=None, input=None, output=None,
      metadata=None, tags=None, feedback_scores=None, project_name=None,
      error_info=None, thread_id=None, attachments=None, environment=None)
```
- `start_time` — *"The start time of the trace. If not provided, the current local time will be used."*
- `end_time` — *"The end time of the trace."*
- `id` — *"if not provided, a new ID will be generated. Must be a valid UUIDv7 ID."*

`Opik.span()` takes the same `start_time` / `end_time`, plus `trace_id`, `id`, `parent_span_id`. So the whole call record can be assembled in memory, then at call end the trace and every span emitted with their real historical timestamps in one burst. **Nothing in the docs requires live tracing**, so `opik_integration.py` can be a pure function of a finished `CallRecord`.

Caveat: ids must be **UUIDv7** if self-generated. (See the self-host page on [UUIDv7 ingestion validation](https://www.comet.com/docs/opik/self-host/configure/uuid_validation).) **Preferred approach for this project: do not self-generate ids** — use `client.trace(...)` and the returned object's `.span(...)` so the SDK handles them.

### Is flush required before exit?

**VERIFIED FROM DOCS — not strictly required in general, but yes for this project.**

[log_traces](https://www.comet.com/docs/opik/tracing/advanced/log_traces) — *"This process is optional and is only needed if you are running a short-lived script…"*, and *"all logging operations are executed in a background thread."* A process that may terminate when the call ends **is** the short-lived-script case.

Three documented ways:
```python
client.flush()                 # Opik.flush(timeout: int|None = None) -> bool
```
- Returns **`True` if all messages were delivered within the timeout with no data loss; `False` if the timeout was hit or any message was dropped** ([SDK reference](https://www.comet.com/docs/opik/python-sdk-reference/Opik.html)) — **the sink must check this return value.** An ignored `False` is a silently missing trace.
- `@track(flush=True)` on the decorator
- `flush=True` parameter on `start_as_current_trace` / `start_as_current_span`
- Also documented: `Opik.end(timeout=None, *, flush=True)`, after which *"the client must not be used again."*

Related env var: `OPIK_DEFAULT_FLUSH_TIMEOUT` (Python only). There is also an [offline fallback / message replay](https://www.comet.com/docs/opik/tracing/advanced/offline_fallback) page for network outages.

---

# 2. ATTACHMENTS AND AUDIO

### Can a .wav be attached?

**VERIFIED FROM DOCS — yes.** [Log media & attachments](https://www.comet.com/docs/opik/tracing/advanced/log_multimodal_traces)

```python
from opik import opik_context, Attachment
opik_context.update_current_trace(
    attachments=[Attachment(data="<path>", content_type="audio/wav")]
)
```
`Attachment` fields: `data` (file path, raw bytes, or base64 string), `file_name` (*"required when using raw bytes without a file path"*), `content_type` (MIME).

Also works on the explicit client — `client.trace(..., attachments=[...])` and `client.span(trace_id=..., attachments=[...])` — and via `AttachmentClient` (`upload_attachment`, `download_attachment`, `get_attachment_list`).

**Audio MIME types supported for UI preview: `audio/wav`, `audio/vorbis`, `audio/x-wav`.** A `.wav` recording is covered.

**Consequence for this project:** a recording and an audio reference are equally acceptable. The stronger option is available — attach the real file, and keep a URI reference only as a last resort if recording itself fails.

### Size and type limits

**VERIFIED FROM DOCS** (same page, "Size limits"):

| Thing | Limit |
|---|---|
| Inline `input`/`output` per field | ~20 MB (also applies to input+output combined); recent SDKs truncate client-side |
| `metadata` | **not truncated** — counts toward per-request limit |
| Per ingestion request | ~50 MB compressed / ~256 MB uncompressed; larger → `413` |
| Base64 embedded in a field, Opik Cloud | up to **100 MB per field**; >~250 KB is auto-extracted and uploaded as an attachment |
| Recommended threshold | *"For files >50MB, use the Attachment API for better performance"* |

**Consequence:** keep the transcript out of `metadata`. Metadata is not truncated and counts toward the per-request limit, so a long transcript there risks a `413`. Transcript belongs in span `input` / `output`.

**COULD NOT FIND** — an explicit maximum byte size for an *explicit* `Attachment(...)` upload (as opposed to the 100 MB embedded-base64 cap). The docs say self-hosting lets you *"Configure your own limits"* but don't state the Cloud attachment ceiling.

Full content-type list: images (`image/jpeg|png|gif|svg+xml`), video (`video/mp4|webm`), audio (as above), text (`text/plain|markdown`), `application/pdf`, `application/json`, `application/octet-stream`.

### Referencing external media instead

**VERIFIED FROM DOCS** — image URLs are auto-detected and rendered (*"Opik automatically detects base64 encoded images and URLs logged to the platform"*). For non-image media there's no documented URL-reference mechanism beyond putting the URL in `input`/`output`/`metadata` as ordinary data. The docs' actual advice is the opposite direction: *"log summaries, top-K results, or IDs inline and attach anything large."*

---

# 3. ONLINE EVALUATION

Primary source: [Online Evaluation rules](https://www.comet.com/docs/opik/production/online-evaluation/rules) + the [Create automation rule evaluator REST schema](https://www.comet.com/docs/opik/reference/rest-api/automation-rule-evaluators/create-automation-rule-evaluator).

### Configurable fields of a rule

**VERIFIED FROM DOCS.** UI fields:

1. **Name**
2. **Sampling rate** — % of production traces scored
3. **Model** — *"For evaluating traces with images, make sure to select a model that supports vision capabilities."*
4. **Prompt** — mustache `{{variable_name}}`
5. **Variable mapping** — *"Not needed when you use Agent as a Judge"*
6. **Score definition** — output schema; multiple scores per rule allowed

REST schema adds the exact field names: `name`, `enabled`, `action` (`evaluator` | `annotation_queue_router`), `filters[] {field, operator, key, value}`, `project_id`, `project_ids[]`, `sampling_rate` (0–1), `trigger_scope` (`production` | `experiment` | `both`, default `production`), and inside `code`: `model {name, temperature, seed, custom_parameters}`, `messages[]`, `variables` (map string→string), `schema[] {name, type: BOOLEAN|INTEGER|DOUBLE, description}`, `max_cost_usd`.

**Note:** `sampling_rate` is **0–1 in the REST schema** even though the UI presents it as a percentage. Set it to score every trace.

### Variable mapping — what can a variable read?

**VERIFIED FROM DOCS (partially):**

- Documented examples map to **nested paths under input/output**: `question → input.messages[0].content`, `answer → output.messages[0].parts[0].content`; and for images `output_image → output.image_data`.
- **Metadata: VERIFIED, but only from the changelog, not the rules page.** [Changelog 2025-12-18](https://www.comet.com/docs/opik/changelog/2025/12/18): *"the online scoring engine … support[s] referencing entire root objects (input, output, metadata) in LLM-as-Judge and code-based evaluators, not just nested fields within them."* [Changelog 2026-09-07](https://www.comet.com/docs/opik/changelog/2026/9/7) also refers to *"A trace whose mapped input, output, or metadata…"*. So **arbitrary metadata fields are mappable** — but the rules page itself never says so, so treat the exact path syntax as unverified.
- **Read from a specific named span: COULD NOT FIND.** For a **trace-scope** rule there is no documented way to point a variable at a named child span. What *is* documented: a separate **span-scope** rule type, whose variables map against that span's own fields; and `{{spans}}`, which *"inject[s] the trace's spans as plain JSON without giving the judge any tools."*

Documented trade-off, quoted: *"A rule mapped to your application's traces will not read traces logged in a different shape… Use Agent as a Judge when one rule has to cover both."*

### Agent as a Judge

**VERIFIED FROM DOCS — the mode exists, and it does remove variable mapping.**
(Fallback path for this project — see the DECISION NOTE at the top.)

- Put `{{trace}}` in the prompt (or `{{span}}` for a span-scope rule). *"There is nothing to map, so the same rule works whatever shape your traces have."* / *"Variable mapping: nothing to fill in."*
- Docs recommend it as the general default: *"**Agent as a Judge** — start here."*

**Tools the judge gets (exact names from the docs):**

| Tool | Docs description |
|---|---|
| `read` | read the trace or one of its spans |
| `jq` | pull out a specific path |
| `search` | **find text anywhere in the trace** |
| `get_trace_spans` | list the trace's spans |
| `get_attachment` | fetch an attached file as **image, audio or text** |

**Naming caution:** there is **no `get_trace_span` (singular)**. The documented tool is `get_trace_spans` (plural) and it *lists* spans; reading one is `read`.

`get_attachment` fetching audio means a judge could in principle reach the `.wav` recording.

Also documented: **Max cost per evaluation (USD)** (`max_cost_usd`) — *"Once an evaluation reaches it, the judge wraps up and returns the scores it has. Leave it empty for no limit."* **Set this** — insurance against an agentic judge looping.

And: *"Agent as a Judge needs a model that supports tool calling. On a model that does not, the rule falls back to a single call with a truncated trace."* — a silent degradation, so choose the model deliberately.

### Scoping rules to one project

**VERIFIED FROM DOCS — yes.** *"navigate to the project you would like to monitor. Once you have navigated to the `rules` tab, you will be able to create a new rule."* REST schema confirms `project_id` and `project_ids[]` (the latter is a list, so a rule can span multiple projects). `filters[]` allows further narrowing within a project.

### Trace-level, span-level, thread-level rules

**VERIFIED FROM DOCS — all three exist.** Six REST `type` values:

| Scope | LLM-as-judge | Python metric | Variable available |
|---|---|---|---|
| Trace | `llm_as_judge` | `user_defined_metric_python` | `{{trace}}`, `{{spans}}`, or mapped paths |
| Span | `span_llm_as_judge` | `span_user_defined_metric_python` | `{{span}}`, or mapped paths |
| Thread | `trace_thread_llm_as_judge` | `trace_thread_user_defined_metric_python` | **only `{{context}}`** |

The `user_defined_metric_python` type is what D13 and Phase 7 Step 1 depend on. **Its presence in this table is from the REST schema only** — no docs page describing how to author one was found, and no free-tier feature matrix exists. Verify availability in the Cloud UI before designing around it.

Thread rules specifics (VERIFIED):
- *"the only variable available is the `{{context}}` one"* — a list of `{role, content}` messages.
- `{{context}}` can be **enriched with full thread structure** — *"every trace in the thread and every span within it… including that span's type, input, output, metadata, and its position in the conversation."* Judge tools there: `read`, `jq`, `search`. Requires a tool-calling model.
- Built-in thread templates: Conversation Coherence, User Frustration, Custom.
- **Cooldown**: *"Opik waits for a 'cooldown period' after the last activity in a thread… The default cooldown period is 15 minutes"*, configurable at workspace level, or via `OPIK_TRACE_THREAD_TIMEOUT_TO_MARK_AS_INACTIVE` when self-hosted.

**Consequence: do not use thread-scope rules in this project.** The 15-minute cooldown would stall the demo, and there is one call per patient.

Sampling-rate subtlety (VERIFIED): *"Thread and span rules only ever run on production (SDK-logged) data."*

**Historical backfill (VERIFIED) — demo insurance:** rules only run on data logged after creation, but you can select traces/threads in the UI and click the brain icon to apply a rule retroactively. A call made before the rule exists does **not** need re-dialling.

### Judge model choice

**VERIFIED FROM DOCS.** Configured under [Workspace Settings → AI Providers](https://www.comet.com/docs/opik/administration/workspace-settings/ai_providers) — *"AI Providers let you connect LLMs for use in the Playground and Online Evaluation."*

Supported: **OpenAI, Anthropic, OpenRouter, Gemini, VertexAI, Azure OpenAI, Amazon Bedrock, Ollama (local or self-hosted, OpenAI-compatible), vLLM / any other OpenAI API-compliant provider.**

So: freely chosen within those providers, using your own API key, and the OpenAI-compatible escape hatch means effectively any model you can serve. Two constraints from the rules page: tool-calling support for Agent as a Judge, vision support for image traces.

**Project decision:** where a judge is used, pick a **different model family from the conversational agent**, to avoid self-preference bias in LLM-as-judge scoring.

---

# 4. HOSTING

### Faster to get running?

**INFERRED** (the docs never compare them head-to-head, but both paths are documented):

**Cloud is faster.** Cloud: create a free account, `pip install opik`, `opik configure`. Self-host: requires Docker + Docker Compose, `git clone`, `./opik.sh`, then `opik configure --use_local`. The docs describe local deployment as *"easy to setup and allows you to get started in a couple of minutes **but** is not meant for production deployments."*

### Does either restrict online evaluation?

**VERIFIED FROM DOCS — no.**
- [FAQ](https://www.comet.com/docs/opik/faq): *"The Open-Source version of the Opik product includes tracing and online evaluation features so you can monitor your LLMs in production."*
- [Self-host overview](https://www.comet.com/docs/opik/self-host/overview): *"you get access to all Opik features including tracing, evaluation, etc but without user management features."*
- [Changelog 2026-08-31](https://www.comet.com/docs/opik/changelog/2026/8/31): *"**Self-hosted: agentic tool-calling scoring is on by default** — … The toggle has been removed and the behavior is now unconditional everywhere."* — i.e. Agent-as-a-Judge is no longer gated on self-hosted.

### Free tier?

**COULD NOT FIND in the docs.** The docs only say *"Create a free account"* and *"available to both free users and paying customers"* — no feature matrix.

**From a non-docs source** (Comet's pricing page, [comet.com/site/pricing](https://www.comet.com/site/pricing/) — official Comet, but not the Opik docs): Free Cloud is $0, 25k spans/month, 60-day retention, and "Online Evaluation" is checkmarked across all tiers including Free. AI Guardrails is *not* on Free or Pro. **Treat as unverified** — a pricing page can change and the docs do not corroborate it.

### Credentials the Python SDK needs

**VERIFIED FROM DOCS** — [SDK configuration](https://www.comet.com/docs/opik/tracing/advanced/sdk_configuration) and [FAQ](https://www.comet.com/docs/opik/faq)

**Opik Cloud:**
| Setting | Env var | Required? |
|---|---|---|
| API key | `OPIK_API_KEY` | required |
| Workspace | `OPIK_WORKSPACE` | optional |
| Server URL | `OPIK_URL_OVERRIDE` | defaults to `https://www.comet.com/opik/api` |
| Project | `OPIK_PROJECT_NAME` | optional (defaults to `Default Project`) |

**Self-hosted:** no API key (*"If you are using the Open-Source Opik platform, you will not have Opik API keys"*); `OPIK_URL_OVERRIDE` required, e.g. `http://localhost:5173/api`; workspace is `default`.

Config precedence is env vars over the `~/.opik.config` TOML file; relocate the file with `OPIK_CONFIG_PATH`. Set up via `opik configure` / `opik.configure(use_local=False)`.

**Inconsistency worth knowing:** the [self-host overview](https://www.comet.com/docs/opik/self-host/overview) page says to `export OPIK_BASE_URL=http://localhost:5173/api`, while the SDK configuration page's env-var table lists only `OPIK_URL_OVERRIDE` and never mentions `OPIK_BASE_URL`. `OPIK_BASE_URL` could not be found in the env var reference. **Use `OPIK_URL_OVERRIDE`.**

Other env vars relevant here: `OPIK_DEFAULT_FLUSH_TIMEOUT`, `OPIK_TRACK_DISABLE`, `OPIK_ENVIRONMENT`, `OPIK_CONSOLE_LOGGING_LEVEL` / `OPIK_FILE_LOGGING_LEVEL` / `OPIK_LOGGING_FILE`.

---

## Two findings that shape the design

1. **After-the-fact trace assembly is a first-class documented API** (`start_time` / `end_time` on both `trace()` and `span()`). The terminating-agent-process constraint is a non-issue, provided `flush()` is called and its boolean return is checked. This validates D5's clean seam.

2. **Agent as a Judge is the most robust judged mode** — the only one whose judge can reach the `.wav` via `get_attachment`, and the only one that survives the trace shape changing. **But per D13 it is the fallback, not the first choice**, because premature disclosure is a decidable property and deserves a deterministic metric. See the DECISION NOTE at the top of this file.

---

**Sources:** [Online Evaluation rules](https://www.comet.com/docs/opik/production/online-evaluation/rules) · [Log traces](https://www.comet.com/docs/opik/tracing/advanced/log_traces) · [Log media & attachments](https://www.comet.com/docs/opik/tracing/advanced/log_multimodal_traces) · [SDK configuration](https://www.comet.com/docs/opik/tracing/advanced/sdk_configuration) · [Opik Python SDK reference](https://www.comet.com/docs/opik/python-sdk-reference/Opik.html) · [opik_context reference](https://www.comet.com/docs/opik/python-sdk-reference/opik_context/update_current_trace.html) · [Create automation rule evaluator (REST)](https://www.comet.com/docs/opik/reference/rest-api/automation-rule-evaluators/create-automation-rule-evaluator) · [AI Providers](https://www.comet.com/docs/opik/administration/workspace-settings/ai_providers) · [Self-host overview](https://www.comet.com/docs/opik/self-host/overview) · [Local deployment](https://www.comet.com/docs/opik/self-host/local_deployment) · [FAQ](https://www.comet.com/docs/opik/faq) · [Quickstart](https://www.comet.com/docs/opik/quickstart) · [Changelog 2025-12-18](https://www.comet.com/docs/opik/changelog/2025/12/18) · [Changelog 2026-08-31](https://www.comet.com/docs/opik/changelog/2026/8/31) · [Changelog 2026-09-07](https://www.comet.com/docs/opik/changelog/2026/9/7) · [Comet pricing — NOT docs, unverified](https://www.comet.com/site/pricing/)
