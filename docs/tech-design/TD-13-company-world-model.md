# TD-13: Company World Model and Independent Employee Cognitive Loop

## Goal

Turn AgentPulse from a recent-transcript chat surface into a durable company
world model. Employees remain independent Hermes profiles, share company facts,
retrieve only the context needed for the current situation, communicate with
one another, and form evidence-backed memories over time.

## Runtime flow

```text
message/task/event
  -> immutable company_events
  -> Context Engine (focus -> retrieve -> rank -> budget)
  -> employee-facing prompt + context_manifest
  -> Hermes ACP Run
  -> message/output/action events
  -> memory ingestion
  -> reflection / relationship update / idea
```

Raw events are never replaced by summaries. Episodes and reflections point back
to their source event IDs. A context manifest records the exact evidence used
for a run so the desktop UI can answer “why did this employee know that?”.

## Retrieval contract

The default rank combines:

- lexical relevance (SQLite FTS/BM25 or PostgreSQL text search);
- recency decay;
- importance and task/goal relevance;
- source confidence and citation availability;
- deduplication across the same conversation, episode, and reflection.

The implementation must work in both SQLite tests and PostgreSQL production.
Optional embeddings may improve ranking later, but they are not required for
the durable contract or for local/offline startup.

## Employee semantics

The prompt describes a colleague by name, role, department, company objective,
experience, and evidence. It does not expose internal runtime identity. A
company-visible DM is represented as a normal colleague conversation and is
indexed into the same company event ledger.

Internal communication is durable and bounded. Repeated content, ping loops,
and conversations without new facts are collapsed or converted into a task;
scope changes still require a derived brief and owner confirmation.

## Obsidian boundary

The desktop client may read only `.agentpulse/managed/**/*.md` under a Vault
selected by the user. It sends the Markdown body, title, relative path, and
modified time to `POST /api/knowledge-sources/obsidian-sync`. The API stores a
workspace-scoped source hash and origin, updates the knowledge index
idempotently, and appends a knowledge event for each changed version. The
original Vault is never scanned wholesale or written back by AgentPulse.

## Acceptance

- A fact written by one employee in a previous group or DM can be retrieved by
  another employee in a later run.
- Private reflection text is not returned by company-wide search unless it has
  been promoted to an evidence-backed company fact.
- Context overflow retains raw history and evidence links while keeping each
  prompt within its budget.
- Employees can ping, support, and create internal tasks without a boss turn;
  external actions still enter the existing approval/action queue.
- A four-person content-team E2E demonstrates cross-employee recall,
  collaboration, reflection, and restart recovery.
