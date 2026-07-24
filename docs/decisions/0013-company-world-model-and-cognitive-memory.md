# ADR 0013: Company World Model and Independent Employee Memory

- Status: accepted
- Date: 2026-07-24
- Decision owners: AgentPulse project owner

## Context

AgentPulse currently stores chat transcripts, task outputs, and a small set of
per-agent experiences, but each reply receives a shallow recent-message window.
The product needs employees who act as independent colleagues serving the
company's objective, while still sharing the company's factual history.

The Generative Agents research demonstrates the useful cognitive primitives:
an append-only memory stream, retrieval weighted by relevance, recency, and
importance, and reflection that derives higher-level memories from evidence.
Its Smallville simulation and JSON storage are not a suitable production
runtime for AgentPulse. Hermes remains the only employee runtime and its ACP
boundary remains unchanged.

## Decision

AgentPulse will add a durable company world model before each Hermes/DeepSeek
run:

1. Every workspace event becomes an immutable company event. Chat messages,
   including employee-to-employee DMs, tasks, decisions, knowledge, outputs,
   human actions, and external results are company-visible facts.
2. Each employee has an independent memory projection containing observations,
   episodes, reflections, relationship facts, and lessons. Private reasoning
   and runtime traces are not shared as employee context.
3. A Context Engine retrieves and ranks company events plus the current
   employee's memories before every run, writes a context manifest, and keeps
   raw evidence available when context is compacted.
4. Employee-facing prompts contain names, roles, relationships, and facts only.
   They do not expose AgentPulse runtime metadata such as `agent_id`,
   `sender_type`, profile names, or the word AI as an identity label.
5. Employees may ping colleagues, create internal work, and advance company
   objectives without a boss message. Existing approval gates still control
   irreversible external actions and scope changes.

## Consequences

The database becomes the canonical factual history; Hermes profile memory and
skills remain employee-local runtime state and a cache of learned behavior.
Context becomes inspectable and reproducible through source-linked manifests.
The system cannot mathematically prevent a model from inferring that it is an
AI from behavior, but it can prevent the product from revealing runtime
identity metadata. A company-wide event ledger also means a DM is a
conversation shape, not a data-access boundary.

## Alternatives rejected

- Injecting the full transcript on every turn: unbounded context, poor recall,
  and no durable evidence trail.
- Copying the Generative Agents Smallville implementation: simulation-specific
  JSON state and synchronous loops do not satisfy AgentPulse durability,
  multi-workspace isolation, approvals, or Hermes ACP execution.
- Making a hidden supervisor agent the source of truth: this would reintroduce
  the boss-centric single-agent model the product is explicitly avoiding.
