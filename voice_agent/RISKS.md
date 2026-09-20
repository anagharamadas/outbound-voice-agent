# Risk Register

Outbound healthcare voice agent. Maintained alongside [DECISIONS.md](DECISIONS.md)
and the [README](README.md).

**Scope note.** This is a demonstrator running against synthetic
patient records. It is **not** a clinically assured system. A production
deployment in this space would need a clinical safety case under the applicable
regime — DCB0129/DCB0160 in the UK, ISO 14971 for device risk management, a
HIPAA security risk analysis in the US — signed off by a named clinical safety
officer. This register is structured to feed such a case, not to substitute for
one.

## How to read this

**Severity** is impact × likelihood, judged for a *production* deployment to
real patients — not for this demonstrator. That is deliberate: a risk that is
harmless here because the records are synthetic may be severe in the setting
this design is arguing for, and rating it at demo severity would hide that.

| Impact | Meaning in this system |
|---|---|
| **Critical** | Protected health information reaches the wrong person, or clinical harm results |
| **High** | A patient is misinformed, or a safety control fails silently |
| **Medium** | Service is degraded, or an operator is misled |
| **Low** | Inconvenience; no safety or privacy consequence |

**Status:** `Mitigated` · `Mitigated, residual accepted` · `Open — accepted` ·
`Open — would block production`

**Evidence** cites where the control is enforced and how it is proven. A control
with no evidence is an intention, not a control.

---

## Register

| ID | Risk | Impact | Likelihood | Status |
|---|---|---|---|---|
| [R-01](#r-01) | Health data disclosed to the wrong person | Critical | Low | Mitigated |
| [R-02](#r-02) | Agent gives medical advice or interprets a result | Critical | Low | Mitigated, residual accepted |
| [R-03](#r-03) | Legitimate patient wrongly refused | High | Medium | Mitigated, residual accepted |
| [R-04](#r-04) | Verification answer leaks through the prompt | Critical | — | Mitigated (occurred) |
| [R-05](#r-05) | Patient misinformed that an appointment was booked | High | Low | Mitigated |
| [R-06](#r-06) | Misheard name or appointment time on narrowband audio | Medium | Medium | Mitigated, residual accepted |
| [R-07](#r-07) | Voicemail receives the call | Medium | High | Open — accepted |
| [R-08](#r-08) | PHI sent to a third-party SaaS | Critical | Certain | Open — would block production |
| [R-09](#r-09) | Event-loop stall degrades live audio | Medium | Medium | Mitigated |
| [R-10](#r-10) | Observability fails silently | Medium | Low | Mitigated |
| [R-11](#r-11) | Test runs write to production telemetry | Medium | — | Mitigated (occurred twice) |
| [R-12](#r-12) | Repeated dialling triggers carrier blocking | Medium | Low | Mitigated |
| [R-13](#r-13) | Phone-call audio is unrecoverable | Low | Certain | Open — accepted |
| [R-14](#r-14) | Safety metric fires on benign calls | Medium | Certain | Open — accepted |
| [R-15](#r-15) | Single point of failure; no scale or recovery | Medium | Certain | Open — accepted (out of scope) |

---

### R-01
**Health data is disclosed to whoever answers, rather than to the patient**

The defining risk of the product. Someone other than the patient picks up — a
family member, a colleague, a wrong number — and hears a diagnosis-adjacent
reading.

**Impact:** Critical · **Likelihood:** Low · **Status:** Mitigated

**Controls**
1. **Architectural.** `UnverifiedAgent` is constructed without the health
   payload. The model cannot disclose a biomarker it was never given ([D10](DECISIONS.md#d10--code-enforced-gate-via-agent-handoff)).
2. **Procedural.** Name confirmation, then one knowledge-based identifier, exact
   match in code, capped at two wrong attempts ([D9](DECISIONS.md#d9--verification-is-knowledge-based-name-then-one-identifier)).
3. **Detective.** An online evaluation rule scores every trace for premature
   disclosure and is a regression test on control 1.
4. **Containment.** The refusal wording is identical whatever the reason, so a
   refusal does not reveal whether the person is a patient at all.

**Evidence:** `src/agent.py` (two classes); `src/verification.py`;
`src/eval_premature_disclosure.py`; `test_phase8.py` asserts no patient's date
of birth or biomarker name appears in any unverified prompt. Verified on a real
phone call: `no_premature_disclosure = 1.0`.

**Residual:** control 1 makes this architecturally unavailable rather than
merely unlikely. The residual is that someone *edits* it away — which is what
control 3 exists to catch.

---

### R-02
**The agent interprets a result, diagnoses, or advises on treatment**

**Impact:** Critical · **Likelihood:** Low · **Status:** Mitigated, residual accepted

**Controls**
1. Interpretation is **precomputed in the data file**. The model reads a
   pre-set status string and never derives clinical meaning ([D2](DECISIONS.md#d2--biomarker-interpretation-is-precomputed-in-the-data-file)).
2. Explicit prohibitions in the verified prompt: no diagnosis, no treatment
   advice, no speculation about cause or severity.
3. The post-call analysis flags `agent_gave_medical_advice` with the quoted
   words that triggered it.

**Evidence:** `data/patients.json` carries `status` per biomarker;
`src/prompts.py`; `src/analysis.py`.

**Residual accepted:** control 3 is an LLM judgement and over-fires — see
[R-14](#r-14). Controls 1 and 2 are the real protection; control 3 is a review
aid, not a gate.

---

### R-03
**A legitimate patient is wrongly refused**

The mirror of R-01, and the reason the gate is not simply made stricter.
Narrowband audio garbles dates; a patient who cannot get past verification gets
no care benefit from the call.

**Impact:** High · **Likelihood:** Medium · **Status:** Mitigated, residual accepted

**Controls**
1. An unparseable answer returns `could_not_understand` and **does not consume
   an attempt** — two coughs must not reject a real patient.
2. Consecutive non-answers are capped at 3, so a bad line ends the call rather
   than looping and billing indefinitely.
3. Patient ID is accepted as an alternative for a patient who cannot recall a
   date of birth.
4. The agent re-asks when it cannot make out the name.

**Evidence:** `src/verification.py` (`COULD_NOT_UNDERSTAND`, `consumed_attempt`);
`src/prompts.py`.

**Residual accepted:** exact match, no fuzzy matching. A patient who
misremembers their own date of birth is refused. That is the deliberate side to
err on, and the call ends by directing them to the clinic.

---

### R-04
**The verification answer leaks into the unverified agent's context**

**Impact:** Critical · **Status:** Mitigated — *this occurred and was fixed*

**What happened.** The unverified prompt contained a worked example — *"I'm
Meera, born 22nd March 1988"* — which was **P001's actual date of birth**. The
agent built specifically without the verification answer was holding it. The
gate itself was never bypassable (comparison happens in code), but the model
could have offered the date back to whoever answered, which is the disclosure
half of R-01.

**Introduced:** commit `a1ad874` (Phase 2a). **Detected:** during Phase 8, by a
leak check written for a different purpose. **Fixed:** commit `04e563d`.

**Controls now**
1. The example uses fictitious values matching no record, with an inline note
   explaining why, so it is not "tidied" back to realistic-looking data.
2. `test_phase8.py` asserts no patient's date of birth or biomarker name appears
   in any unverified prompt — for **every** record, not just the demo one.

**Lesson recorded:** realistic-looking example data in a prompt is a disclosure
surface. Prompt content needs the same review as code.

---

### R-05
**The patient is told an appointment was booked when it was not**

The transcript can read exactly like success while the booking tool failed.

**Impact:** High · **Likelihood:** Low · **Status:** Mitigated

**Controls**
1. `appointment_booked` is derived from **tool evidence**, never from the model
   reading the transcript ([D4](DECISIONS.md#d4--appointment_booked-comes-from-tool-evidence-never-from-the-llm)).
2. When the model's reading and the tool log disagree, the tool log wins **and
   the disagreement is recorded** as a `discrepancy` rather than reconciled.
3. The prompt requires the agent to state plainly that the appointment was *not*
   made when the tool reports failure, and forbids inventing a confirmation code.
4. The analysis model is shown the transcript **only** — never the tool log — so
   it cannot copy the deterministic answer back and make disagreement impossible.

**Evidence:** `src/analysis.py`; `test_phase5.py` section 2 constructs a record
whose transcript says *"Your appointment is booked"* while `book_appointment`
returned `backend_unavailable`, and asserts `appointment_booked` is false with a
discrepancy set.

---

### R-06
**A name or appointment time is misheard on narrowband audio**

Phone audio is narrow and degrades exactly on names, dates and numbers.

**Impact:** Medium · **Likelihood:** Medium · **Status:** Mitigated, residual accepted

**Controls**
1. Medical-vocabulary STT model (`deepgram/nova-3-medical`).
2. The agent re-asks when it cannot make out whether it is speaking to the
   patient.
3. The chosen day and time are **read back and confirmed** before booking.
4. The confirmation code is read back slowly after booking.

**Evidence:** `src/prompts.py`; `test_phase8.py` section 9.

**Residual accepted — and deliberately bounded:** readback is applied to the
name and the appointment slot, and is **explicitly forbidden for the identifier**.
Reading a date of birth back "to check it" hands the answer to whoever is
holding the phone, defeating R-01. The prompt says so in as many words.

---

### R-07
**Voicemail or an answering machine receives the call**

**Impact:** Medium · **Likelihood:** High · **Status:** Open — accepted

No answering-machine detection. A voicemail receives the opening line: *"This is
an automated call from Lakeside Family Clinic. May I speak with Meera?"*

**Why the impact is Medium and not Critical:** the gate holds. Voicemail never
verifies, so no biomarker is ever spoken. What leaks is the clinic's name and
that it called — real but far lower severity.

**Approach if built:** AMD on the first 2–3 seconds of audio, then hang up
without speaking. Listed in the README known gaps.

---

### R-08
**Protected health information is sent to a third-party SaaS**

Patient name, phone number, biomarker values and a full healthcare transcript
are transmitted to Opik.

**Impact:** Critical · **Likelihood:** Certain (by design) ·
**Status:** Open — **would block production**

**Acceptable here** because records are synthetic. **Not acceptable** for real
patients without one of:
1. Redaction at the sink boundary before data leaves the process, or
2. A [self-hosted Opik](https://www.comet.com/docs/opik/self-host/overview)
   deployment inside the same trust boundary as the patient data.

Option 2 preserves full observability and is the stronger answer for a covered
entity; option 1 is cheaper but costs debuggability precisely where it is most
needed.

**Evidence:** documented at the top of `src/opik_integration.py` and in the
README. **Deliberately not hidden** — recognising the constraint is the
deliverable at this stage.

---

### R-09
**Synchronous work stalls the event loop and degrades live audio**

**Impact:** Medium · **Likelihood:** Medium · **Status:** Mitigated

**Mitigated.** Opik's `flush()` blocks until delivery and held the agent's loop
for **1116ms**, caught by the framework's own detector. All sink calls now run
via `asyncio.to_thread`, at the seam rather than inside the Opik sink, so every
future sink inherits the protection ([I5](DECISIONS.md#i5--sink-calls-run-off-the-event-loop)).
Regression-tested with a heartbeat beside a deliberately slow sink.

**Also mitigated.** The lazy `import opik` inside `build_sink()` stalled the
loop **~1000ms at session start** — when the greeting should be going out, on
*every* call, because LiveKit spends one job process per call and a fresh
process has never imported it.

Moved to `setup_fnc`, which runs while a pre-warmed process sits idle with
nobody on the phone. Measured: **1001.7ms → 14.5ms** at session start, with
758ms now paid during warm-up. The lazy import is unchanged and still
function-scoped, so D5's "delete the file and it still runs" is untouched — the
prewarm is a pure optimisation that may fail freely.

**Evidence:** `src/sinks.py::prewarm`; `test_phase6.py` section 10b asserts that
a deleted or unimportable sink module leaves the prewarm a no-op and the agent
still building a sink, and section 11 asserts on the AST that nothing at module
scope imports Opik.

**Residual:** none known. The remaining loop-blocking risk is a future sink that
blocks in a way `asyncio.to_thread` does not cover.

---

### R-10
**Observability fails silently and nobody notices**

Opik logs from a background thread and reports a dropped message by **returning
`False`**, not by raising. An unchecked flush yields a silently empty project:
the call sounds perfect, the process exits cleanly, and nothing records that
anything was lost.

**Impact:** Medium · **Likelihood:** Low · **Status:** Mitigated

**Controls**
1. `on_call_end` returns an `EmitResult` and may not return `None`; a sink
   returning `None` is itself reported ([I3](DECISIONS.md#i3--on_call_end-returns-a-result-it-may-not-return-none)).
2. `GuardedSink` logs failures at ERROR with detail, then swallows them — the
   call continues, the operator still learns.
3. The local JSON record is written **first** and independently, so a complete
   record survives total sink failure ([I2](DECISIONS.md#i2--the-local-json-record-is-written-before-the-sink-is-called-and-never-depends-on-it)).
4. Flush timeouts are split — 3s for the trace, 15s for the attachment upload —
   after a 3s timeout reported data loss that had not happened.

**Evidence:** `src/sinks.py`; `test_phase4.py` sections 3–6 cover a sink that
raises, one that drops silently, and one that returns `None`.

---

### R-11
**Test runs write to production telemetry**

**Impact:** Medium · **Status:** Mitigated — *this occurred twice*

**What happened.** Two separate mechanisms, both writing junk traces to a live
Opik project:
1. The Phase 4 suite runs the real entrypoint, which loads `.env`, which set
   `OPIK_ENABLED=true` — **4 junk traces**.
2. Opik auto-instruments `BaseMetric.score()`, so every local call of the metric
   under test logged a trace — **15 junk traces**.

**Controls now**
1. `test_phase4.py` **forces** `OPIK_ENABLED=false` (not `setdefault` — `.env`
   is loaded by the code under test).
2. `test_phase7.py` sets `OPIK_TRACK_DISABLE=true` **before** importing `opik`.
3. Write-safety is verified by snapshotting the project before and after a full
   suite run, not assumed.

**Lesson recorded:** a test suite that loads the application's real environment
inherits its side effects. `setdefault` is not a guard when the value is already
set by the file the code under test loads.

---

### R-12
**Repeated dialling triggers carrier blocking or unexpected cost**

Bursts of short calls to India can trigger carrier-side filtering, and every
connected call bills as a rounded whole minute.

**Impact:** Medium · **Likelihood:** Low · **Status:** Mitigated

**Controls**
1. **One dial attempt per invocation.** No retry, no backoff, no loop anywhere
   in the dial path.
2. Dialling is opt-in behind `--call`; without it the dispatcher prints the
   record and exits.
3. A failed call reports the SIP status with a layer diagnosis and exits.
   Re-dialling is a human decision.

**Evidence:** `dispatch.py`; `test_phase8.py` section 6 asserts no `while`,
`for`, `retry` or `range` construct exists anywhere in the dial path.

---

### R-13
**Phone-call audio cannot be retrieved**

**Impact:** Low · **Likelihood:** Certain · **Status:** Open — accepted

A phone call's audio never touches the local machine, so recording requires a
LiveKit egress, and the egress writes to whatever storage the request names.
With no bucket configured it writes to LiveKit's own egress server.

Captured instead: the egress id and reported location. The brief permits "call
recording **or** audio reference". Console calls still attach playable audio.

**Upgrade path:** configure an S3/GCP/Azure bucket in `start_call_recording()`;
the same code then yields a fetchable file ([I10](DECISIONS.md#i10--phone-call-audio-is-a-reference-not-a-file)).

---

### R-14
**The medical-advice flag fires on benign calls**

**Impact:** Medium · **Likelihood:** Certain · **Status:** Open — accepted

`agent_gave_medical_advice` returns true on clean calls. Two observed triggers,
both correct behaviour by the agent:
- reading a reference range aloud (*"above the typical range of 4.0 to 5.6"*);
- deferring to a clinician (*"the doctor is the best person to discuss them
  with"*).

A flag that fires on every well-behaved call cannot narrow a review queue.

**Control:** `medical_advice_evidence` carries the agent's exact quoted words, so
a false alarm is dismissable in seconds rather than opaque. **Accepted
deliberately** in favour of safety bias — a missed disclosure costs more than a
dismissed alarm — with the cost recorded rather than hidden.

**If tightened:** remove the "when uncertain, answer true" instruction and
enumerate what is *not* advice. Risk of tightening: a subtly-worded violation
scores false.

---

### R-15
**Single point of failure; no scale, queue or recovery**

**Impact:** Medium · **Likelihood:** Certain · **Status:** Open — accepted (out of scope)

State is a JSON file and process memory. One invocation, one call. No queue, no
database, no concurrency, no resumption after a crash mid-call.

**Accepted** as an explicit scope boundary rather than an oversight — see
[D8](DECISIONS.md#d8--stateless-per-call-dispatcher-is-single-shot) and the
README known gaps, which names the approach for each.

---

## Risks that closed during the build

Kept for traceability — a register that only ever grows hides how issues were
actually found.

| Risk (from PLAN.md §5) | Outcome |
|---|---|
| Opik SDK differs from assumptions | **Closed.** Recon done first; every API verified against the installed package, not the docs. |
| "Online evaluation" means something specific | **Closed.** Confirmed by probing a live rule before designing the metric. `user_defined_metric_python` is available. |
| Silent data loss if the process exits before flush | **Closed.** `add_shutdown_callback` verified; budget raised to 30s; flush result checked. |
| Attachment support may not match expectation | **Closed with a caveat.** Console audio attaches; phone audio is a reference — [R-13](#r-13). |
| Handoff may drop the audio session | **Closed.** Proven on a real phone call; audio continued uninterrupted. |
| Version drift between docs and installed package | **Closed.** Versions pinned exactly; APIs re-confirmed against the installed package. |
| Narrowband STT degrades on the phone | **Closed with residual** — [R-06](#r-06). |
