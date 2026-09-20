# Outbound Healthcare Voice Agent

An AI agent that telephones a patient, **confirms who it is speaking to before
saying anything clinical**, reads out their recent test results, and books a
follow-up appointment through a tool call. After the call it produces a
structured outcome record and ships the whole thing — transcript, tool calls,
analysis — to [Opik](https://www.comet.com/docs/opik) for observability, where
an automated rule scores every trace for premature disclosure of health data.

Built on LiveKit Agents with Twilio SIP for telephony. It works over a real
phone call and locally in a console, with the same code.

---

**Companion documents:** [ARCHITECTURE.md](ARCHITECTURE.md) — the end-to-end
design: C4 context/container/component views, the call sequence, the
verification gate as a state machine, trust boundaries and failure modes.
[DECISIONS.md](DECISIONS.md) — the decision log, design
and implementation, each with the evidence that drove it.
[RISKS.md](RISKS.md) — the risk register, including four issues that occurred
during the build and how they were caught.

**Demo:** [`demo_recording.mp4`](../demo_recording.mp4) — a real
outbound call placed from the terminal, the post-call analysis, and the Opik
trace with its evaluation score. The two traces shown are exported alongside it
as [`clip1_opik_trace.json`](../clip1_opik_trace.json) (identity verified,
appointment booked) and [`clip2_opik_trace.json`](../clip2_opik_trace.json)
(identity **not** verified — note that no biomarker appears anywhere in that
transcript, and there is no `book_appointment` span).

## Contents

- [The idea in one picture](#the-idea-in-one-picture)
- [Setup](#setup)
- [Usage](#usage)
- [The Opik integration](#the-opik-integration)
- [The evaluation](#the-evaluation)
- [Design decisions](#design-decisions)
- [Known gaps](#known-gaps)
- [Tests](#tests)

---

## The idea in one picture

The central problem is that you are about to say something private to whoever
picks up the phone, and you do not yet know who that is.

```
  dispatch.py                 LiveKit room                    the patient
  ───────────                 ───────────                     ───────────
  1. dispatch agent  ────────▶ agent joins, waits
  2. place SIP call  ────────▶ ────────────── Twilio ─────────▶  ring ring

                              ┌──────────────────────────┐
                              │   UnverifiedAgent        │   "May I speak
                              │   • NO health data       │    with Meera?"
                              │   • NO booking tools     │
                              │   • one tool:            │   "Date of birth?"
                              │     verify_patient_…     │
                              └───────────┬──────────────┘
                                          │  correct DOB → the tool RETURNS
                                          │  a new agent object, and LiveKit
                                          ▼  hands off mid-call
                              ┌──────────────────────────┐
                              │   VerifiedAgent          │   "Your HbA1c is
                              │   • holds biomarkers     │    7.8 percent…"
                              │   • get_available_slots  │
                              │   • book_appointment     │   "Booked, code
                              │   • NOT given the DOB    │    APT-P001-8087"
                              └───────────┬──────────────┘
                                          │ call ends
                                          ▼
     CallRecord ──▶ analysis ──▶ ObservabilitySink ──▶ Opik ──▶ eval rule
     (transcript,   (deterministic   (no-op by          (trace,   (scores every
      tool calls)    + inferred)      default)           spans)    trace)
```

**Why the gate is two classes and not a prompt rule.** `UnverifiedAgent` is
constructed without the health payload at all. It is not told to keep a secret —
it does not have one. A model cannot disclose a biomarker it was never given, so
the guarantee is checkable by reading a constructor rather than by trusting an
instruction. On a correct answer the verification tool *returns a different
agent object*, and the framework swaps it in without dropping the audio.

| Component | Why it exists |
|---|---|
| `dispatch.py` | Something has to decide who gets called and when. One dial per run. |
| `src/agent.py` | The two agent classes and the worker entrypoint. |
| `src/prompts.py` | Two prompt builders. The unverified one never receives health data. |
| `src/verification.py` | Date/ID parsing and the attempt policy. Exact match, in code. |
| `src/booking.py` | Mock booking. Can legitimately fail or return no slots. |
| `src/events.py` | `CallRecord` — what one call produced, as plain data. |
| `src/analysis.py` | Post-call outcome: computed facts vs. model-inferred ones. |
| `src/sinks.py` | The observability seam. No-op by default. |
| `src/opik_integration.py` | The only file that imports Opik. Delete it and the agent still runs. |
| `src/eval_premature_disclosure.py` | The online eval metric, uploaded to Opik and run there. |
| `src/telephony.py` | Maps a SIP failure to the layer most likely at fault. |

---

## Setup

### Prerequisites

- **Python 3.12**
- A **LiveKit Cloud** project — https://cloud.livekit.io
- The **LiveKit CLI** (`lk`) — `brew install livekit-cli`
- A **Twilio** account with an Elastic SIP Trunk (only needed for real phone calls)
- An **Opik / Comet** account — https://www.comet.com/signup (free tier is enough)

### 1. Install

```bash
git clone https://github.com/anagharamadas/outbound-voice-agent.git
cd outbound-voice-agent/voice_agent
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Fill in `.env`. Every variable and where to find it:

| Variable | Where it comes from | Needed for |
|---|---|---|
| `LIVEKIT_URL` | LiveKit Cloud → project settings (`wss://…`) | everything |
| `LIVEKIT_API_KEY` | LiveKit Cloud → API keys | everything |
| `LIVEKIT_API_SECRET` | LiveKit Cloud → API keys | everything |
| `TWILIO_SIP_TERMINATION_DOMAIN` | Twilio → Elastic SIP Trunk → Termination. **Hostname only**, no `sip:` prefix | phone calls |
| `TWILIO_SIP_AUTH_USERNAME` | Twilio → trunk → Termination → credential list | phone calls |
| `TWILIO_SIP_AUTH_PASSWORD` | same credential list | phone calls |
| `TWILIO_PHONE_NUMBER` | the US number attached to the trunk, E.164 (`+1…`) | phone calls |
| `DESTINATION_PHONE_NUMBER` | the mobile to call, E.164 (`+91…`) | phone calls |
| `LIVEKIT_OUTBOUND_TRUNK_ID` | produced by step 3 below | phone calls |

Opik variables, also in `.env.example`:

| Variable | Where it comes from |
|---|---|
| `OPIK_API_KEY` | Comet → account settings → API keys |
| `OPIK_WORKSPACE` | your Comet **username** — see the warning below |
| `OPIK_PROJECT_NAME` | any name; the project is created on first write |
| `OPIK_ENABLED` | `true` to export, `false` to run with the no-op sink |

> **`OPIK_WORKSPACE` is the workspace, not the project.** In Opik the hierarchy
> is *workspace → projects → traces*, and your workspace is normally your Comet
> username. Putting a project name here fails with
> `401 User not allowed to access workspace!`, which does not name the real
> problem. If unsure, `curl -H "Authorization: $OPIK_API_KEY" https://www.comet.com/api/rest/v2/workspaces`.

Optional:

| Variable | Default | Effect |
|---|---|---|
| `ANALYSIS_ENABLED` | `true` | `false` skips the post-call LLM analysis (saves inference while iterating) |
| `CALL_RECORDING_ENABLED` | `false` | `true` starts a LiveKit egress per call — see [call recording](#call-recording) |
| `PATIENT_ID` | `P001` | which patient a **console** session calls |
| `CALL_AUDIO_DIR` | `console-recordings/` | where to look for a recording |

### 3. Create the LiveKit SIP trunk (phone calls only)

```bash
lk cloud auth
./create_trunk.sh --dry-run     # shows the config, redacted, creates nothing
./create_trunk.sh
```

Paste the printed `ST_…` id into `LIVEKIT_OUTBOUND_TRUNK_ID` in `.env`.

Twilio side, before this works: the trunk needs a **Termination** SIP domain, a
**credential list attached to that trunk**, and your US number listed under the
trunk's **Numbers**. For calls to India, Twilio → Voice → Settings →
**Geographic Permissions** must have India enabled — otherwise calls fail with
notification `32205`, which `src/telephony.py` recognises and explains.

### 4. Check the phone path on its own

```bash
./venv/bin/python test_call.py
```

Places **one** SIP call with no agent attached. Your phone ringing and then
silence is the successful outcome. On failure it prints the SIP status and names
the layer most likely at fault. **It does not retry** — nothing in this repo does.

---

## Usage

### Talk to the agent locally (no phone, no Twilio)

The fastest loop. Everything except telephony works here.

```bash
lk agent console --text src/agent.py     # type instead of talking
lk agent console src/agent.py            # speak
lk agent console --record src/agent.py   # speak, and save the audio
```

The demo patient is **P001, Meera Nair, date of birth 22 March 1988**. Say you
are Meera, give that date, accept an appointment. Change patient with
`PATIENT_ID=P002`.

Try the failure paths too — they are the interesting ones:

| Say this | What should happen |
|---|---|
| A wrong date, twice | Two attempts, then the call ends. No biomarker is ever spoken. |
| "This is Amida" | Polite close. It never says why it called. |
| "I don't remember" | Does **not** consume an attempt. Capped at 3 in a row. |
| Ask for results before verifying | Refused, in the same words every time. |

### Call a real phone

Two terminals. **The worker must be running first** — otherwise the dispatch
succeeds and nobody joins the room.

```bash
# terminal 1 — the agent worker; wait for "registered worker"
./venv/bin/python src/agent.py start
```

> If this fails with `address already in use` on port 8081, a worker is already
> running from an earlier attempt. `lsof -ti :8081 | xargs kill` and retry —
> only one worker at a time.

```bash
# terminal 2 — place the call
./venv/bin/python dispatch.py --patient-id P001            # dry run: prints the record, dials nothing
./venv/bin/python dispatch.py --patient-id P001 --call     # actually dials
```

**Dialling is opt-in.** Without `--call` it prints the patient record and exits.
A tool that rings a real phone and bills by the rounded minute should not fire
because someone pressed up-arrow and enter.

**One dial per invocation.** No retry, no backoff, no loop — anywhere. If the
call fails you get the SIP status and a diagnosis; re-dialling is your decision.

The call record, analysis and Opik trace are written by the **worker** process
when the call ends, so watch terminal 1, not terminal 2.

### What you get afterwards

```bash
ls call_records/          # one JSON per call: transcript, tools, analysis
```

Every call writes this file **regardless of whether Opik is enabled** — it is
the inspection artifact, and it deliberately does not depend on the thing being
inspected.

> `call_records/` and `console-recordings/` are gitignored. They contain
> biomarkers and full transcripts of a healthcare conversation.

---

## The Opik integration

**The requirement:** observability must be pluggable, and removing it must not
break the agent. The whole of that claim lives in one file.

```
   agent.py ──emits──▶ ObservabilitySink (a protocol, 3 methods)
                              │
                     build_sink() picks one
                    ╱                      ╲
            NoOpSink                  OpikSink
         (does nothing,          (src/opik_integration.py —
          successfully)           the ONLY file importing opik)
```

`src/agent.py` does not import Opik, does not name an Opik symbol, and does not
know it exists. The import sits **inside** the branch in `build_sink()`, not at
module scope — which is what makes "delete the file and it still runs" true
rather than aspirational. With Opik off, the module is never imported and the
package need not be installed.

### Turning it off

```bash
OPIK_ENABLED=false ./venv/bin/python src/agent.py start
```

That is the entire change. The call behaves identically, the record is still
written, the analysis still runs, and no trace appears. To go further, delete
`src/opik_integration.py` and remove `opik` from `requirements.txt` — the agent
keeps working, degrading loudly to the no-op sink rather than silently.

### What lands in a trace

- **One trace per call**, named `call P001 <timestamp>`
- **Trace input** — patient id, name, phone, the biomarkers passed in
- **A `conversation` span** holding the transcript turn by turn
- **One `tool` span per tool call**, with arguments, result and timestamp
- **The analysis**, plus tags you can filter on — `verified` / `unverified`,
  `booked` / `not-booked`, `outcome:…`, `end:…`
- **Numeric feedback scores** so a dashboard can average them across calls
- **The recording**, attached and playable for console calls — see below

### Two things worth knowing

**The transcript goes in a span, never in trace metadata.** Opik does not
truncate metadata, and it counts toward a request size cap, so a long transcript
there risks a `413` on ingestion. Span `input` is truncated safely.

**The flush is checked.** Opik logs from a background thread and reports a
dropped message by *returning `False`*, not by raising. An unchecked flush is a
silently empty project: the call sounds perfect, the process exits cleanly, and
nothing recorded that anything was lost. `on_call_end` returns an `EmitResult`
for exactly this reason, and a failure is logged at ERROR.

### Call recording

Console and phone calls differ, because the audio is in different places.

- **Console call** — the audio passes through your machine, so
  `lk agent console --record` writes `console-recordings/session-*/audio.ogg`.
  The agent finds it and **attaches it to the trace, playable in the Opik UI**.
- **Phone call** — the audio flows between LiveKit and the carrier and never
  touches your machine. There is no local file. With
  `CALL_RECORDING_ENABLED=true` the agent asks LiveKit for an audio-only
  **egress** and records the **reference** — the egress id, and where egress
  says it put the file. The trace carries `audio_egress_id` and an explicit
  `audio_is_attached: false`.

To get a *fetchable* file from a phone call, point the egress `file_outputs` at
an S3/GCP/Azure bucket in `start_call_recording()`. Without a bucket the file
lands on LiveKit's own egress server, which your process cannot read. The brief
permits "call recording **or** audio reference"; this is the reference.

> The job log shows `enable_recording: true` on every job. That is LiveKit
> Cloud's own session flag and produces **no** egress — verified, it yielded zero
> egress items. It is not the recording you are looking for.

### PII

Patient name, phone number, biomarker values and a full healthcare transcript
are sent to a **third-party SaaS**. That is acceptable for a demo against
synthetic records and **is not acceptable in production** without either
redacting identifiers before they leave the process, or running a
[self-hosted Opik](https://www.comet.com/docs/opik/self-host/overview) inside
the same trust boundary as the patient data. This is noted at the top of
`src/opik_integration.py` as well as here.

---

## The evaluation

One online rule, running on Opik's servers against every trace as it arrives.

**What it measures: premature disclosure.** Did the agent name a biomarker, or
speak one of its values, *before* identity was confirmed?

**Why that one.** It is the sharpest possible test of the gate, and a failure is
a genuine privacy incident rather than a style complaint. For a healthcare
system that is the thing worth watching.

**Why it is code and not an LLM judge.** The question is *decidable*. The turns
are ordered, verification is a recorded event, and the answer is a comparison.
Handing that to a judge would introduce position bias, leniency drift and
run-to-run disagreement into a safety check that has an exact answer — and a
judge that is 95% reliable is a poor instrument for a property that is simply
true or false. Judges are for questions with no computable ground truth.

**What it really is: a regression test.** `UnverifiedAgent` is constructed
without health data, so premature disclosure is *architecturally unavailable*,
not merely unlikely. A green score every day is the metric working. It fires the
day someone passes health data to the wrong constructor, adds a biomarker to the
unverified prompt, or removes the gate.

**Ordering is causal, not chronometric.** Measured on a real call, the first
biomarker-bearing turn is timestamped **7ms** after verification returned —
because LiveKit stamps a message when its turn *begins*, not when it is
delivered. Adjacent turns are a median of 11.7s apart. So turns are compared at
turn granularity, which has seconds of slack, rather than by raw timestamps,
which would have none.

### Installing the rule

```bash
./venv/bin/python register_eval_rule.py            # create or update
./venv/bin/python register_eval_rule.py --show     # list what exists
```

The metric body is `src/eval_premature_disclosure.py`, uploaded verbatim — so
the text under review and the text Opik runs are the same thing, rather than a
snippet pasted into a web form that nobody can diff. Re-running updates the rule
in place instead of stacking duplicates.

### Known weaknesses

- It matches biomarker **names and digit values**. An agent that spelled a
  reading out in words *and* avoided the name would slip past. The name is the
  reliable signal; both are matched, neither alone is sufficient.
- A trace with no readable turn data scores **0.0 with `NOT EVALUATED`** in the
  reason. That is visible on purpose — a safety metric that silently skips is
  indistinguishable from one that never ran — but it means a project average
  mixes *unsafe* with *unknown*. Filter on the reason before reading a mean.
- The post-call analysis also carries `agent_gave_medical_advice`, which is an
  **LLM** judgement and does fire on benign calls (reading a reference range
  aloud, deferring to a doctor). It ships with a `medical_advice_evidence` field
  quoting the words that triggered it, so a false alarm is dismissable at a
  glance rather than opaque.

**What an offline eval suite would add:** a fixed dataset of transcripts with
known-correct labels, run on every change, measuring the metric itself rather
than the calls. The online rule tells you what happened in production; an
offline suite tells you whether your detector still works. That is out of scope
here and named below.

---

## Design decisions

The full log — including the twelve implementation decisions forced during the
build, each citing the evidence that changed it — is in
**[DECISIONS.md](DECISIONS.md)**. Summary:

| # | Decision | Why |
|---|---|---|
| D1 | Cascaded STT → LLM → TTS, not speech-to-speech | Discrete, inspectable transcript and tool calls — which the observability and eval phases depend on. |
| D2 | Biomarker *interpretation* is precomputed in the data file | Safety. The model reads a pre-set status string; it never decides clinical meaning. |
| D3 | Identity gate before any biomarker is spoken | The core requirement. |
| D4 | `appointment_booked` comes from **tool evidence**, never from the LLM reading the transcript | Ground truth beats inference. A call can *sound* like a booking while the tool failed. |
| D5 | Observability behind a seam, no-op default | The brief demands pluggability. Deleting the Opik file must leave a working agent. |
| D6 | Online evaluation is a platform rule scoring traces as they arrive | The platform-native reading of the requirement. |
| D7 | The mock booking tool can fail and can return no slots | A tool that always succeeds proves nothing and gives the eval nothing to measure. |
| D8 | Stateless per call; dispatcher single-shot | Simplest thing that satisfies the brief. Scaling is a documented gap, not a built feature. |
| D9 | Verification is KBA — confirm the name, then ask for **one** identifier | Standard in healthcare telephony. One identifier only: each extra one multiplies the STT failure surface. |
| D10 | **Code-enforced gate via agent handoff** — two classes, health data in neither the prompt nor the unverified object | The agent cannot disclose what it was never constructed with. Checkable by reading a constructor. |
| D11 | Verification is a tool bound to the agent class | Tools reach agent state through `self`; also yields a deterministic, auditable event. |
| D12 | Health data passes only through `VerifiedAgent.__init__` — never `userdata`, tool names, descriptions or enums | Tool schemas are demonstrably sent to the model. Sidestep rather than reason about it. |
| D13 | Score deterministically where the property is decidable; reserve LLM judges for genuinely subjective questions | See [the evaluation](#the-evaluation). |

Two consequences worth calling out, because they look like bugs and are not:

- **The verified agent is never given the date of birth.** It confirmed identity
  a moment ago but cannot state, confirm or hint at the value — there is nothing
  to leak.
- **The unverified agent refuses in the same words every time.** Varying the
  refusal by *why* it refused would leak whether the person is a patient at all.

---

## Known gaps

Assessed with impact, likelihood and residual risk in
**[RISKS.md](RISKS.md)**. The table below is the scope view; the register is the
safety view, and R-08 (PHI to a third party) is marked as would-block-production.

Deliberately out of scope. Listed with the approach that would be taken, because
hiding them would be worse than not building them.

| Gap | What it would take |
|---|---|
| **Voicemail / answering machine detection** | LiveKit can surface early media; the real fix is an AMD model on the first 2–3s of audio, then hang up without speaking. Today a voicemail gets the opening line and nothing more — the gate still holds, since no one verifies. |
| **No-answer / busy retry policy** | Deliberately absent. Retries need a scheduler, a per-patient attempt budget and quiet-hours rules; a naive loop risks carrier blocking and bills per attempt. |
| **Batch dispatch across patients** | `dispatch.py` is one patient per invocation. Batching needs a queue, concurrency limits and per-call isolation so one failure cannot stall a campaign. |
| **Real calendar availability** | `src/booking.py` is a mock with a fixed slot list. Production means a real scheduling API, conflict resolution and holding a slot during the conversation. |
| **Human handoff / warm transfer** | A `transfer_call` tool plus a second SIP participant. The interesting part is policy — when to escalate, and what the agent says while transferring. |
| **PII redaction before traces leave the process** | Today the full transcript and biomarkers reach Opik. Production needs redaction at the sink boundary, or self-hosted Opik. See [PII](#pii). |
| **Offline eval dataset / regression suite** | A labelled transcript set run on every change, to test the *metric* rather than the calls. The online rule and an offline suite answer different questions. |
| **Any database, queue, cache or web server** | State is a JSON file and process memory. Anything multi-user needs real storage. |
| **Multi-agent orchestration** | Two agent classes and one handoff is the whole topology, and that is deliberate. |
| **Phone-call audio is a reference, not a file** | Needs a storage bucket. See [call recording](#call-recording). |


---

## Tests

```bash
./venv/bin/python test_phase4.py    # call record + the observability seam
./venv/bin/python test_phase5.py    # post-call analysis, deterministic vs inferred
./venv/bin/python test_phase6.py    # the Opik sink
./venv/bin/python test_phase7.py    # the evaluation metric
./venv/bin/python test_phase8.py    # telephony wiring — dials nothing
```

**156 checks on a fresh clone**, rising to 174 once you have made some calls —
several sections additionally run against every record in `call_records/`, which
a clone does not have. Those sections fall back to a synthetic fixture, so the
suites are self-contained; a real record is preferred when present because it
catches shapes a fixture would not think to produce.

**None of them place a call, and none write to your Opik project** — both
verified by snapshotting the project around a full run, after two incidents
where test runs polluted a live one.

Two suites can optionally hit the network:

```bash
./venv/bin/python test_phase5.py --live   # runs the real model over a saved call
./venv/bin/python test_phase6.py --live   # exports a real trace to Opik
```

The interesting tests are the ones asserting things that have never happened: a
transcript that claims an appointment while the tool failed, an agent naming a
biomarker before verification, a sink that drops data silently. Those cannot be
produced by making a call — the architecture forbids them — so they are
synthesised.
