# Agent OS Events (Phase 0)

Status: **accepted** — Phase 0. Defines the durable event envelope and the
initial event taxonomy. Transport (HTTP API, outbox, queue) is Phase 1+
(proposal §10.4, §17); this document freezes the *shape* and the naming
decision, not the delivery mechanism.

Source model: `contracts/events.py` (`EventEnvelope`, `EventType`).
Committed schema: `contracts/schemas/EventEnvelope.json`. Spec: [`PROPOSAL.md`](PROPOSAL.md)
§10.2 (envelope) and §10.3 (taxonomy).

## Envelope

The envelope mirrors proposal §10.2 exactly:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `event_id` | `str` | yes | prefixed ULID, `evt_...` |
| `event_type` | `str` (open dotted `domain.verb`) | yes | pattern-constrained open string; producers use the `EventType` registry (§10.3) — see "Open event-type registry" below |
| `occurred_at` | timezone-aware UTC datetime | yes | ISO-8601, emitted with `Z` |
| `machine_id` | `str` | yes | originating machine (`mach_...`) |
| `agent_id` | `str?` | no | nullable context field |
| `session_id` | `str?` | no | nullable context field |
| `project_id` | `str?` | no | nullable context field |
| `repository_id` | `str?` | no | nullable context field |
| `task_id` | `str?` | no | nullable context field |
| `payload` | `object` | no (default `{}`) | event-type-specific body |
| `schema_version` | `int` | no (default `1`) | contract version |

The context fields are nullable because not every event is scoped to an agent,
session, project, repository, or task. Every event *does* originate on a machine,
so `machine_id` is required.

## Taxonomy (§10.3)

`task.created`, `task.updated`, `task.claimed`, `task.blocked`, `task.completed`,
`task.reconciled`; `agent.started`, `agent.status_changed`, `agent.waiting`,
`agent.failed`, `agent.finished`; `repo.file_changed`, `repo.graph_updated`,
`repo.commit_created`, `repo.tests_completed`; `db.graph_updated`;
`memory.observation_created`, `memory.project_updated`,
`memory.promotion_requested`, `memory.global_updated`; `sync.started`,
`sync.completed`, `sync.failed`.

`task.reconciled` is the optimistic-concurrency reconciliation event promised by
§14.3 (conflicting task edits produce a reconciliation event rather than a silent
overwrite); it was added to the registry during the Phase 0 contract review, as
§10.3's literal list omitted it.

The `EventType` registry is additive within a major schema version — new members
may be appended; existing values never change (see [`CONTRACTS.md`](CONTRACTS.md)
Decision 5).

## Open event-type registry

`EventEnvelope.event_type` is an **open, pattern-constrained string**
(`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`), *not* a closed `EventType` enum field.
The `EventType` enum is retained as the **producer-side registry**: the
documented, checked vocabulary producers should emit.

Why open rather than a closed enum field: the envelope is the durable format that
flows through an outbox and is drained by consumers that may be running an older
contract build. If `event_type` were a closed enum, a consumer that had not yet
learned a newer event type could not even *parse* the envelope to route or
dead-letter it — one unknown type would stall the outbox cursor for everything
behind it. An open string lets any consumer parse, group by domain prefix, and
route/dead-letter an unrecognised-but-well-formed type, while the pattern keeps
values well-formed and the registry keeps producers honest (a producer that wants
to prove it only emits declared fields uses `contracts.strict_validate`). This is
the same tolerant-reader posture documented in [`CONTRACTS.md`](CONTRACTS.md)
Decision 1, applied to the type field.

## ADR — why domain field names, not CloudEvents

[CloudEvents](https://cloudevents.io/) is the obvious industry envelope standard,
and we deliberately considered it. We keep our **own domain field names** for
Phase 0 for these reasons:

- **All producers and consumers are ours.** The envelope only ever crosses
  between the Agent OS plugin, the Agent OS API, and the control plane — all
  first-party. The interop benefit CloudEvents exists to provide (heterogeneous
  producers/consumers across vendors) does not apply yet.
- **Domain-specific context is first-class.** `machine_id`, `agent_id`,
  `session_id`, `project_id`, `repository_id`, and `task_id` are load-bearing
  routing/scoping fields. In CloudEvents most of these would be non-standard
  *extension attributes* anyway, so adopting the spec would not remove them —
  it would only rename `id`/`type`/`time`/`source`/`data` and add `specversion`.
- **Readability and lower ceremony.** A flat, explicitly-named envelope is
  easier to read in logs and to validate strictly (`extra="forbid"`) than a
  CloudEvents document with a bag of extension attributes.

This is a reversible decision. If an external, non-first-party consumer ever
needs CloudEvents, we add a **bridge** that maps our envelope onto CloudEvents
attributes rather than changing the internal contract. The mapping below is
recorded now so that bridge is mechanical.

### Mapping table (future CloudEvents bridge)

| Agent OS field | CloudEvents attribute | Notes |
|----------------|-----------------------|-------|
| `event_id` | `id` | unique per (source, id) |
| `event_type` | `type` | e.g. `task.completed` — already dotted/reverse-DNS-friendly |
| `occurred_at` | `time` | RFC 3339 / ISO-8601 |
| `machine_id` | `source` | context that emitted the event (a URI-reference in CE; wrap as `agent-os://machine/<machine_id>`) |
| `task_id` | `subject` | the event's subject within the source, when present |
| `payload` | `data` | event body |
| `schema_version` | `dataschema` | or a CE extension; identifies the contract version |
| `agent_id` | extension `agentid` | CE extension attribute |
| `session_id` | extension `sessionid` | CE extension attribute |
| `project_id` | extension `projectid` | CE extension attribute |
| `repository_id` | extension `repositoryid` | CE extension attribute |
| `specversion` | — | supplied by the bridge (e.g. `1.0`) |
| `datacontenttype` | — | supplied by the bridge (e.g. `application/json`) |

No CloudEvents code exists in Phase 0; this table is documentation for a later
gateway.
