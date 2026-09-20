# Decision Log

Architecture decision records for the outbound healthcare voice agent.

Two kinds of entry. **Design decisions (D1–D13)** were taken up front, before
implementation, and are the contract the build was held to. **Implementation
decisions (I1–I12)** were forced during the build, usually by something the
documentation did not say or the first approach did not survive; each names the
evidence that changed the decision.

An entry is recorded when the choice was *not* obvious, when a reasonable
engineer would have chosen differently, or when the reason would otherwise be
lost. Entries are not revised in place — a superseded decision keeps its record
and gains a pointer.

**Status key:** `Accepted` · `Accepted, with known cost` · `Superseded`

---

## Design decisions

### D1 — Cascaded STT → LLM → TTS, not a speech-to-speech model
**Status:** Accepted

A speech-to-speech model would be lower latency and sound more natural.

**Chosen anyway:** the pipeline produces a discrete transcript and discrete tool
calls. Every later phase depends on that — the call record, the deterministic
outcome analysis, the trace span tree, and an evaluation rule that compares turn
ordering. A speech-to-speech model yields audio and a weaker transcript, and the
observability requirement is a graded part of the brief.

**Cost:** measurably more latency per turn.

---

### D2 — Biomarker interpretation is precomputed in the data file
**Status:** Accepted

The model reads a pre-set status string (`"above target range"`). It never
decides what a reading means.

**Why:** clinical interpretation by a language model is the highest-severity
failure available in this system. Removing the capability is stronger than
instructing against it. See [R-02](RISKS.md#r-02).

---

### D3 — Identity gate before any biomarker is spoken
**Status:** Accepted

The core requirement. Implementation is D10.

---

### D4 — `appointment_booked` comes from tool evidence, never from the LLM
**Status:** Accepted

**Why:** a call can *sound* exactly like a successful booking while the tool
failed — the agent offered a slot, the patient agreed, everyone was pleased, and
the backend returned an error. The transcript genuinely reads as success.

The booking system is right and the transcript is not. When the model's reading
and the tool log disagree, the tool log wins **and the disagreement is recorded**
rather than reconciled, because "the model believed something the system
contradicts" is the most interesting signal the analysis produces.

---

### D5 — Observability behind a seam, no-op by default
**Status:** Accepted

`src/opik_integration.py` is the only file importing Opik, and the import is
*inside* the branch in `build_sink()` rather than at module scope.

**Why module scope would have failed:** with a top-level import, deleting the
file breaks `src/sinks.py`, and "the agent still runs without Opik" becomes a
claim rather than a fact. The lazy import makes it literally true — the package
need not even be installed.

**Cost:** the first call pays an ~888ms import stall. See [I11](#i11--accept-an-888ms-import-stall-rather-than-couple-the-agent-to-opik) and [R-09](RISKS.md#r-09).

---

### D6 — Online evaluation is a platform rule, not a local script
**Status:** Accepted

Scoring happens on Opik's servers, against every trace as it arrives. A local
script would be an *offline* evaluation wearing the wrong name.

---

### D7 — The mock booking tool can fail and can return no slots
**Status:** Accepted

**Why:** a tool that always succeeds proves nothing. The honest-failure path —
where the agent must tell a patient the appointment was *not* made — is the one
worth demonstrating, and it gives the analysis a disagreement case to detect.

---

### D8 — Stateless per call; dispatcher is single-shot
**Status:** Accepted, with known cost

No queue, no database, no concurrency. One invocation, one call.

**Cost:** does not scale. Recorded as a gap rather than hidden. See
[README known gaps](README.md#known-gaps).

---

### D9 — Verification is knowledge-based: name, then one identifier
**Status:** Accepted

Confirm the name, then ask the person to *state* their date of birth (patient ID
accepted as an alternative).

**One identifier, not two:** each additional one multiplies the speech
recognition failure surface. On narrowband telephony that is a real cost paid by
legitimate patients, not a theoretical one.

---

### D10 — Code-enforced gate via agent handoff
**Status:** Accepted

Two agent classes. `UnverifiedAgent` is constructed **without** the health
payload and **without** booking tools. On a correct identifier the verification
tool *returns a `VerifiedAgent` instance*, and the framework swaps it in
mid-call without dropping audio.

**Why not a prompt instruction:** a prompt says "do not reveal this" and hands
the model the thing not to reveal. This construction does not give it to the
model at all. The agent cannot disclose what it was never constructed with, and
the guarantee is checkable by reading a constructor rather than by trusting an
instruction to survive an adversarial caller.

**Rejected alternative:** mutating the running agent's context after
verification. Whether injected context takes effect on the next generation is
provider-dependent and was not documented; handoff is documented and was proven
empirically.

---

### D11 — Verification is a tool bound to the agent class
**Status:** Accepted

Not a module-level function. Tools reach agent state through `self`, and the
call produces a deterministic, timestamped, auditable event.

---

### D12 — Health data passes only through `VerifiedAgent.__init__`
**Status:** Accepted

Never in `userdata`, never in a tool name, description, parameter name or enum
value.

**Why:** tool schemas are demonstrably sent to the model. Whether `userdata` is
serialised into the prompt could not be verified from the documentation — so the
design sidesteps the question rather than reasoning about it. A slot id is an
opaque string (`SLOT-A`), not an enum of real appointment times, for the same
reason.

---

### D13 — Score deterministically where the property is decidable
**Status:** Accepted

LLM-as-a-judge only where the question has no computable ground truth.

**Why:** whether a biomarker was named before verification is *decidable* — the
turns are ordered, verification is a recorded event, the answer is a comparison.
A judge would inject position bias, leniency drift and run-to-run disagreement
into a safety check that has an exact answer. A judge that is 95% reliable is a
poor instrument for a property that is simply true or false.

Judges are reserved for genuinely subjective questions, such as whether an
explanation was clear and appropriately non-alarming.

---

## Implementation decisions

### I1 — The observability sink is a pure function of a finished record
**Status:** Accepted
**Evidence:** Opik SDK reference — `Opik.trace()` and `Opik.span()` both accept
`start_time` and `end_time`.

Because a trace can be created after the fact with its real historical
timestamps, the whole call is assembled in memory and emitted in one burst at
the end. Live instrumentation is not required.

**Consequence:** the sink never touches the call path, which is what keeps D5
honest. Wiring Opik in as live hooks would have destroyed exactly the modularity
the brief grades.

---

### I2 — The local JSON record is written before the sink is called, and never depends on it
**Status:** Accepted

Ordering in `finish_call()`: write the record → emit the trace → wait briefly
for audio → analyse → rewrite.

**Why:** the record costs milliseconds and depends on nothing. Losing the
analysis costs a summary; losing the record costs the call. They are not worth
the same, so they are not ordered arbitrarily. The inspection artifact must not
depend on the thing being inspected.

---

### I3 — `on_call_end` returns a result; it may not return `None`
**Status:** Accepted
**Evidence:** Opik's `flush()` returns `False` on a dropped message rather than
raising.

A sink that cannot report failure turns the highest-severity silent failure in
the build into one with no symptom at all — the call sounds perfect, the process
exits cleanly, and the project is simply empty.

Decided in Phase 4, before Opik existed, because a contract with no success
channel cannot carry a flush result later and retrofitting a return type across
a wired seam is the rework the plan exists to avoid.

---

### I4 — The shutdown budget is raised from 10s to 30s
**Status:** Accepted
**Evidence:** `AgentServer` defaults `shutdown_process_timeout` to 10.0s;
`job_proc_lazy_main` gathers shutdown callbacks with no timeout of its own, and
its source comment notes that a hung callback is how jobs hit that deadline.
Measured: the analysis took 2.1–4.4s on a real call.

The default is sized for an agent that does nothing once the call ends. This one
flushes a trace, waits for a recording, analyses a transcript and flushes again
— worst case ~25.5s. Every step remains independently capped, so the ceiling
rises without removing a guard.

---

### I5 — Sink calls run off the event loop
**Status:** Accepted
**Evidence:** the framework's loop-blocking detector, on a real call:
*"event loop blocked for 1116ms … move it to a thread or an async client"*.

`asyncio.to_thread` at the **seam**, not inside the Opik sink. A sink is a plain
synchronous object on purpose — writing one should stay easy — and requiring
each implementation to remember not to block is a contract nobody can keep. The
caller offloads; every future sink inherits it.

---

### I6 — Disclosure ordering is decided causally, not by timestamp
**Status:** Accepted
**Evidence:** on a real call, the first biomarker-bearing turn was timestamped
**7.2ms** after verification returned. Adjacent turns are a median of **11.7s**
apart. The sub-second gaps are an artifact: LiveKit stamps a message when its
turn *begins*, not when it is delivered.

The naive metric — compare two timestamps, take the smaller — passes on the
recorded call by 7ms and is the wrong thing to build. Turn-granularity
comparison has seconds of slack.

---

### I7 — The evaluation metric ships from a file, not a web form
**Status:** Accepted

`src/eval_premature_disclosure.py` is uploaded verbatim by
`register_eval_rule.py`, so the text under review and the text Opik executes are
the same thing rather than a snippet pasted into a UI months ago that nobody can
diff. Re-running updates in place instead of stacking duplicate rules.

---

### I8 — A trace that cannot be evaluated scores 0.0, visibly
**Status:** Accepted, with known cost
**Evidence:** returning `scoring_failed=True` caused Opik to reject the result —
*"the provided 'code' field didn't return any usable ScoreResult"* — leaving the
trace with **no score at all** and only a line in the rule log.

A safety metric that silently skips is indistinguishable from one that never
ran. It now lands a visible `0.0` whose reason leads with `NOT EVALUATED` and
states that it is not a violation.

**Cost:** on a higher-is-better scale that sits alongside real violations, so a
project average mixes *unsafe* with *unknown*. Filter on the reason before
reading a mean.

---

### I9 — The analysis re-sends the full trace payload instead of updating it
**Status:** Accepted
**Evidence:** the Opik SDK warns — *"Calling Trace.update() shortly after
creation with batching enabled may cause data loss"* — and the documentation's
remedy is to re-send the full payload under the same id, which the backend
upserts.

Removes the race rather than timing around it. Reusing Opik's own id is not the
same as generating one, so the UUIDv7 requirement is intact.

---

### I10 — Phone-call audio is a reference, not a file
**Status:** Accepted, with known cost
**Evidence:** the job carries `enable_recording: true`, and that produced **zero**
egress items — it is LiveKit Cloud's own session flag, not a file-producing one.

A console call's audio passes through the local machine and can be written to
disk. A phone call's audio flows between LiveKit and the carrier and never
touches it, so recording requires an egress, and the egress writes to whatever
storage the request names. With none named it writes to LiveKit's own egress
server, which this process cannot read.

So the egress id and reported location are captured instead. The brief permits
"call recording **or** audio reference".

**Upgrade path:** point `file_outputs` at an S3/GCP/Azure bucket and the same
code yields a fetchable file. See [R-08](RISKS.md#r-08).

---

### I11 — Accept an ~888ms import stall rather than couple the agent to Opik
**Status:** Accepted, with known cost
**Evidence:** framework warning on a real call — the lazy `import opik` inside
`build_sink()` pulls in `sentry_sdk` and blocks the loop for 888ms at session
start.

The lazy import is precisely what makes D5's "delete the file and it still runs"
true. Hoisting it to module scope would remove the stall and break the claim.

**Cost:** a stall at the moment the greeting should go out — the one issue in
this build a patient could plausibly notice. **Open.** The fix is a prewarm hook
that imports at worker startup without coupling the agent to Opik. See
[R-09](RISKS.md#r-09).

---

### I12 — Dialling is opt-in; tests never dial and never write to production telemetry
**Status:** Accepted
**Evidence:** two incidents — test runs exported junk traces to a live Opik
project twice, by two different mechanisms.

`dispatch.py` requires `--call`. A tool that rings a real phone and bills by the
rounded minute should not fire because someone pressed up-arrow and enter.

Test suites force `OPIK_ENABLED=false` and `OPIK_TRACK_DISABLE=true` rather than
defaulting them, because `.env` is loaded by the code under test. Verified by
snapshotting the project before and after a full suite run. See
[R-11](RISKS.md#r-11).
