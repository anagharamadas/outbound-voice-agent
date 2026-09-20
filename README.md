# Outbound Healthcare Voice Agent — Portfolio Project

An AI agent that telephones a patient, **confirms who it is speaking to before
saying anything clinical**, reads out their recent test results, and books a
follow-up appointment through a tool call. After the call it produces a
structured outcome record and ships the whole thing to
[Opik](https://www.comet.com/docs/opik) for observability, where an automated
rule scores every trace for premature disclosure of health data.

Built on LiveKit Agents with a Twilio SIP trunk. It has been run end to end
against a real phone call, and works locally in a console with the same code.

---

## 👉 The implementation is in [`voice_agent/`](voice_agent/)

**Start there:** [`voice_agent/README.md`](voice_agent/README.md) — what it does,
setup, how to run it locally and over the phone, how the Opik integration works,
and what the evaluation measures.

Everything at this level is supporting evidence for the demo.

---

## What each file is

### The implementation

| | |
|---|---|
| [`voice_agent/`](voice_agent/) | **The working code.** Agent, dispatcher, tools, observability seam, evaluation metric, and five test suites (160 checks on a fresh clone). |

Four documents live inside it, each answering a different question:

| Document | Answers |
|---|---|
| [`voice_agent/README.md`](voice_agent/README.md) | How do I run it? |
| [`voice_agent/ARCHITECTURE.md`](voice_agent/ARCHITECTURE.md) | How does it fit together? C4 views, call sequence, the verification gate as a state machine, trust boundaries. |
| [`voice_agent/DECISIONS.md`](voice_agent/DECISIONS.md) | Why is it built this way? 13 design decisions and 13 implementation decisions, each with the evidence that drove it. |
| [`voice_agent/RISKS.md`](voice_agent/RISKS.md) | What could go wrong? A risk register rated for production, including four issues that occurred during the build. |

### The demo

| File | What it shows |
|---|---|
| [`demo_recording.mp4`](demo_recording.mp4) **(7:15)** | The main demo. A real outbound call placed from the terminal and answered on a handset, then a second call demonstrating the identity gate, with the post-call analysis and the Opik trace. |
| [`worker_terminal_screenshot.png`](worker_terminal_screenshot.png) | The agent worker's live view during **both** calls. The contrast is the whole argument: the first reaches `IDENTITY VERIFIED … handing off to VerifiedAgent`; the second shows `VERIFICATION FAILED` twice and **no handoff line at all**. |
| [`clip1_opik_trace.json`](clip1_opik_trace.json) | The Opik trace for call 1, exported. Identity verified, appointment booked. Four spans — the conversation and one per tool call, each with arguments and result. Scored `no_premature_disclosure = 1.0`. |
| [`clip2_opik_trace.json`](clip2_opik_trace.json) | The Opik trace for call 2. Identity **not** verified. Both `verify_patient_identity` spans failed, there is **no `book_appointment` span**, and **no biomarker appears anywhere** in the transcript. |

> The trace exports are included because the Opik project is private — they let
> you read exactly what the platform received without needing an account.

### Two short addenda

| File | Why it exists |
|---|---|
| [`addendum_clip1_post_call_analysis.mov`](addendum_clip1_post_call_analysis.mov) **(0:21)** | Call 1's post-call analysis, rendering correctly. |
| [`addendum_clip2_post_call_analysis.mov`](addendum_clip2_post_call_analysis.mov) **(0:12)** | Call 2's post-call analysis, rendering correctly. |

**Why these are here.** At around 5:00 the main recording shows
`No analysis on this record` where the post-call analysis should be. That
message is wrong, and the cause is worth stating plainly rather than editing
away:

The agent writes the call record to disk **immediately**, so a complete record
survives whatever follows, and only then runs the analysis and rewrites the
file — an ordering chosen because losing the record costs the call, while losing
the analysis costs a summary. The small presenter script used to display the
record on screen waited a fixed 1.5 seconds before rendering. The analysis takes
2–6 seconds plus two flushes, so it displayed the *first* write, before the
analysis existed, and then guessed at a cause it had no evidence for.

**The agent was never at fault.** Both calls analysed correctly within seconds,
and the results are visible in the trace exports above, which were taken before
this was noticed. The defect was in a presentation tool. It is fixed, and the
addenda re-render the **same saved records** — no calls were re-placed.

---

## The shortest path through this

1. **Watch** [`demo_recording.mp4`](demo_recording.mp4) — a real call, start to finish.
2. **Look at** [`worker_terminal_screenshot.png`](worker_terminal_screenshot.png) — the gate holding, and failing to hold, side by side.
3. **Read** [`voice_agent/README.md`](voice_agent/README.md) — what it is and how to run it.
4. **If you have ten more minutes**, [`voice_agent/DECISIONS.md`](voice_agent/DECISIONS.md) is where the reasoning lives.

---

## The one thing worth knowing up front

The agent that answers the phone is **constructed without the patient's health
data**. It is not instructed to keep a secret — it does not have one. On a
successful identity check the verification tool returns a *different agent
object*, one built with the biomarkers and the booking tools, and the framework
swaps it in mid-call without dropping the audio.

So premature disclosure is not unlikely; it is architecturally unavailable. The
guarantee is checkable by reading a constructor rather than by trusting a
language model to follow an instruction while an unpredictable human talks to
it. Everything else in the design follows from that.
