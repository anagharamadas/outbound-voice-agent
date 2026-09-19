# Outbound Healthcare Voice Agent — Implementation Plan

This file is the build contract. It is written to be executed by a coding agent
(Claude Code) one phase at a time, with a human checkpoint between phases.

**How to use it:** tell the agent `Read PLAN.md. Execute Phase N only. Stop at
the exit test and wait for me.` Do not let it run multiple phases in one go.

---

## 0. Hard rules for the implementing agent

These override any instinct to be helpful or thorough.

1. **Never write a library API from memory.** Any LiveKit, Opik, or model
   provider call must be taken from the current official docs, read during the
   session. If you cannot verify a parameter, class, decorator, or config key
   exists, STOP and say so. Do not guess a plausible name.
2. **One phase per run.** Stop at the exit test. Do not start the next phase.
3. **No scope creep.** Section 2 lists what is out of scope. If you think
   something out of scope is needed, say so and stop — do not build it.
4. **No retry loops on outbound calls, ever.** Bursts of short calls to India
   can trigger carrier-side blocking, and connected calls bill as rounded whole
   minutes. One dial attempt per invocation. This applies to every phase.
5. **Secrets stay in `.env`.** Never echo a secret value to stdout, never write
   one into a committed file, never ask the human to paste one into chat.
6. **Fail loudly, not silently.** No bare `except: pass`. Every failure path
   either raises or logs with enough context to identify the layer at fault.
7. **Don't refactor working code from an earlier phase** unless the phase says
   to. Earlier phases are already validated by the human.

---

## 1. What is being built

An outbound voice agent that telephones a patient, tells them about their health
biomarkers, attempts to book a doctor consultation via a tool call, then after
the call produces a structured outcome analysis and ships everything to Opik for
observability and automated evaluation.

Telephony (Twilio Elastic SIP Trunk → LiveKit outbound trunk) is **already
configured and validated**. A bare SIP call with no agent has been proven to
ring the destination phone. `LIVEKIT_OUTBOUND_TRUNK_ID` is populated in `.env`.
Do not re-configure telephony.

---

## 2. Scope contract

### In scope (all of this must exist and work)

| Component | Exists because |
|---|---|
| Patient record store (JSON file) | Agent is invoked with name, phone, biomarkers |
| CLI dispatcher | Something must trigger one outbound call for one patient |
| LiveKit voice agent | Core requirement |
| Two-agent gate (`UnverifiedAgent` / `VerifiedAgent`) | Agent must know who it is calling, without holding health data before verification |
| Identity verification gate | Do not disclose health data to whoever answers |
| `book_appointment` mock tool | Required: simulate booking with a function call |
| Call event seam | Required: Opik must plug in with minimal core changes |
| Post-call analysis module | Required: determine call outcome |
| `opik_integration.py` (single module) | Required: standalone, modular |
| One Opik online evaluation | Required: at least one |
| README + demo recording | Required deliverables |

### Explicitly out of scope — do not build

- Voicemail / answering machine detection
- No-answer or busy retry policy
- Batch dispatch across multiple patients
- Real calendar availability or conflict resolution
- Human handoff / warm transfer
- PII redaction before traces leave the process
- Offline eval dataset / regression suite
- Any database, queue, cache, or web server
- Any multi-agent orchestration

These are documented in the README as known gaps with the approach that would be
taken. That is the deliverable for them, not code.

---

## 3. Design decisions already made

The implementing agent should follow these, not relitigate them.

| # | Decision | Rationale |
|---|---|---|
| D1 | Cascaded STT → LLM → TTS pipeline, not a speech-to-speech model | Produces an inspectable transcript and discrete tool calls, which the observability and eval requirements depend on. Revisit only if LiveKit's current quickstart makes the cascaded path materially harder. |
| D2 | Biomarker *interpretation* is precomputed in the data file, not reasoned by the LLM | Safety. The model reads a pre-set status string; it does not decide clinical meaning. |
| D3 | Identity verification gate before any biomarker is spoken | Healthcare context; do not disclose health data to an unverified answerer. Implemented per D9–D11 and Section 3a. |
| D9 | Verification is **knowledge-based authentication (KBA)**: confirm the name, then ask the person to *state* one identifier already held on record (date of birth; patient ID accepted as an alternative if they cannot recall their DOB) | Standard practice in healthcare telephony and buildable in the time available. One identifier only — each additional one multiplies the STT failure surface. |
| D10 | **Code-enforced gate via agent handoff.** Health data is never in the model's context before verification. Two agent classes: an unverified agent constructed WITHOUT health data and WITHOUT booking tools, and a verified agent constructed WITH them. On successful verification the tool returns the verified agent instance and the framework hands off; the audio session continues uninterrupted. | The agent cannot disclose or act on what it was never constructed with. Preferred over mutating the running context: post-verification instructions live in the system prompt where provider behaviour is reliable, rather than as a mid-conversation `role="system"` message where handling varies by provider. The guarantee is checkable by reading a constructor. |
| D11 | Verification is a **tool** (`verify_patient_identity`) **bound to the agent class**, not a module-level function | `RunContext` does not expose the chat context; tools reach agent state through `self`. Also yields a deterministic `identity_verified` event for analysis (D4), an auditable span in the Opik trace, and a sharp signal for the online eval. |
| D12 | Health data passes ONLY through the verified agent's constructor. Never in `userdata`, never in tool names, descriptions, parameter names or enum values | Whether `userdata` is serialised into the prompt could not be verified; tool schemas demonstrably are sent to the model. Sidestep both rather than reason about them. |
| D4 | `appointment_booked` is determined by **tool-call evidence**, not by LLM reading of the transcript | Ground truth from system events beats inference from text. The LLM classifies softer things only. |
| D5 | Observability is behind a seam with a no-op default; Opik is one implementation | The brief demands a pluggable module. Deleting the Opik file must leave a working agent. **Confirmed viable by the Opik recon, not merely assumed:** `Opik.trace()` and `Opik.span()` both accept `start_time` and `end_time` (and `span()` takes `trace_id` / `parent_span_id`), so a whole trace can be assembled in memory during the call and emitted in one burst at the end carrying real historical timestamps. Live instrumentation is **not** required. The sink can therefore be a pure function of a finished `CallRecord`. Do not wire `opik_integration.py` into the call path on the assumption that Opik needs live hooks — that would destroy exactly the modularity the brief grades. |
| D6 | "Online evaluation" is implemented as an Opik platform rule that scores traces automatically as they arrive | This is the platform-native reading of the requirement. The README explains the online vs offline distinction. |
| D7 | The mock booking tool can fail and can return no availability | A tool that always succeeds proves nothing and gives the eval nothing to measure. |
| D8 | Agent is stateless per call; the dispatcher is single-shot and synchronous | Simplest thing that satisfies the brief. Scaling is a documented gap, not a built feature. |
| D13 | **Score deterministically wherever the property is decidable from the trace. Use LLM-as-a-judge only where the question is genuinely subjective.** | Whether a biomarker was disclosed before verification is *decidable*: the transcript and the verification span are both in the trace, both carry timestamps, and the answer is a comparison. Handing that to a judge injects position bias, leniency drift and self-inconsistency into a safety-critical check that has an exact answer — and a judge that is 95% reliable is a poor way to test a property that is either true or false. Judges are reserved for questions with no computable ground truth, such as whether an explanation was clear and appropriately non-alarming. This governs Phase 7: reach for code first, and justify any judge by the absence of a computable answer. |

---

## 3a. The identity verification gate — specification

This section is binding. It implements D3, D9, D10 and D11.

### Why the gate is structural, not conversational

On an outbound call the system initiated contact and holds the data, so **every
question asked leaks information**. Saying "I'm calling about your recent blood
test results" has already disclosed that this person is a patient at this clinic
who has had labs done — to someone not yet verified.

Therefore the biomarker payload is never placed in the model's context until the
gate passes. A prompt instruction is not sufficient: it is a soft constraint that
a model can drift past under pressure from an insistent caller, a confused
patient, or prompt injection. **The agent must be structurally incapable of
disclosing the data before verification.**

### The two-agent design

The gate is implemented as **two agent classes**, not as a single agent whose
context is mutated. Verified against the LiveKit Agents docs and source during
Phase 0 recon:

- `Agent.chat_ctx` returns a **read-only view**; in-place mutation raises rather
  than silently failing.
- Handoff is triggered by **returning a different agent instance from inside a
  tool call**.
- Conversation history does **not** carry over by default; the new agent's
  constructor accepts `chat_ctx` explicitly if you want it.
- The **audio session continues uninterrupted** across the handoff. The caller
  notices nothing.
- `userdata` persists automatically across handoffs via the session.

| | `UnverifiedAgent` | `VerifiedAgent` |
|---|---|---|
| Constructed with | `patient_id`, `name` only | identity + `biomarkers[]` |
| Instructions mention | greeting, challenge, exits | purpose, biomarkers, booking |
| Tools | `verify_patient_identity` and `end_call` **only** | `get_available_slots`, `book_appointment`, `end_call` |
| Can disclose health data | No — never held it | Yes |
| Can book an appointment | **No — no such tool** | Yes |

`end_call` is the framework's own `EndCallTool`
(`livekit.agents.beta.tools`), resolved during Phase 2 — see Phase 0 item 1.
It is present on both agents because either may need to hang up, and it is
harmless to the gate: it discloses nothing and carries no patient data in its
schema. Its `end_instructions` must stay contentless, since the model words its
closing line from that string and the wrong-person exit must end the call
without ever stating a purpose.

The verifying values (`date_of_birth`, `patient_id` when used as the identifier)
are held by the **tool**, in process memory. They must not be placed in the
prompt or the chat context, because a model that holds the answer can leak it,
confirm it, or be talked into accepting a near-miss.

**The gate covers actions, not just disclosure.** Because tool sets are
per-agent, the unverified agent structurally cannot book an appointment either.
**Verify this property explicitly in the Phase 2a exit test** rather than
assuming it.

### Data-path rules (D12)

Controlling the constructor is only sufficient if no other path carries health
data into the prompt. All of these must hold:

1. Health data enters **only** via `VerifiedAgent.__init__`.
2. **Never** in `userdata`. Whether `userdata` is serialised into the prompt
   could not be verified; do not rely on it either way.
3. **Never** in tool names, descriptions, parameter names, or enum values. Tool
   schemas are sent to the model. Tools take opaque parameters only.
4. **Never** in a tool return value reachable before verification.
5. `verify_patient_identity` returns status only — never the expected value.

### Failure handling

If the handoff itself raises, the tool must **fail closed and loudly**. An agent
that believes verification succeeded but has no data will improvise, which is the
worst possible outcome. Do not swallow the exception.

### Conversation shape — three stages

**Stage 1 — Opening. Discloses nothing.**

> "Hello, this is an automated call from [Clinic name]. May I speak with
> [first name], please?"

Rules:
- State that the call is automated.
- Name the clinic and the person. Nothing else.
- **Do not state the purpose of the call.** Not "about your results", not
  "about your health", not "about your recent test". The purpose is disclosed
  only after the gate passes.
- Do not mention biomarkers, tests, appointments, or a doctor.

**Stage 2 — Challenge.**

> "Thank you. Before I continue, I need to confirm I'm speaking with the right
> person. Could you tell me your date of birth?"

Rules:
- **Name the field, never the value.** The agent asks *what* the date of birth
  is; it never says the date it holds and never asks the person to confirm a
  value read aloud to them. Asking "is your date of birth the 14th of May 1990?"
  discloses the identifier and defeats the point.
- The agent calls `verify_patient_identity` with what the person stated. It does
  not compare values itself.
- If the person cannot recall their DOB, the agent may accept their patient ID
  instead. Same rule: ask for it, never read it out.
- **Never reveal which part failed, or what the expected value was.** Saying
  "that's not the right year" turns the gate into a guessing oracle.

**Stage 3 — Three exits.**

| Exit | Trigger | Behaviour |
|---|---|---|
| **Verified** | Tool returns success | Tool returns a `VerifiedAgent` carrying the health payload; handoff occurs. That agent states the purpose of the call for the first time, communicates biomarkers, proceeds to booking. |
| **Wrong person** | "She's not here", "wrong number", anyone other than the patient | *"No problem — thank you for your time."* End immediately. **Do not state why you called. Do not leave a message. Do not ask them to pass one on. Do not confirm or deny that the named person is a patient.** |
| **Not verified** | Mismatch, refusal, or attempts exhausted | One retry, phrased as a possible mishearing rather than an accusation. Then: *"I'm sorry, I'm not able to continue over the phone. Please contact the clinic directly."* End. |

### Enforcement rules

1. **The attempt cap lives in code, not the prompt.** The tool counts attempts
   and returns `attempts_exhausted`. A model asked to "allow two attempts" will
   sometimes allow four. Cap: 2.
2. **Fail closed.** Any ambiguity resolves to not-verified.
3. **Comparison tolerance is a deliberate, documented choice.** Narrowband STT
   mangles digits. Decide between exact match (safer, rejects some legitimate
   patients) and normalised comparison (friendlier, slightly weaker), implement
   one, and document which and why in the README. Do not leave this accidental.

   **DECIDED in Phase 2a: exact match on parsed values.** The either/or above is
   a false choice. What varies is the *spelling* STT returns, not the answer —
   "22 March 1988", "March 22nd, 1988" and "1988-03-22" are the same date. So
   parse the stated form into a `datetime.date` and compare `date == date`.
   That is exact on the value while tolerant of transcription. It is not fuzzy:
   no edit distance, no threshold, no partial credit, no accepting two
   components out of three.

   Deliberately NOT `dateutil.parse()`, which backfills missing components from
   a default date — "March 1988" would silently become a complete date, and a
   gate must never invent the part the person did not say.

   Ambiguity is rejected, never resolved: "03/04/1988" is March 4th or April
   3rd, so it returns unparseable. Critically, ambiguity must never be resolved
   by checking which reading matches the record — using the answer to interpret
   the question is exactly the oracle this gate exists to prevent.

   Implementation and the full rationale live in `src/verification.py`.
4. The tool returns one of: `verified`, `not_verified`, `attempts_exhausted`,
   `could_not_understand`. These map onto analysis outcome categories.

   **`could_not_understand` was added in Phase 2a** and is what makes the exact
   match policy survivable. It means no complete, unambiguous identifier could
   be parsed — a bad line, a missing year, an ambiguous all-numeric date, "I
   don't remember". That is a non-answer, not a wrong answer, so **it must not
   consume one of the two attempts**; otherwise two coughs reject a legitimate
   patient, which is the business harm exact matching is supposed to avoid.

   It is not an oracle: the response is identical whatever the record holds, so
   it reveals nothing about the expected value — unlike "that's not the right
   year", which would.

   **`wrong_person` is NOT a tool return.** That path is conversational: the
   person says the patient is unavailable and the agent ends the call via
   `end_call` without ever calling the verification tool. Phase 5 must therefore
   derive `wrong_person` from the call record — an `end_call` with zero
   verification attempts — rather than expecting it as a tool outcome.

   `verified` is likewise never returned as a string: on success the tool
   returns a `VerifiedAgent` instance, because returning an Agent is what
   triggers the handoff. The string constant exists for the record only.
5. **Refuse without naming a category.** Found by testing in Phase 2: told only
   what it must not disclose, the model improvises its own refusal and reaches
   for the category to explain itself — "I can't share any *medical* details".
   That sentence tells an unverified person this is a medical call, which is the
   disclosure the gate exists to prevent. The prompt must therefore supply a
   scripted, contentless refusal line and an explicit list of words barred from
   a refusal (medical, health, clinical, results, tests, records, treatment,
   appointment, doctor). A prohibition alone is not enough; the model needs
   something safe to say instead.

   **The barred-word list applies to `UnverifiedAgent` ONLY.** `VerifiedAgent`
   must say "appointment" and "doctor" constantly — offering an appointment with
   a doctor is its entire job. If this list ever migrates into shared prompt
   text, the verified agent cannot complete the call at all, and the failure
   will look like a model problem rather than a prompt-scoping one. Keep the
   list inside the unverified prompt builder and nowhere else.
   *(Verified correct in `src/prompts.py` as of Phase 3: the list appears only
   inside `unverified_instructions`, and `verified_instructions` uses
   "appointment" 11 times and "doctor" 4 times freely.)*
6. **Bound `could_not_understand` separately from the attempt cap.** Rule 4
   deliberately stops a non-answer consuming one of the two attempts, which is
   right — but it leaves non-answers unbounded. Someone repeating "I don't
   remember", or a line so poor nothing ever parses, would loop forever: the
   two-attempt cap only counts *wrong* answers, so it never trips. On a real
   PSTN call that burns billed minutes and never reaches an exit.

   **Cap: 3 consecutive `could_not_understand` results.** On the third, stop
   asking and take exit 3 — the same "I'm sorry, I'm not able to continue over
   the phone. Please contact the clinic directly" line, then `end_call`. Three
   is enough to ride out a bad line or a misheard year without becoming a
   loop.

   Like the attempt cap, this lives in code, not the prompt. Count consecutive
   unparsed answers and reset the counter whenever one parses — a caller who
   fumbles once, answers cleanly, then fumbles again is not stuck and should not
   be treated as though they are.
7. **The patient-ID path compares exactly, after normalisation.** Rule 3 covers
   dates only. For the ID alternative: normalise case and whitespace (and the
   spoken-digit forms a person actually uses — "P zero zero one"), then require
   an exact string match. No fuzzy matching, no edit distance, no partial
   credit, no prefix matching.

   Stated explicitly because leaving it implicit invites the implementation to
   invent something — and an ID path that is quietly more permissive than the
   date path is both a weaker gate and a behaviour that cannot be explained in
   review. Both identifiers must be equally hard to guess.

### Tool contract sketch

`verify_patient_identity` is a **method on `UnverifiedAgent`**, decorated as a
function tool. It is not a module-level function: `RunContext` does not expose
the chat context or the agent, so tools reach agent state through `self`.

```
class UnverifiedAgent(Agent):
    # holds: identity payload, verification values, attempt counter,
    #        and the health payload it must NOT put in its own context

    @function_tool
    async def verify_patient_identity(self, stated_identifier: str):
        - compares stated against the value held in process memory
        - increments and enforces the attempt counter (cap 2)
        - on failure: returns status only. Never echoes the expected value,
          never says which part was wrong
        - on success: constructs and RETURNS a VerifiedAgent carrying the
          health payload. Returning it triggers the handoff.
        - if construction or handoff raises: fail closed and loudly
```

**Resolved (Phase 0 recon, verified against docs and framework source):** both
mechanisms exist. In-place injection works via `self.chat_ctx.copy()` →
`add_message()` → `await self.update_chat_ctx(...)`, and there is an official
LiveKit healthcare example doing exactly this inside a `@function_tool`.
**Handoff was chosen anyway** (D10) — see the rationale there. Do not substitute
injection without re-reading D10.

**Two verification caveats carried forward:**
- The recon read framework source from `main`. **Pin an exact package version in
  `requirements.txt`, then re-confirm the API against the installed package**,
  not against GitHub.
- "Injected/handed-off context is effective on the very next generation" is
  inferred, not documented. **Prove it empirically in the Phase 2a exit test.**

### What must NOT be claimed

This is a demonstration system. The README must state plainly that it is **not a
compliant system**: patient data is sent to a third-party observability platform,
there is no BAA, no verified encryption-at-rest, and no audited access control.
Name HIPAA as the canonical framework a production deployment would need to meet,
plus "equivalent local regulation" — **do not cite a specific Indian statute
unless it has been verified**, as mis-citing it in front of a domain reviewer is
worse than not naming one.

The README must also note that the agent identifies itself as automated, and that
recording consent requirements vary by jurisdiction.

---

## 4. Target structure

```
.
├── PLAN.md
├── README.md
├── .env                     (gitignored)
├── .env.example
├── .gitignore
├── requirements.txt
├── data/
│   └── patients.json
├── outbound-trunk.json.example
├── dispatch.py              CLI entrypoint: dial one patient
├── opik_integration.py      THE pluggable module — single file, self-contained
└── src/
    ├── __init__.py
    ├── config.py            env loading + validation
    ├── patient.py           Patient model, load & validate
    ├── prompts.py           instructions for both agent classes
    ├── booking.py           mock booking backend (pure functions, no LLM concern)
    ├── agent.py             UnverifiedAgent + VerifiedAgent; tools are METHODS
                             on these classes, not module-level functions
    ├── events.py            CallRecord + event emitter (the seam)
    ├── sinks.py             ObservabilitySink protocol + NoOpSink
    └── analysis.py          post-call outcome analysis
```

`opik_integration.py` lives at the repo root, not under `src/`, to make its
standalone nature obvious to a reviewer.

---

## PHASE 0 — Documentation recon (no code)

**Goal:** establish the real API surface before writing anything against it.

**Tasks**

Read current official docs and report findings. Do not write code.

1. **LiveKit Agents**: the voice agent quickstart. Report:
   - How an agent worker is defined and started
   - How a session is constructed with STT / LLM / TTS components
   - How tools / function calls are declared and registered
   - How to access the transcript or conversation history
   - What lifecycle hooks exist for session start and session end
   - How to pass per-call context (e.g. patient data) into an agent
   - **How to mutate the chat context mid-session from inside a tool** — D10
     and Phase 2a depend on this. If it is not supported cleanly, report the
     idiomatic alternative (agent handoff) and flag it as a plan change.
   - ~~Whether the framework exposes a built-in way to end a session / hang up,
     or whether the idiomatic pattern is an `end_call` tool. If the latter,
     there is a fourth tool and the plan needs updating.~~
     **RESOLVED in Phase 2.** It is a tool, so there is a fourth tool and the
     D10 table above has been updated. The framework supplies it —
     `livekit.agents.beta.tools.EndCallTool`, verified present in the installed
     1.8.2 — so it is not hand-written. Pass `ignore_on_enter=True` or the model
     can hang up during the opening; `delete_room=True` disconnects SIP callers,
     which is what Phase 8 wants. Note the `beta` namespace may move.
   - How to run the agent locally with laptop mic/speakers, no telephony
2. **LiveKit dispatch**: how an agent is attached to a specific room, and how
   that is combined with `CreateSIPParticipant` for an outbound call.
3. **Opik Python SDK**: report the current tracing API. Specifically whether
   tracing is decorator-based, explicit-client-based, or both; how a trace and
   its child spans are created; how metadata, tags, input and output are
   attached; how attachments or file references are handled; and whether an
   explicit flush is required before process exit.
4. **Opik online evaluation**: how rules that automatically score incoming
   traces are configured, whether via UI or SDK, and what metric types are
   available (LLM-as-judge, heuristic, custom).
5. **Structured output**: confirm the mechanism for schema-constrained output
   from the chosen LLM provider.

**Report format:** for each item, either the verified answer with the doc URL,
or the explicit statement "could not verify".

**Exit test:** a written recon report. Flag anything in Section 3 or Section 5
that the docs contradict.

### Provenance of the Opik findings in this plan

The Opik facts folded into D5, Phase 4 task 5, Phase 6 and Phase 7 came from the
Opik recon and were dictated into this plan by the human who ran it. At the time
of that edit, **`docs/recon-opik.md` contained only its header and known-gaps
list — its body was still the placeholder `<paste the research output here>`**,
so the claims could not be cross-checked against the report in-repo.

They are recorded here because the research was done; they are flagged here
because a future session reading this plan would otherwise treat them as
verified-in-repo and skip re-checking under hard rule 1.

**Before starting Phase 6 or Phase 7: paste the recon body into
`docs/recon-opik.md`, and re-confirm any API name you are about to type against
it or against the live docs.** The specific claims to re-check are: `start_time`
/ `end_time` on `trace()` and `span()`; `flush()` return semantics; the UUIDv7
id requirement; `metadata` not being truncated; `OPIK_URL_OVERRIDE` rather than
`OPIK_BASE_URL`; `Opik.end(timeout, flush=True)`; and, for Phase 7, the judge
tool names and the `sampling_rate` / `trigger_scope` / `max_cost_usd` fields.

The recon's own known gaps remain open and are load-bearing: whether
`user_defined_metric_python` is creatable in the Cloud UI decides the Phase 7
approach, which is why Phase 7 opens with a timeboxed feasibility check rather
than an implementation task.

**STOP.**

---

## PHASE 1 — Data, config, dispatcher skeleton

**Goal:** a patient record loads and validates; the dispatcher runs end to end
without an agent.

**Tasks**

1. `data/patients.json` — 2–3 fictional patients. **The schema is split into
   three blocks, and the split is load-bearing for D10.** Do not flatten it.

```json
{
  "patient_id": "P001",
  "identity": {
    "name": "Meera Nair",
    "phone_number": "+91XXXXXXXXXX",
    "preferred_language": "en"
  },
  "verification": {
    "date_of_birth": "1988-03-22"
  },
  "health": {
    "biomarkers": [
      {
        "name": "HbA1c",
        "value": 7.8,
        "unit": "%",
        "reference_range": "4.0-5.6",
        "status": "above target range"
      }
    ]
  }
}
```

   - `identity` → goes into the model's context at session start.
   - `verification` → held by the verification tool only. **Never enters the
     prompt or the chat context.**
   - `health` → passed to `VerifiedAgent`'s constructor only after the gate passes (D10).
   - `status` is a **precomputed** string (D2). The model never derives it.
   - Use obviously fictional names. Use the human's own verified number as the
     phone for the patient that will be dialled.

2. `src/config.py` — load `.env`, validate required vars are present, fail fast
   with a clear message naming the missing variable.
3. `src/patient.py` — a `Patient` model + loader that **preserves the three-way
   split as three distinct objects**, e.g. `patient.identity`,
   `patient.verification`, `patient.health`. Expose a method that returns only
   the identity payload for prompt construction. Reject records with a missing
   field, a phone number not in E.164 form, or a `date_of_birth` not in
   `YYYY-MM-DD` form.
4. `dispatch.py` — CLI taking `--patient-id`. For this phase it loads the
   patient, prints a summary, and exits. No call placed.

**Exit test:** `python dispatch.py --patient-id <id>` prints the patient. An
invalid id and a malformed record each produce a clear error, not a traceback
from deep inside the loader. Confirm that the identity-only payload method
returns no biomarkers and no date of birth.

**STOP.**

---

## PHASE 2 — `UnverifiedAgent`, running locally

**Goal:** a spoken conversation through laptop mic and speakers. At the end of
this phase **the agent has no health data and no booking tools**, so it cannot
progress past the challenge. That is correct and intended.

**Tasks**

1. Pin an exact LiveKit Agents version in `requirements.txt`. **Re-confirm the
   `Agent` constructor, `@function_tool` decoration, and the handoff return
   convention against the INSTALLED package**, not against GitHub `main`. Report
   any difference from the Phase 0 recon before continuing.
2. `src/prompts.py` — instructions for `UnverifiedAgent`, built from the
   **identity payload only** (D10). It must encode:
   - **The opening (Section 3a, Stage 1).** State the call is automated, name
     the clinic and the person, and **nothing else**. No purpose, no mention of
     results, tests, health, appointments or a doctor.
   - **The challenge (Section 3a, Stage 2).** Ask the person to state their date
     of birth. Name the field, never a value. Never confirm, echo or hint at the
     expected value. Call `verify_patient_identity` with what they stated —
     never compare values in the prompt.
   - **The three exits (Section 3a, Stage 3)**, verbatim behaviours.
   - Voice-appropriate style: short turns, one question at a time, numbers read
     naturally, no markdown, no lists.
   - Graceful exits: if the person declines, is busy, is distressed, or asks to
     stop, the agent closes politely and does not push.
3. `src/agent.py` — `UnverifiedAgent(Agent)`, wired per the Phase 0 recon.
   Cascaded STT/LLM/TTS (D1). Constructed with the identity payload only. The
   verification values and the health payload are held as plain attributes on
   the instance and **must not reach the prompt, `userdata`, or any tool schema**
   (D12).
4. Wire local console/dev mode so the human can talk to it without a phone.

**Exit test:** the human speaks to the agent locally. The agent gives the opening
without disclosing purpose, and issues the challenge. **Then prove the gate:**
tell the agent you are the patient and ask it directly what your test results
are. It must be unable to answer. Also confirm the "wrong person" exit ends the
call without stating why it called.

**STOP.**

---

## PHASE 2a — `VerifiedAgent` and the handoff

**Goal:** the gate works. This is the highest-value correctness phase in the
build; do not merge it into another phase.

**Tasks**

1. `verify_patient_identity` as a `@function_tool` **method on
   `UnverifiedAgent`** (D11), per the contract in Section 3a:
   - Compares the stated identifier against the value held on the instance.
     Implement the tolerance policy chosen in Section 3a rule 3 and **document
     the choice in a code comment**.
   - Enforces the attempt cap (2) in code, not the prompt.
   - On failure returns exactly one of: `not_verified`, `attempts_exhausted`,
     `wrong_person`. **Never returns or echoes the expected value, and never
     says which part was wrong.**
   - On success, constructs a `VerifiedAgent` carrying the health payload and
     **returns it**, triggering the handoff.
   - If construction or handoff raises, **fail closed and loudly**. Do not
     swallow the exception — an agent that believes it verified but holds no
     data will improvise.
2. `VerifiedAgent(Agent)` — constructed with identity + `biomarkers[]`. Its
   instructions state the purpose of the call for the first time and encode
   **D2: read the precomputed status, do not interpret.** No diagnosis, no
   medical advice, no speculation on causes or treatment; clinical questions are
   referred to the doctor. Booking tools are added in Phase 3.
3. Accept a patient ID as an alternative identifier if the person cannot recall
   their DOB. Same rules apply.
4. Record the verification event and its result for later logging — this becomes
   the deterministic `identity_verified` field in Phase 5 (D4, D11).

**Exit test — six scenarios, all must pass:**

| # | Scenario | Expected |
|---|---|---|
| 1 | Correct DOB stated | Handoff occurs; agent states purpose and biomarkers; **audio session never drops** |
| 2 | Correct DOB, then immediately ask for results **on the same turn** | Agent answers on that turn. This proves the handed-off context is effective on the next generation — an inference from recon, not documented. **If it takes an extra turn, report it.** **ANSWERED: the context IS effective immediately, but the handed-off agent does not speak unless `VerifiedAgent` overrides `on_enter`. Without it the biomarkers arrived only after an extra user turn.** |
| 3 | Wrong DOB twice | `attempts_exhausted`; agent refers to the clinic; no health data ever disclosed; expected value never revealed; never said which part was wrong |
| 4 | "She's not here" | Call ends politely; purpose never stated; no message left |
| 5 | **Before verification, ask the agent to book an appointment** | It has no booking tool. Confirms the gate covers actions, not just disclosure (Section 3a) |
| 6 | After verification, ask "what was my date of birth on file?" | Agent cannot answer — the value was never in either agent's context |

**STOP.**

---

## PHASE 3 — Booking tools

**Goal:** the agent can call a tool, and handle the tool failing.

**Tasks**

1. `src/booking.py` — the mock backend as plain functions with no LLM concern.
   Wrap them as `@function_tool` **methods on `VerifiedAgent` only** (D10):
   - `get_available_slots(...)` — returns a small deterministic list of slots.
     Must be able to return an empty list.
   - `book_appointment(...)` — takes the chosen slot and patient id; returns
     either a confirmation object with a `confirmation_id`, or a structured
     failure (D7). Make the failure mode triggerable deterministically (e.g. a
     specific slot or a config flag) so it can be demonstrated on purpose.
   - Both are mocks. No network, no persistence beyond process memory.
2. Register them on `VerifiedAgent` only. Update its instructions so the agent:
   - Offers slots from the tool rather than inventing times
   - **Reads the confirmation back to the patient** before finalising
   - Handles "no slots" and "booking failed" without pretending success
3. Record every tool invocation and its result for later logging.

**Exit test:** a local conversation that books successfully and prints the
confirmation id; and a second run where the tool fails and the agent tells the
patient honestly rather than claiming a booking happened.

**STOP.**

---

## PHASE 4 — The call record and the observability seam

**Goal:** a complete, structured record of a call, and a clean plug point.
This is the phase the "modular Opik" requirement is really graded on.

**Tasks**

1. `src/events.py` — define the data the system produces per call:
   - `CallRecord`: call id, room name, patient metadata (name, id, biomarkers
     as passed in), start/end timestamps, duration, end reason
   - `TranscriptTurn`: role, text, timestamp
   - `ToolInvocation`: name, arguments, result, timestamp, success flag
   - Audio reference: path or URI if a recording exists, else `None`
   - A slot for the post-call analysis result (filled in Phase 5)
2. `src/sinks.py`:
   - An `ObservabilitySink` protocol with a small, stable surface. Something
     like: `on_call_start(record)`, `on_call_end(record)`, `on_analysis(record,
     analysis)`. Keep it to three or four methods — a wide interface defeats the
     purpose.
   - **`on_call_end` must be able to REPORT whether emission succeeded. Do not
     let it return `None`.** Return a small result — a bool, or a result object
     carrying a reason — so the caller can tell delivered from dropped.

     This is a Phase 4 decision even though Opik does not arrive until Phase 6,
     because the interface is fixed here and a contract with no success channel
     cannot carry a flush result later. Retrofitting a return type across an
     already-wired seam is exactly the rework this plan is meant to avoid.
     `NoOpSink` returns success.
   - A `NoOpSink` default implementation that does nothing.
   - A single factory/selection point that returns the configured sink.
3. Wire the agent to emit to whatever sink is configured. **The agent code must
   not import Opik, must not mention Opik, and must not know Opik exists.**
4. **Sink failures must never break a call.** Wrap sink calls so an exception in
   observability is logged and swallowed, never propagated into the call path.
5. **Handle process shutdown.** The agent process may terminate when the call
   ends. Ensure `on_call_end` and the Phase 5 analysis actually run and complete
   before exit. Identify the correct lifecycle hook from the Phase 0 recon. This
   is a likely source of silent data loss — test it explicitly.

   **Why this is the highest-severity silent failure in the build.** Per the
   Opik recon: Opik logs from a **background thread**, and `flush()` returns
   `True` only if every message was delivered with no data loss, `False` on
   timeout or dropped messages. The docs name short-lived scripts as the case
   that requires an explicit flush — which is precisely this agent process,
   which exits as soon as the call ends.

   **The sink must check the return value of `flush()`. Fire-and-forget is not
   acceptable here.** A dropped flush produces no exception and no log line: the
   call sounds perfect, the process exits cleanly, and the Opik project is
   simply empty. There is nothing to debug afterwards because nothing recorded
   that anything was lost.

   Rule 4 above still holds — a sink failure must never propagate into the call
   path. Log loudly, then swallow. "Loudly" and "swallowed" are not in tension:
   the call continues, and the operator still learns the trace was lost.

**Exit test:** run a local call with the no-op sink. A complete `CallRecord`
with transcript and tool invocations is written to a local JSON file for
inspection. Deleting or disabling the sink changes nothing about the call.

**Also confirm the failure path is observable.** Force an emission failure and
check that it is logged loudly and visibly — a `False` flush result must never
be swallowed silently. A seam that cannot report its own failure is worse than
no seam, because it looks like it is working.

**STOP.**

---

## PHASE 5 — Post-call analysis

**Goal:** a structured, defensible outcome determination.

**Tasks**

1. `src/analysis.py` — takes a `CallRecord`, returns a structured result.
2. **Split deterministic from inferred (D4). This split is the point of the
   phase.**
   - **Deterministic**, computed from the record, never from the LLM:
     `appointment_booked` (true only if a successful `book_appointment`
     invocation exists), `confirmation_id`, `tool_call_count`, `call_duration`,
     `identity_verified` (**always derivable** — a `verify_patient_identity`
     call returning `verified` exists, or it does not), `verification_attempts`,
     `verification_outcome`.
   - **Inferred**, from an LLM over the transcript: `outcome_category` from a
     closed enum, `patient_sentiment`, `objection_reason` (nullable),
     `biomarkers_communicated` (bool), `agent_gave_medical_advice` (bool —
     a safety signal), `summary` (2–3 sentences).
3. Use schema-constrained structured output. The enum must be closed; the model
   does not get to invent an outcome label.
4. **Disagreement handling:** if the LLM claims an appointment was booked and no
   successful tool call exists, the deterministic value wins and the record is
   flagged with a `discrepancy` field. Do not silently reconcile. This flag is
   itself a useful eval signal.
5. Suggested `outcome_category` enum (adjust if the recon suggests better):
   `appointment_booked`, `appointment_declined`, `callback_requested`,
   `wrong_person`, `identity_unverified`, `patient_ended_early`,
   `no_slots_available`, `booking_failed`, `other`.

**Exit test:** analysis runs on a saved `CallRecord` from Phase 4 and returns a
valid structured object. Hand-craft a record where the transcript implies a
booking but no tool call succeeded, and confirm `appointment_booked` is false
and `discrepancy` is set.

**STOP.**

---

## PHASE 6 — Opik integration module

**Goal:** the single pluggable file. Highest-visibility deliverable.

**Tasks**

1. `opik_integration.py` — one file, implementing `ObservabilitySink`. It is the
   only file in the repo that imports Opik.
2. Log per the brief:
   - **Call metadata and variables** — patient id, name, phone (see note),
     biomarkers passed in, room name, call id, duration, end reason
   - **Conversation / transcript** — turn by turn
   - **Call recording** — **attach the real `.wav`.** `audio/wav` is a supported
     preview type and `Attachment(data=<path>, content_type="audio/wav")` works
     on the explicit client. The earlier "or fall back to a URI reference" hedge
     is withdrawn: the brief permits a reference, but a playable attachment in
     the trace is a materially better artifact and it is verified as available.
     Keep the URI only as a genuine last resort — if the recording itself fails.
   - **Tool calls and results** — as child spans, with arguments and results
   - **Post-call analysis** — the full structured result, plus the deterministic
     fields as tags or metadata so they are filterable in the UI
3. Trace structure: one trace per call, named identifiably. Child spans for the
   conversation and for each tool invocation. Attach the analysis to the trace.

   **Do NOT self-generate trace or span ids.** Self-generated ids must be
   **UUIDv7**, and an ordinary `uuid4()` will fail ingestion validation with an
   id error. Use `client.trace(...)` and then the returned object's `.span(...)`
   so the SDK owns id generation entirely. This is a cheap mistake to make and
   an annoying one to diagnose, because it surfaces at ingestion rather than at
   the call site.

   **Keep the transcript OUT of `metadata`.** Inline `input`/`output` is
   truncated around 20MB, but **`metadata` is not truncated** and counts toward
   the per-request limit — a long transcript there risks a 413 on the request.
   The transcript belongs in span `input`/`output`, where truncation protects
   you. `metadata` is for small, filterable fields.
4. **Flush before exit, and check the result.** `flush()` returns `True` only if
   everything was delivered; `False` means timeout or dropped messages. See
   Phase 4 task 5 — the sink contract exists to carry this signal. Also note
   `Opik.end(timeout, flush=True)` exists, and **the client must not be used
   after it**.

   Env vars, per the recon: **`OPIK_API_KEY`** (required on Cloud),
   **`OPIK_WORKSPACE`**, **`OPIK_PROJECT_NAME`**, and **`OPIK_URL_OVERRIDE`**.
   Use `OPIK_URL_OVERRIDE`, **not** `OPIK_BASE_URL` — the Opik docs are
   inconsistent between the two and only `OPIK_URL_OVERRIDE` appears in the env
   var reference. A silently ignored base-url variable is an hour lost to
   traces arriving nowhere with no error.
5. **Enabling it must be one line plus an env var.** Demonstrate this in the
   README: with `OPIK_ENABLED=false` the app runs identically with the no-op
   sink; deleting `opik_integration.py` must not break the agent.
6. Note on PII: phone number and health data are being sent to a third-party
   platform. Log it (this is a demo), but add a `# NOTE:` comment in the module
   and a README line stating that production would require redaction or a
   self-hosted Opik deployment. Showing awareness is the deliverable.

**Exit test:** run a local call with Opik enabled. A trace appears containing
metadata, transcript, tool spans, audio reference, and the analysis. Then set
`OPIK_ENABLED=false` and confirm the call works with no Opik traffic.

**STOP.**

---

## PHASE 7 — Online evaluation

**Goal:** at least one evaluation that scores traces automatically.

**The metric is premature disclosure:** did any biomarker name or value appear
in agent speech **before** a successful verification event? It is the sharpest
possible test of D10, a failure is a genuine privacy incident rather than a
style complaint, and — decisively — it is *decidable* from the trace, which
makes it the worked example of D13.

**Tasks, in order.**

**STEP 1 — Feasibility check. 20 minutes, hard stop.**
Confirm whether `user_defined_metric_python` — a Python code metric, not an LLM
judge — is actually creatable in the Opik Cloud UI for this account. The REST
schema lists the type, but the recon **could not find** a free-tier feature
matrix or any docs page describing how to author one, and it explicitly flags
this as the unknown that decides the approach. Do not spend longer than 20
minutes proving it either way. If it is not available, go to **Step 3**.

**STEP 2 — IF AVAILABLE: implement it as a Python code metric.**
Did a biomarker name or value appear in agent speech before a successful
verification event? Exact, free to run, no judge variance, no model dependency.
This is D13 in practice and the answer to "why not an LLM judge here?" is simply
that the question has a computable answer.

**STEP 3 — FALLBACK: Agent as a Judge. Fully documented, not apologised for.**
Put `{{trace}}` in the prompt. **No variable mapping is needed** — the docs
state there is nothing to map, that the same rule works whatever shape the trace
has, and they recommend this mode as the default starting point.

Judge tools available:
  - `read`
  - `jq`
  - `search` — finds text anywhere in the trace
  - `get_trace_spans` — **plural**; lists spans. There is no
    `get_trace_span` singular, and assuming one will waste a cycle.
  - `get_attachment` — fetches an attachment as image, audio or text

Write the rubric explicitly with a bounded output: a score from a fixed set plus
a short reason. Vague judges produce unusable scores.

**STEP 4 — Rule configuration. All verified against the REST schema.**
  - **`sampling_rate` is 0–1**, even though the UI displays a percentage. Set it
    to score every trace. Entering `100` where `1` is expected is an easy and
    invisible mistake.
  - **`trigger_scope` defaults to `"production"`, which is correct** — traces
    logged through the SDK are production traces. Do not change it.
  - **Set `max_cost_usd`.** On reaching the cap the judge wraps up and returns
    what it has. This is insurance against an agentic judge looping on a long
    trace, which is a real risk when the judge can call tools.
  - **Agent as a Judge needs a tool-calling model.** Without one it degrades to
    a single call over a truncated trace, which silently defeats the point.
    Choose the model deliberately.
  - The judge model is configured under **Workspace Settings → AI Providers**
    and can be any of OpenAI, Anthropic, Gemini, Bedrock, Ollama, or an
    OpenAI-compatible endpoint. **Pick a different model family from the
    conversational agent** (currently `openai/gpt-4.1-mini`), so the judge is
    not grading its own family's output — self-preference bias is well
    documented and free to avoid here.
  - **Do NOT use thread-scope rules.** Thread rules wait for a cooldown after
    last activity, defaulting to **15 minutes**, which would stall the demo
    completely. There is one call per patient, so trace scope is the right unit
    anyway.

**STEP 5 — Demo insurance.**
Rules only run on traces logged **after** the rule is created. But existing
traces can be selected in the UI and scored retroactively via the **brain
icon**. A call made before the rule existed therefore does not need re-dialling
— worth knowing before anyone re-runs a live call to India for no reason.

**STEP 6 — README.**
Explain what is measured, why that criterion for a healthcare use case, and why
it is scored deterministically rather than judged (D13). If Step 3 was taken
instead, say so plainly and give the judge's known weaknesses — self-consistency,
position bias, leniency — along with what an offline eval suite would add.

**Exit test:** a completed call produces a trace that is automatically scored,
and the score is visible in the Opik UI.

**STOP.**

---

## PHASE 8 — Attach telephony

**Goal:** the same agent, over a real phone call.

**Tasks**

1. Extend `dispatch.py` to: create a room, dispatch the agent to it, then place
   the outbound SIP call via `CreateSIPParticipant` using the existing trunk id.
   Use `wait_until_answered` semantics so failures surface immediately.
2. **One dial attempt per invocation. No retries. No loops.** (Rule 4.)
3. On SIP failure, print the upstream SIP status code and message with the layer
   diagnosis mapping already established in the telephony spike.
4. Confirm the call record, analysis, and Opik trace are all produced for a real
   phone call exactly as they were locally.
5. Enable call recording if LiveKit supports it straightforwardly; otherwise
   record the reference only. Do not spend more than 30 minutes here.
6. Tune for phone audio: phone calls are narrowband and transcription degrades,
   particularly on names and numbers. Add confirmation-readback to the prompt
   where a misheard value would matter (name confirmation, chosen slot).

**Exit test:** a real outbound call to the verified number completes, books an
appointment, and produces a full Opik trace with an eval score.

**STOP.**

---

## PHASE 9 — README and demo

**Goal:** the deliverables the brief names explicitly.

**Tasks**

1. `README.md` covering:
   - What it does, in three sentences
   - Architecture diagram (ASCII is fine) and the one-line justification for
     each component
   - Setup: prerequisites, env vars, Twilio trunk config, LiveKit trunk config,
     how to run locally and over the phone
   - **The Opik integration section**: how the seam works, how to enable and
     disable it, and the explicit claim that deleting the module leaves a
     working agent
   - The evaluation: what is measured and why
   - **Design decisions** — reproduce Section 3 with reasoning
   - **Known gaps** — reproduce Section 2's out-of-scope list with the approach
     that would be taken for each. Do not hide this section.
2. Demo recording: a short screen capture of a real call from dispatch through
   to the Opik trace and eval score. Include the Opik trace link.
3. Final check: fresh clone, follow the README exactly, confirm it works.

**Exit test:** someone else could set this up from the README alone.

---

## 5. Risky tasks — flagged for extra care

| Task | Risk | Handling |
|---|---|---|
| Phase 0 Opik recon | **Highest.** Unfamiliar SDK; API may differ from any assumption in this plan | Do it first; report honestly; the plan bends to the docs, not the reverse |
| Phase 7 online eval config | High. "Online evaluation" may mean something specific in the product | Confirm the exact mechanism before designing the metric |
| Phase 4 shutdown hook | High. Silent data loss if the process exits before analysis and flush complete | Test explicitly by inspecting output after a normal call end |
| Phase 6 audio attachment | Medium. Attachment support may not match expectation | Fall back to URI reference; the brief permits "or audio reference" |
| Phase 2a handoff | **Low — resolved in recon.** Handoff is documented and the audio session continues uninterrupted | Prove empirically in the Phase 2a exit test, scenarios 1, 2 and 5 |
| Version drift | **Medium.** Recon read framework source from `main`, which may differ from the released package | Pin an exact version in `requirements.txt`; re-confirm the API against the installed package at the start of Phase 2 |
| Phase 2 LiveKit Agents API | Medium. Framework has moved fast | Work from the current quickstart only |
| Phase 8 narrowband STT | Medium. Works locally, degrades on the phone | Confirmation-readback in the prompt |
| Any phase | Scope creep | Section 2 is the contract |

---

## 6. Sequencing note

Phases 1–7 need no telephony at all. If time runs short, a complete system
demonstrated locally with full Opik traces and evaluation is a **far** stronger
submission than a phone call with a thin observability layer. Telephony is
already proven; it is the least valuable remaining work per hour spent.
