# Phase 0 Recon — Part 1: mid-session context mutation & agent handoff

**Status:** answers PLAN.md Phase 0 item 1 only.
**Method:** LiveKit Agents docs + framework source, read 2026-09-19.

**Still outstanding (do not assume these are answered):**
- LiveKit session construction, worker setup, lifecycle hooks
- LiveKit dispatch + `CreateSIPParticipant` wiring
- Opik SDK tracing API
- Opik online evaluation mechanism
- Structured output mechanism
- LiveKit Inference model IDs
- Whether `end_call` must be a tool or is a framework capability

**Caveats that survive into the build:**
1. Framework source was read from `main`, which may differ from the pinned
   release. Re-confirm against the INSTALLED package at the start of Phase 2.
2. "Handed-off / injected context is effective on the very next generation" is
   INFERRED, not documented. Prove it empirically — Phase 2a exit test #2.
3. Whether `userdata` is serialised into the prompt COULD NOT BE VERIFIED.
   D12 sidesteps this: never put health data in `userdata`.

---

## 1. ANSWER

**Yes.** A function tool can add content to the agent's chat context mid-call,
and it persists for the remainder of the session. There is an official LiveKit
healthcare example doing exactly this inside a `@function_tool`.

**However, the project chose agent handoff instead — see PLAN.md D10.** This
section is retained because it documents the alternative and the constraints
that apply to both.

---

## 2. IN-PLACE INJECTION (documented, not chosen)

### The pattern

VERIFIED FROM DOCS — https://docs.livekit.io/agents/logic/chat-context/

    chat_ctx = self.chat_ctx.copy()
    chat_ctx.add_message(role="system", content="...")
    await self.update_chat_ctx(chat_ctx)

The `.copy()` step is mandatory, not stylistic. `Agent.chat_ctx` does not
return the live object.

VERIFIED FROM SOURCE — livekit-agents/livekit/agents/voice/agent.py

    @property
    def chat_ctx(self) -> llm.ChatContext:
        """Provides a read-only view of the agent's current chat context."""
        return _ReadOnlyChatContext(self._chat_ctx.items)

`llm/chat_context.py` carries the guard message:
"please use .copy() and agent.update_chat_ctx() to modify the chat context".
In-place mutation therefore fails loudly rather than silently no-opping —
relevant to the threat model.

VERIFIED FROM SOURCE — signature:

    async def update_chat_ctx(
        self, chat_ctx: llm.ChatContext, *,
        exclude_invalid_function_calls: bool = True
    ) -> None

### Reachability from inside a tool

The docs alone do NOT answer this. The tools definition page and the external
data / RAG page show context injection only via the `on_user_turn_completed`
node, never from a tool.

VERIFIED FROM DOCS — `RunContext` exposes `session`, `function_call`,
`speech_handle`, `userdata`. **It does NOT expose chat context.**

VERIFIED FROM SOURCE — official example `examples/healthcare/agent.py`,
approx. lines 359–393, calls `self.chat_ctx.copy()` / `add_message()` /
`await self.update_chat_ctx(...)` inside a `@function_tool()`, followed by
`await self.session.generate_reply(...)`.

**Consequence for this project:** tools reach agent state through `self`, not
through `RunContext`. A module-level `@function_tool` taking only `RunContext`
has no documented path to the chat context. Verification and booking tools must
be bound to the agent class. (PLAN.md D11.)

That example also contains a `profile_authenticator` and a gated-profile
pattern. Worth reading in full.

### Timing

COULD NOT FIND — no doc states when the update takes effect.

INFERRED: the example calls `update_chat_ctx` then immediately
`session.generate_reply(...)`, which only makes sense if the update applies to
that generation. Treat as near-certain but unstated. Prove empirically.

### Persistence

VERIFIED FROM DOCS — the chat-context page contrasts the two: messages added in
`on_user_turn_completed` "apply to the current turn only. Call
`update_chat_ctx` to persist them." `update_chat_ctx` replaces the agent's
internal `_chat_ctx`, so it survives subsequent turns.

### Caveats

- `exclude_invalid_function_calls=True` by default — drops function calls and
  outputs not belonging to the agent's tools. Relevant around a handoff.
- Realtime mode also updates the live provider session and raises
  `llm.RealtimeError` on failure (VERIFIED FROM SOURCE docstring). A swallowed
  exception here yields an agent that believes it has data it does not.
- `truncate()` strips leading function-call items to avoid orphaned tool
  results, and preserves system instructions.
- `copy()` accepts `exclude_instructions` / `exclude_function_call` filters.
- COULD NOT FIND any documented guidance on token limits, races, or ordering
  when `update_chat_ctx` is called concurrently with an in-flight generation.

---

## 3. AGENT HANDOFF — THE CHOSEN MECHANISM (PLAN.md D10)

VERIFIED FROM DOCS — https://docs.livekit.io/agents/logic/agents-handoffs/

- Triggered by **returning a different agent instance from inside a tool call.**
- **Conversation history does NOT carry over by default.** "Each new agent or
  task starts with a fresh conversation history" unless `chat_ctx` is passed to
  the constructor explicitly.
- `Agent.__init__` accepts `chat_ctx`
  (VERIFIED FROM SOURCE: `chat_ctx: NotGivenOr[llm.ChatContext | None] = NOT_GIVEN`).
  The docs note `BillingAgent(chat_ctx=self.chat_ctx)` works even when the
  subclass `__init__` does not declare it.
- `userdata` persists automatically across handoffs via the session.
- **The audio session continues uninterrupted** — the caller stays connected.
- An `AgentHandoff` item is appended to the context recording both agent IDs.
  (Useful: this is an auditable gate event for the Opik trace.)

---

## 4. RISKS

**`role="system"` mid-conversation is the weak point of in-place injection.**
Both the docs and the official example inject with `role="system"`.
Mid-conversation system messages are handled inconsistently across providers —
some weight a late system message differently, some reorder or coalesce them.
This does not threaten the privacy property but may threaten correctness: the
model may underuse data injected this way. **Handoff avoids this entirely**, by
placing post-verification content in the new agent's instructions.

**Realtime models are the danger zone.** Two closed issues in `livekit/agents`
concern `update_chat_ctx` misbehaving under Realtime models specifically:
#3386 (context not updating, Gemini Flash 2.5, closed as `question`, Sept 2025)
and #4497 (labeled `bug` — context injection strips system messages in Gemini
RealtimeModel, closed Jan 2026). Status checked only; threads not read. Both
closed and months old, presumably fixed. Pattern: this mechanism is better
tested on the STT→LLM→TTS pipeline than on Realtime. Consistent with PLAN.md D1.

**The structural guarantee is only as good as every other path into the prompt.**
Health data must also stay out of `instructions`, out of tool descriptions and
enum values (the official example builds tools dynamically with
`json_schema_extra={"enum": available_doctors}` — real data in a schema, which
is sent to the model), and out of any tool return value reachable before
verification. See PLAN.md D12.

**Fail closed.** If handoff or construction raises and the tool swallows it, the
agent believes verification succeeded, holds no data, and improvises.
