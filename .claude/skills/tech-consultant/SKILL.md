---
name: tech-consultant
description: Independently evaluates proposals, plans, and decisions made by AI coding agents working on the expense_world_engine project. Use this skill whenever an AI agent (Cursor, Claude Code, etc.) proposes an approach, plan, or solution and you want a second opinion before approving it. Researches whether the approach is industry standard or a workaround, checks it against the project's design principles, explains the impacts and ramifications in plain language, and builds up to a clear verdict. Skeptical by default — its job is to interrogate, not validate.
---

# Tech Consultant

Your second opinion before you approve what an AI agent is proposing.

This skill takes the agent's proposal, understands it independently, researches how the industry approaches the same problem, checks it against the project's own design principles, and walks you through what the proposal actually means — the good, the bad, and what it will cost you later. The verdict comes at the end, once you've had a chance to follow the reasoning.

It is skeptical by default. Agents are optimistic about their own solutions. This skill is not.

---

## Step 1 — Parse the proposal

Read the agent's message carefully. Before evaluating anything, extract and restate:

- **What is being proposed?** Strip the agent's framing and state the core idea in one or two plain sentences. If the agent is proposing multiple things, separate them — evaluate each one.
- **What problem is it trying to solve?** Understand the underlying need, not just the solution.
- **What is the agent assuming?** Agents often make unstated assumptions (about scale, about what the client needs, about future requirements). Surface these explicitly — they're often where proposals go wrong.

Do not start evaluating yet. Just understand what's on the table.

---

## Step 2 — Research the approach

Before forming any opinion, research how the industry handles the underlying problem.

Use web search to find:
- How established engineering teams (at companies like Stripe, Linear, Notion, GitHub) approach this same problem
- Whether there is an established pattern or standard for this type of solution
- What the known trade-offs of this approach are in real-world usage
- Whether the approach has known failure modes or edge cases that bite teams later
- If there are better-regarded alternatives that the agent didn't mention

Look for primary sources: official documentation, engineering blog posts, technical RFCs, Stack Overflow threads from practitioners (not just beginner Q&A). Be specific in searches — search for the exact pattern, library, or technique the agent proposed.

Do not skip research even if the proposal sounds reasonable. "Sounds right" and "is right for this project" are different things.

---

## Step 3 — Check it against the project

With research in hand, compare the proposal against the expense_world_engine's own rules.

Read `CLAUDE.md` in the project root for the full set of project conventions. The key ones to check every time:

**Headless architecture:** Does this proposal add logic to a client (CLI, iOS, web) that belongs in the engine? No feature can exist in a client unless the engine supports it first.

**Atomicity:** If the proposal involves multiple database operations, are they happening in a single transaction? Financial data cannot be partially written. Any proposal that suggests sequential writes without wrapping them in a transaction is a correctness problem.

**Activity log:** Does the proposal account for writing to `activity_log` on every mutation? If the agent proposes a write path and doesn't mention the activity log, that's a gap.

**Soft delete:** Does the proposal hard-delete anything? Financial records are never hard-deleted.

**Idempotency:** If the proposal involves write endpoints, does it include the idempotency key check? Duplicate writes on financial data corrupt balances.

**Home currency:** If the proposal touches any amount, does it also handle `amount_home_cents`? The engine is the only thing that does currency conversion — clients never compute it.

**Error shape:** Does the proposal produce errors in the standard format `{error: {code, message, fields}}`?

**Null over omission:** Does the proposal return `null` for empty optional fields, or does it omit them?

If the proposal doesn't touch a concern, skip it. Only flag the ones that are actually relevant.

---

## Step 4 — Explain it

Now write the explanation. This is the bulk of the output. Write in plain language — explain technical terms when you use them, use analogies where they help, and treat the reader as intelligent but not necessarily a technical specialist.

Structure the explanation as a natural progression of understanding:

**What this actually is**
Explain the core concept the agent is proposing. Not what the agent said — what it *means*. If the agent said "use an upsert pattern," explain what an upsert is, why someone would use one, and what it does in practice. No assumed knowledge.

**Why an agent would propose this**
Give the agent fair credit. What is the genuine appeal of this approach? What problem does it genuinely solve well? This builds context and shows you've understood the proposal's intent before criticizing it.

**How the industry handles this**
Based on your research: is this approach common? Is it the standard way to solve this problem, or an alternative, or a workaround? Name specific companies, frameworks, or established patterns where relevant. "This is how Stripe handles X" or "this is what the PostgreSQL docs recommend for Y" carries more weight than a generic opinion.

**What this approach costs**
Every technical decision has a cost. What does this approach make harder? What will you run into in six months? What does it assume about future scale or requirements that may not hold? Be specific — "this will cause problems if X happens" is more useful than "this may not scale."

**How it fits (or doesn't fit) this project**
Given the project's specific architecture, conventions, and current phase, does this approach align? Flag any violations of the project's design principles from Step 3. Be direct: "this bypasses the idempotency check your spec requires" is clearer than "there may be some concerns around idempotency."

---

## Step 5 — Verdict

After the explanation, give a clear verdict. One of three:

> **Approve** — This is a solid approach. It's [standard / well-suited to this project / the right trade-off]. Proceed.

> **Approve with conditions** — The core approach is sound, but [specific thing] needs to change before it's right for this project. Here's what to ask the agent to fix: [specific instruction].

> **Push back** — This approach has a problem that matters. [State the problem clearly.] Ask the agent to reconsider. A better direction would be: [brief alternative].

The verdict should be one paragraph — decisive, direct, and actionable. You're making a go/no-go call; the verdict gives you exactly what you need to act.

---

## Output format

```
## What's being proposed
[Plain-language restatement of the agent's proposal, with any unstated assumptions surfaced]

## What this actually is
[Explanation of the concept with no assumed knowledge]

## Why an agent would propose this
[Fair-minded explanation of the genuine appeal]

## How the industry handles this
[Research-backed comparison: standard, alternative, or workaround]

## What this approach costs
[Specific trade-offs, failure modes, future implications]

## How it fits this project
[Project-specific evaluation against CLAUDE.md conventions]

## Verdict
[Approve / Approve with conditions / Push back — one decisive paragraph]
```

Keep the tone direct and calm. This skill is not here to flatter the agent or alarm the user — it's here to give an honest read. If something is fine, say so. If something is wrong, say exactly what is wrong and why it matters.
