# Architecture

End-to-end design of the outbound healthcare voice agent.

Companion documents: [README](README.md) — how to run it ·
[DECISIONS.md](DECISIONS.md) — why it is built this way ·
[RISKS.md](RISKS.md) — what could go wrong.

The views below follow the [C4 model](https://c4model.com) — context,
container, component — followed by behavioural, data, timing, deployment and
security views. Diagrams are ASCII so they render everywhere, including in a
terminal and in a diff.

---

## Contents

1. [The problem the architecture solves](#1-the-problem-the-architecture-solves)
2. [C4 Level 1 — System context](#2-c4-level-1--system-context)
3. [C4 Level 2 — Containers](#3-c4-level-2--containers)
4. [C4 Level 3 — Components](#4-c4-level-3--components)
5. [Module dependency graph](#5-module-dependency-graph)
6. [Runtime sequence — one call](#6-runtime-sequence--one-call)
7. [The verification gate as a state machine](#7-the-verification-gate-as-a-state-machine)
8. [Data model](#8-data-model)
9. [Shutdown timing budget](#9-shutdown-timing-budget)
10. [Deployment view](#10-deployment-view)
11. [Trust boundaries and data classification](#11-trust-boundaries-and-data-classification)
12. [Failure modes and degradation](#12-failure-modes-and-degradation)
13. [Extension points](#13-extension-points)

---

## 1. The problem the architecture solves

> You are about to say something private down a telephone line, and you do not
> yet know who is holding the handset.

Everything structural follows from that sentence.

A conventional design would put the health data in the agent and instruct it not
to reveal anything prematurely. That makes disclosure a *behavioural* property —
one that depends on a language model following an instruction while an
unpredictable human talks to it.

This design makes disclosure **structurally unavailable** instead. The agent
that answers the phone is constructed without the health data. It is not keeping
a secret; it does not have one. The guarantee is checkable by reading a
constructor rather than by testing a model's compliance.

Three properties fall out, and the rest of this document is mostly their
consequences:

| Property | Mechanism |
|---|---|
| Health data cannot leak before verification | Two agent classes; payload passed only to the second ([D10](DECISIONS.md#d10--code-enforced-gate-via-agent-handoff)) |
| Call outcome cannot be misreported | Deterministic facts from tool evidence, never from the transcript ([D4](DECISIONS.md#d4--appointment_booked-comes-from-tool-evidence-never-from-the-llm)) |
| Observability can be removed without breaking anything | One-way seam, no-op default, import inside the branch ([D5](DECISIONS.md#d5--observability-behind-a-seam-no-op-by-default)) |

---

## 2. C4 Level 1 — System context

Who and what this system talks to.

```
     ┌───────────────┐                         ┌────────────────────┐
     │   Clinic      │  runs a campaign        │   Patient          │
     │   operator    │─────────┐               │   (on a mobile)    │
     └───────────────┘         │               └─────────┬──────────┘
                               │                         │ speaks,
                               ▼                         │ hears results
                    ╔══════════════════════════════════╗ │
                    ║                                  ║ │
                    ║   Outbound Healthcare            ║◀┘
                    ║   Voice Agent                    ║
                    ║   (this system)                  ║
                    ║                                  ║
                    ╚══╦════════╦═══════════╦═══════╦══╝
                       │        │           │       │
        places calls   │        │ speech    │       │ traces,
        via SIP        │        │ + LLM     │       │ scores
                       ▼        ▼           ▼       ▼
                ┌──────────┐ ┌────────┐ ┌────────┐ ┌──────────┐
                │ Twilio   │ │LiveKit │ │ LLM /  │ │  Opik    │
                │ Elastic  │ │ Cloud  │ │ STT /  │ │ (Comet)  │
                │SIP Trunk │ │        │ │ TTS    │ │          │
                └────┬─────┘ └────────┘ └────────┘ └──────────┘
                     │ PSTN                              ▲
                     ▼                                   │ reads traces,
              ┌─────────────┐                    ┌───────┴────────┐
              │  Carrier    │                    │ Clinical /     │
              │  network    │                    │ QA reviewer    │
              └─────────────┘                    └────────────────┘
```

| External system | Purpose | Failure impact |
|---|---|---|
| **Twilio Elastic SIP Trunk** | Bridges LiveKit to the public phone network | No calls can be placed |
| **LiveKit Cloud** | Room hosting, agent dispatch, SIP participant, media routing | Total outage |
| **LLM / STT / TTS** | Reached via LiveKit Inference, one credential | Agent cannot converse |
| **Opik (Comet)** | Traces, analysis storage, online evaluation | **None** — degrades to no-op, calls continue |

Opik's failure impact is *none by design*. That is the point of the seam, not a
happy accident.

---

## 3. C4 Level 2 — Containers

Three processes. They do not share memory, and two of them are short-lived.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  OPERATOR'S MACHINE / CI                                                 │
│                                                                          │
│   ┌────────────────────┐          ┌──────────────────────────────────┐   │
│   │  dispatch.py       │          │  agent worker                    │   │
│   │  (CLI, short-lived)│          │  src/agent.py start              │   │
│   │                    │          │  (long-running, pre-forks job    │   │
│   │  • load + validate │          │   processes)                     │   │
│   │    patient         │          │                                  │   │
│   │  • dispatch agent  │──1──────▶│  registered as                   │   │
│   │  • place ONE call  │──2─┐     │  "healthcare-outbound"           │   │
│   │  • exit            │    │     │                                  │   │
│   └────────────────────┘    │     │  per job:                        │   │
│            │                │     │   UnverifiedAgent → VerifiedAgent│   │
│            │                │     │   CallRecorder                   │   │
│            │                │     │   analysis → sink                │   │
│            │                │     └───────────────┬──────────────────┘   │
│            │                │                     │                      │
│            │                │                     ▼                      │
│            │                │         call_records/<id>.json             │
│            │                │         (gitignored — PHI)                 │
│   ┌────────┴───────────┐    │                                            │
│   │ register_eval_     │    │                                            │
│   │ rule.py (one-off)  │    │                                            │
│   └────────────────────┘    │                                            │
└─────────────────────────────┼──────────────────────────────────────────--┘
                              │
        1. CreateAgentDispatch│(agent_name, room, metadata={"patient_id"})
        2. CreateSIPParticipant(trunk, number, room, wait_until_answered)
                              ▼
                      ┌────────────────┐
                      │ LiveKit Cloud  │
                      │  room: call-…  │
                      └────────────────┘
```

**Why the agent is dispatched before the call is placed.** If the phone rang
first, a patient could answer into an empty room. Ordering is asserted in
`test_phase8.py`.

**Why the patient id travels as dispatch metadata** rather than an environment
variable: one long-running worker serves calls to any patient without a restart.
`PATIENT_ID` survives only as the console fallback, where no dispatch exists.

**Why the dispatcher exits before the call ends.** It is not the owner of the
call. The record, analysis and trace are written by the *worker* process at
shutdown — which is why the README says to watch the worker's log.

---

## 4. C4 Level 3 — Components

Inside the agent worker, during one job.

```
                        ┌───────────────────────────┐
   job metadata  ──────▶│  entrypoint(ctx)          │
   {"patient_id"}       │  src/agent.py             │
                        └──┬─────────┬─────────┬────┘
                           │         │         │
          ┌────────────────┘         │         └──────────────┐
          ▼                          ▼                        ▼
  ┌───────────────┐        ┌──────────────────┐     ┌──────────────────┐
  │ patient.py    │        │ CallRecorder     │     │ build_sink()     │
  │ load + parse  │        │ events.py        │     │ sinks.py         │
  │ the record    │        │                  │     │                  │
  └──────┬────────┘        │ accumulates:     │     │  OPIK_ENABLED?   │
         │                 │  • turns         │     │   no  → NoOpSink │
         │                 │  • tool calls    │     │   yes → OpikSink │
         │                 │  • end reason    │     └──────────────────┘
         │                 └────────▲─────────┘
         │                          │ session events
         ▼                          │ (conversation_item_added, close)
  ┌──────────────────────────────────────────────────────────┐
  │  AgentSession  (LiveKit)                                 │
  │                                                          │
  │   ┌──────────────────┐   verify ok    ┌────────────────┐ │
  │   │ UnverifiedAgent  │───returns─────▶│ VerifiedAgent  │ │
  │   │                  │   an Agent     │                │ │
  │   │ prompts.py:      │   ⇒ handoff    │ prompts.py:    │ │
  │   │  unverified_…    │                │  verified_…    │ │
  │   │                  │                │                │ │
  │   │ tools:           │                │ tools:         │ │
  │   │  verify_patient_ │                │  get_available_│ │
  │   │    identity      │                │    slots       │ │
  │   │  end_call        │                │  book_appoint… │ │
  │   │                  │                │  end_call      │ │
  │   │ HOLDS: name only │                │ HOLDS:         │ │
  │   │ NOT: biomarkers  │                │  biomarkers    │ │
  │   │ NOT: booking     │                │ NOT: the DOB   │ │
  │   └────────┬─────────┘                └───────┬────────┘ │
  │            │ verification.py                  │booking.py│
  │            │ parse + exact match              │ mock     │
  └────────────┴──────────────────────────────────┴──────────┘
                                 │ call ends
                                 ▼
                    ┌────────────────────────┐
                    │ finish_call()          │
                    │ src/agent.py           │
                    └──┬──────┬──────┬───────┘
                       │      │      │
         write JSON ───┘      │      └─── analysis.py ──▶ sink.on_analysis
         (always, first)      │            deterministic
                              │            + inferred
                              └─── sink.on_call_end ──▶ Opik trace
```

**`VerifiedAgent` is never given the date of birth.** It confirmed identity a
moment ago, but holds no copy of the answer — so it cannot state it, confirm it,
or be talked into hinting at it.

---

## 5. Module dependency graph

Derived from the source, not drawn from memory. Arrows point from dependent to
dependency.

```
  LAYER 4   dispatch.py ─────────────┐         agent.py
  orchestr.      │                   │         │  │  │  │
                 │                   │         │  │  │  │
  ───────────────┼───────────────────┼─────────┼──┼──┼──┼──────────────
                 │                   │         │  │  │  │
  LAYER 3        │                   │         │  │  │  └──▶ sinks.py
  adapters       │                   │         │  │  │           ╎
                 │                   │         │  │  │           ╎ lazy,
                 │                   │         │  │  │           ╎ inside
                 │                   │         │  │  │           ▼ build_sink()
                 │                   │         │  │  │      opik_integration.py
  ───────────────┼───────────────────┼─────────┼──┼──┼───────────┼──────
                 │                   │         │  │  │           │
  LAYER 2        │                   │         │  │  └──▶ analysis.py
  logic          │                   │         │  │           │
  ───────────────┼───────────────────┼─────────┼──┼───────────┼─────────
                 │                   │         │  │           │
  LAYER 1        │                   │         │  └──▶ prompts.py    │
  data           │                   │         └─────▶ events.py ◀───┘
                 │                   │                    │  │
  ───────────────┼───────────────────┼────────────────────┼──┼─────────
                 │                   │                    │  │
  LAYER 0   config.py          telephony.py         patient.py  verification.py
  pure      (no internal deps — leaves)                booking.py
```

Three properties this graph has, each deliberate:

**`events.py` has no external dependencies at all.** A `CallRecord` is a plain
value. It could have come from a phone call, a replayed fixture or a test, and
nothing downstream can tell. That is what lets the observability sink be a pure
function of a finished record instead of a set of hooks in the call path.

**`sinks.py` has no module-level internal imports.** The `opik_integration`
import is *inside* `build_sink()` — verified: `sinks.py`'s module body imports
only `logging`, `os`, `dataclasses` and `typing`. This breaks what would
otherwise be a cycle (`sinks` needs `OpikSink`; `opik_integration` needs
`EmitResult`), and more importantly it is what makes "delete
`opik_integration.py` and the agent still runs" *true* rather than aspirational:
with Opik disabled the module is never imported and the package need not be
installed.

**`agent.py` never imports `opik`.** Asserted by a test, not by inspection.

---

## 6. Runtime sequence — one call

```
operator   dispatch.py     LiveKit      worker/agent    patient      Opik
   │            │             │              │            │           │
   ├─ --call ──▶│             │              │            │           │
   │            ├─ dispatch ─▶│              │            │           │
   │            │             ├─ job ───────▶│            │           │
   │            │             │              ├ load patient           │
   │            │             │              ├ build sink ────────────┤
   │            │             │              ├ start recording (opt)  │
   │            │             │              ├ join room  │           │
   │            ├─ SIP call ─▶│              │            │           │
   │            │             ├──── PSTN ────────────────▶│  ☎ ring   │
   │            │             │              │◀─ answer ──┤           │
   │            │◀─ answered ─┤              │            │           │
   │◀─ exits ───┤             │              │            │           │
   │                          │              ├─ "May I speak with…" ─▶│
   │                          │              │◀─ "Speaking."──────────┤
   │                          │              ├─ "Date of birth?" ────▶│
   │                          │              │◀─ "22 March 1988" ─────┤
   │                          │              │                        │
   │                          │              ├ verify_patient_identity│
   │                          │              │   exact match in code  │
   │                          │              │   ⇒ returns VerifiedAgent
   │                          │              ├ ═══ HANDOFF ═══        │
   │                          │              │   audio uninterrupted  │
   │                          │              │                        │
   │                          │              ├─ "Your HbA1c is 7.8" ─▶│
   │                          │              ├ get_available_slots    │
   │                          │              ├─ "Monday at ten?" ────▶│
   │                          │              │◀─ "Yes please" ────────┤
   │                          │              ├ book_appointment       │
   │                          │              ├─ "Code APT-P001-…" ───▶│
   │                          │              │◀─ hangs up ────────────┤
   │                          │              │                        │
   │                          │              ├ SHUTDOWN CALLBACK      │
   │                          │              │  1 write JSON ─────────┼──▶ disk
   │                          │              │  2 sink.on_call_end ───┼──▶ trace
   │                          │              │  3 wait for audio      │
   │                          │              │  4 analyse transcript  │
   │                          │              │  5 sink.on_analysis ───┼──▶ update
   │                          │              │                        │    + score
```

Steps 1–5 are ordered by *cost of loss*, not by convenience — see
[§9](#9-shutdown-timing-budget).

---

## 7. The verification gate as a state machine

The safety-critical path. Transitions are enforced in code, not by prompt.

```
                        ┌──────────────┐
                        │   ANSWERED   │  agent speaks first
                        └──────┬───────┘  (outbound: nobody else will)
                               │
                   ┌───────────┴────────────┐
       "not Meera" │                        │ "speaking"
                   ▼                        ▼
           ┌───────────────┐        ┌────────────────┐
           │ WRONG PERSON  │        │ AWAITING       │◀────────┐
           │               │        │ IDENTIFIER     │         │
           │ close warmly, │        └───────┬────────┘         │
           │ state NO      │                │                  │
           │ purpose       │      verify_patient_identity       │
           └───────┬───────┘                │                  │
                   │            ┌───────────┼───────────┐      │
                   │            │           │           │      │
                   │     unparseable    no match     MATCH     │
                   │            │           │           │      │
                   │            ▼           ▼           │      │
                   │   ┌─────────────┐ ┌──────────┐     │      │
                   │   │ COULD NOT   │ │ attempts │     │      │
                   │   │ UNDERSTAND  │ │   += 1   │     │      │
                   │   │             │ └────┬─────┘     │      │
                   │   │ does NOT    │      │           │      │
                   │   │ spend an    │  < 2 ─┴─ = 2     │      │
                   │   │ attempt     │   │       │      │      │
                   │   │ cap: 3 in   │   └───────┼──────┴──────┘
                   │   │ a row       │           │      │
                   │   └──────┬──────┘           ▼      │
                   │          │ 3rd       ┌───────────┐ │
                   │          ▼           │ EXHAUSTED │ │
                   │   ┌────────────┐     └─────┬─────┘ │
                   └──▶│ END CALL   │◀──────────┘       │
                       │ no biomarker ever spoken       │
                       └────────────┘                   │
                                                        ▼
                                              ┌───────────────────┐
                                              │ ═══ HANDOFF ═══   │
                                              │ VerifiedAgent     │
                                              │ constructed WITH  │
                                              │ biomarkers, WITH  │
                                              │ booking tools,    │
                                              │ WITHOUT the DOB   │
                                              └───────────────────┘
```

Three properties worth naming:

**An unparseable answer does not spend an attempt.** Two coughs must not reject
a real patient. But unbounded non-answers would loop and bill forever, so
consecutive ones are capped at 3 ([R-03](RISKS.md#r-03)).

**Every refusal uses identical wording.** Varying it by *reason* would reveal
whether the person is a patient at all.

**The left-hand exits reach `END CALL` without any biomarker ever being
spoken** — and not because the agent chose well. `UnverifiedAgent` has no
biomarker to speak.

---

## 8. Data model

```
CallRecord  ── the unit of everything downstream ──────────────────────┐
                                                                       │
  call_id, room_name                    identity of the call           │
  patient_id, patient_name              who                            │
  biomarkers: (Biomarker, …)            what was to be communicated    │
  started_at, ended_at, duration_s      when                           │
  end_reason                            how it finished                │
                                                                       │
  transcript: (TranscriptTurn, …)  ─── role, text, at, interrupted     │
  tool_invocations: (ToolInvocation,…) ─ name, arguments, result,      │
                                          at, succeeded                │
  verification_attempts: (…)       ─── outcome, identifier_kind,       │
                                        stated, attempt_number,        │
                                        consumed_attempt               │
                                        (NEVER the expected value)     │
  audio_path        local file OR remote URI                           │
  audio_egress_id   set when the recording is remote                   │
  analysis          filled after the call ───────────────────────┐     │
└─────────────────────────────────────────────────────────────---│-----┘
                                                                 │
CallAnalysis ────────────────────────────────────────────────────┘
  ┌──────────────────────────────┬──────────────────────────────────┐
  │ DETERMINISTIC                │ INFERRED                         │
  │ computed from the record     │ an LLM reading the transcript    │
  │ exact, free, reproducible    │ no computable ground truth       │
  ├──────────────────────────────┼──────────────────────────────────┤
  │ appointment_booked           │ outcome_category   (closed enum) │
  │ confirmation_id              │ patient_sentiment  (closed enum) │
  │ identity_verified            │ objection_reason                 │
  │ verification_attempts        │ biomarkers_communicated          │
  │ verification_outcome         │ agent_gave_medical_advice        │
  │ tool_call_count              │ medical_advice_evidence          │
  │ call_duration_seconds        │ summary                          │
  └──────────────┬───────────────┴────────────────┬─────────────────┘
                 │                                │
                 └────────▶ discrepancies ◀───────┘
                    where the two disagree.
                    The deterministic value WINS;
                    the disagreement is RECORDED, not reconciled.
```

**The split is the point.** A call can *sound* exactly like a successful booking
while the tool failed — the agent offered a slot, the patient agreed, everyone
was pleased. The transcript genuinely reads as success. The booking system is
right and the transcript is not.

**The analysis model is shown the transcript only, never the tool log.** Hand it
the booking result and it copies the deterministic answer back, making
disagreement impossible and the check worthless.

**The expected verification value is never stored.** The record is bound for a
third-party platform; what the patient *said* is already in the transcript, so
keeping it adds no disclosure — what they *should have said* would.

---

## 9. Shutdown timing budget

The most constrained part of the system. The process is about to exit, and
everything that must outlive it happens here.

```
  call ends
      │
      ▼
  ┌─────────────────────────────────────────────────────────────┐
  │ shutdown callback              budget: 30s (raised from 10) │
  │                                                             │
  │  1. write JSON             ~ms     ◀── cheapest, certain    │
  │  2. sink.on_call_end     ≤  3s     ◀── the trace            │
  │  3. wait for audio       ≤ 1.5s    ◀── may not exist        │
  │  4. analyse transcript   ≤  6s     ◀── the only slow step   │
  │  5. sink.on_analysis     ≤ 15s     ◀── waits on an upload   │
  │                          ────────                           │
  │              worst case  ≈ 25.5s   inside 30s               │
  └─────────────────────────────────────────────────────────────┘
```

**Ordered by cost of loss.** Losing the analysis costs a summary and a sentiment
label. Losing the record costs the call. They are not worth the same, so they
are not ordered arbitrarily — the record is written first and depends on
nothing.

**Why 30s and not the default 10s.** `AgentServer` defaults
`shutdown_process_timeout` to 10.0s, sized for an agent that does nothing once
the call ends. This one does four things. Overrunning means the supervisor kills
the process *mid-flush*, losing exactly the telemetry the shutdown exists to
deliver. Every step remains independently capped, so the ceiling rises without
removing a guard ([I4](DECISIONS.md#i4--the-shutdown-budget-is-raised-from-10s-to-30s)).

**Every sink call runs via `asyncio.to_thread`.** Opik's `flush()` blocks until
delivery and held the event loop for 1116ms on a real call. The offload is at
the *seam*, so every future sink inherits it rather than each having to remember
([I5](DECISIONS.md#i5--sink-calls-run-off-the-event-loop)).

---

## 10. Deployment view

```
   DEVELOPER LOOP                        PHONE CALL
   ──────────────                        ──────────
   lk agent console --text               terminal 1: agent.py start
        │                                     │ registers with LiveKit Cloud
        ▼                                     │ pre-forks job processes
   local process, no room,                    │
   no LiveKit registration,              terminal 2: dispatch.py --call
   no telephony, no cost                      │ dispatch + one SIP call
        │                                     ▼
        └──▶ same agent code ────────────▶ LiveKit Cloud room
             same CallRecord                  │
             same analysis                    ▼
             same sink                   Twilio trunk ──▶ PSTN ──▶ handset
```

**The console path is not a simulation.** It runs the same entrypoint, the same
agent classes, the same recorder, the same analysis and the same sink. What it
omits is telephony. That is why most of this system could be built and tested
before a phone was ever dialled.

| | Console | Phone |
|---|---|---|
| LiveKit registration | no | yes (`agent_name`) |
| Patient selected by | `PATIENT_ID` | dispatch metadata |
| Audio recording | local file, attachable | egress reference ([R-13](RISKS.md#r-13)) |
| Cost per run | inference only | inference + per-minute telephony |

**One worker at a time.** A second binds port 8081 and fails with
`address already in use`.

---

## 11. Trust boundaries and data classification

```
  ┌─ TRUST BOUNDARY: this process ────────────────────────────────┐
  │                                                               │
  │   patients.json ──▶ Patient                                   │
  │                      ├── identity ──────▶ prompt  [NAME ONLY] │
  │                      ├── verification ──▶ tool memory  ⚠ never │
  │                      │                    reaches a prompt    │
  │                      └── health ────────▶ VerifiedAgent only  │
  │                                                               │
  └──────────┬────────────────────────────────────┬───────────────┘
             │                                    │
             ▼                                    ▼
   ┌───────────────────┐                 ┌────────────────────────┐
   │ call_records/     │                 │ Opik (THIRD PARTY)     │
   │ local disk        │                 │                        │
   │ gitignored        │                 │ name, phone, biomarker │
   │                   │                 │ values, full transcript│
   │ PHI, unencrypted  │                 │                        │
   └───────────────────┘                 │ ⚠ R-08: acceptable for │
                                         │   synthetic records.   │
                                         │   NOT for real patients│
                                         │   without redaction or │
                                         │   self-hosting.        │
                                         └────────────────────────┘
```

| Class | Data | Where it may go |
|---|---|---|
| **Identity** | name, first name, language | Unverified prompt — the minimum needed to ask for the right person |
| **Verification** | date of birth, patient ID | Tool memory only. Never a prompt, never a log, never a trace, never a terminal |
| **Health** | biomarker names, values, status | `VerifiedAgent.__init__` only, post-gate |
| **Derived** | transcript, tool log, analysis | Local disk and Opik |

**Why the verification value is handled most strictly of all** — more strictly
than the health data it protects: it is the *key*. Health data leaks one
patient's results; the verification value leaks the ability to obtain them.

**A slot id is an opaque string** (`SLOT-A`), not an enum of real appointment
times. Tool schemas are demonstrably sent to the model; putting live data in one
widens the surface for no benefit ([D12](DECISIONS.md#d12--health-data-passes-only-through-verifiedagent__init__)).

---

## 12. Failure modes and degradation

What breaks, and what survives.

```
  FAILURE                        CALL      RECORD    ANALYSIS   TRACE
  ─────────────────────────────  ────────  ────────  ─────────  ────────
  Opik unreachable / bad key     ✓ fine    ✓ written ✓ runs     ✗ logged ERROR
  opik_integration.py deleted    ✓ fine    ✓ written ✓ runs     ✗ no-op
  Sink raises                    ✓ fine    ✓ written ✓ runs     ✗ logged ERROR
  Sink returns None              ✓ fine    ✓ written ✓ runs     ✗ reported
  Analysis model times out       ✓ fine    ✓ written ✗ 6s cap   ✓ sent
  Disk unwritable                ✓ fine    ✗ logged  ✓ runs     ✓ sent
  Booking backend fails          ✓ fine    ✓ honest  ✓ flags it ✓ sent
  VerifiedAgent construct fails  ✗ ends    ✓ written ✓ runs     ✓ sent
  SIP dial fails                 ✗ no call ✗ none    —          —
```

**The call is the last thing to break.** Every observability failure is logged
loudly and swallowed: the patient on the line is unaffected by a telemetry
problem, and the operator still learns the record was lost. Those are not in
tension.

**One deliberate exception.** If `VerifiedAgent` construction fails *after* a
successful verification, the call is ended. An agent that believes it verified
but holds no data will improvise — the worst outcome available. It fails closed.

---

## 13. Extension points

Where the design expects to be changed, and what each costs.

| Extension | Where | Cost |
|---|---|---|
| **A different observability platform** | Implement `ObservabilitySink`, add a branch to `build_sink()` | One file. Nothing else changes. |
| **Real booking backend** | Replace `src/booking.py` | Same return contract: confirmation or *structured* failure, never an exception. |
| **More evaluation rules** | Add a metric file, register it | Rules are per-project; the metric ships from a file, not a web form. |
| **PHI redaction** | Wrap the sink | The seam is already the choke point — every outbound field passes through it. |
| **Self-hosted Opik** | `OPIK_URL_OVERRIDE` | Config only. Closes [R-08](RISKS.md#r-08). |
| **Fetchable phone-call audio** | Point egress `file_outputs` at a bucket | Config plus credentials. Closes [R-13](RISKS.md#r-13). |
| **Batch campaigns** | Above `dispatch.py` | Needs a queue, concurrency limits, per-call isolation. Out of scope ([D8](DECISIONS.md#d8--stateless-per-call-dispatcher-is-single-shot)). |

**The seam is deliberately narrow** — three methods. A wide interface would
defeat its purpose: every method is one the agent must call at the right moment,
and each addition is another thing a future sink must implement correctly to
avoid silently doing nothing.
