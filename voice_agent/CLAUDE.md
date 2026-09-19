# Project context

Outbound healthcare voice agent: LiveKit + Twilio SIP + Opik.
Portfolio project. Built in 3 days.

## Read these before doing anything

- `PLAN.md` — the build contract. Phases, decisions, exit tests.
  Section 0 contains hard rules that override default behaviour.
- `docs/recon.md` — verified LiveKit findings. Partial; see its header
  for what is still outstanding.
- `docs/recon-opik.md` — verified Opik findings (once it exists).

## Non-negotiables

- Never write a LiveKit or Opik API call from memory. Read the docs.
  Say "could not verify" rather than guessing a name.
- One phase per session. Stop at the exit test and wait for the human.
- No retry loops on outbound calls. One dial attempt per invocation.
- Health data enters only via `VerifiedAgent.__init__`. Never in
  instructions, `userdata`, tool names, descriptions, or enum values.
