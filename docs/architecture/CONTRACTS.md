# Agent OS Contracts (Phase 0)

Status: **accepted** — Phase 0 (architecture contracts). Scope: the stable,
typed interfaces every later phase depends on. No transport, server, database,
or hook wiring is introduced here (that is Phase 1+). This document is the ADR
record for the decisions frozen in the `contracts/` package.

Canonical spec: [`PROPOSAL.md`](PROPOSAL.md) — see §4.3-4.4, §5.4, §5.6,
§10.2-10.3, §11, §12.1, §13, §14, §16 Phase 0, §22. Event-envelope decisions
are recorded separately in [`EVENTS.md`](EVENTS.md).

## Where the contracts live

```
contracts/
  __init__.py           # re-exports + ALL_MODELS registry (introspected)
  base.py               # AgentOSModel (tolerant reader) + strict_validate + Dotted + utc_now()
  identifiers.py        # prefixed-ULID generator + prefix registry
  events.py             # EventEnvelope + EventType   (see EVENTS.md)
  registry.py           # AgentRegistration + AgentStatus
  capabilities.py       # Capability enum + CapabilityGrant
  tasks.py              # TaskRecord + TaskSyncStatus  (sync projection)
  memory.py             # MemoryNode / MemoryEdge (+ vocabularies)
  artifacts.py          # §11.1 agent-communication artifacts + Claim
  context.py            # ContextPackage
  identity.py           # MachineIdentity / RepositoryIdentity + helpers
  examples.py           # one valid instance of every model (python -m contracts.examples)
  generate_schemas.py   # (re)generate / --check the committed JSON Schemas
  schemas/*.json        # committed generated JSON Schemas (one per model)
```

The package imports only the standard library and **pydantic v2**. It must never
import from `mcp/`: contracts are the dependency of everything and dependent on
nothing (proposal §15 — clear package boundaries first).

## Decision 1 — Pydantic v2 models are the source of truth

The models are authoritative; the JSON Schemas under `contracts/schemas/*.json`
are **generated artifacts** committed alongside them so non-Python consumers can
validate payloads and so schema changes are visible in review.

- Generate / rewrite: `python -m contracts.generate_schemas`
- Verify (CI drift gate): `python -m contracts.generate_schemas --check`

A drift test regenerates in memory and asserts byte-for-byte equality with the
committed files. Output is deterministic (`json.dumps(schema, indent=2,
sort_keys=True)` + trailing newline). Editing a model without regenerating fails
the drift test.

Every model derives from `AgentOSModel`, which fixes two invariants:

- `extra="ignore"` — the **tolerant-reader** posture (see below).
- `schema_version: int = 1` — see Decision 5.

Contract models are **not frozen** (mutable) in Phase 0. This keeps example and
test construction simple; immutability can be revisited if a concrete need
appears. Field-level strictness comes from typed / enum / bounded fields; the
unknown-key posture is split between reader and producer as follows.

### Tolerant reader, strict producer

Contracts are cross-machine wire formats that evolve **additively within a major
version** (Decision 5). If every model rejected unknown keys (`extra="forbid"`),
adding a single optional field would break every consumer still running an older
contract build — an additive change would behave like a breaking one and force
lockstep fleet upgrades. So the posture is:

- **Consumers tolerate.** Wire models parse with `extra="ignore"`: an unknown key
  (e.g. a field a newer producer added) is dropped, not rejected, so an older
  consumer can still read a newer payload.
- **Producers must not emit unknowns for their declared version.** Strictness
  moves to an explicit authoring utility, `contracts.strict_validate(model_cls,
  data)`, which rejects any unknown top-level key. Tests use it to prove the
  models reject stray fields, and a producer (or a future producer-side gateway)
  uses it to prove it is only emitting fields declared by the contract version it
  claims.

Consequently the committed JSON Schemas do **not** set `additionalProperties:
false` — that reflects the tolerant on-the-wire posture. (`strict_validate`
enforces closure out-of-band via an internal `extra="forbid"` mirror; it does not
change the published schema.)

## Decision 2 — Identifiers are prefixed ULIDs

Durable entities are named `<prefix>_<ulid>`, where the ULID is a 26-character
Crockford base32 string (`contracts/identifiers.py`). A ULID's leading 48 bits
are the creation time in milliseconds, so ids sort lexicographically in creation
order; the trailing 80 bits are randomness for cross-machine collision safety.
The generator is vendored (~40 lines, no dependency) and is **not**
process-monotonic — two ids in the same millisecond are ordered only by their
random tail, which is sufficient for Phase 0.

`is_valid_ulid` enforces the canonical 128-bit bound: a ULID encodes 128 bits in
26 base32 chars, so the leading character carries only 3 bits and must be `0`-`7`
(the largest valid ULID is `7ZZZ…`). Strings that encode more than 128 bits are
rejected, matching what cross-language ULID libraries accept.

### Prefix registry

| Prefix   | Entity                          |
|----------|---------------------------------|
| `evt_`   | event (`EventEnvelope`)         |
| `task_`  | task (`TaskRecord.global_id`)   |
| `mach_`  | machine (`MachineIdentity`)     |
| `repo_`  | repository (`RepositoryIdentity`)|
| `proj_`  | project                         |
| `agent_` | agent session                   |
| `sess_`  | session                         |
| `mem_`   | memory node / edge              |
| `prop_`  | memory proposal (`MemoryProposal`)|
| `grant_` | capability grant (`CapabilityGrant.grant_id`)|

The prefix **constants** in `identifiers.py` are the single source of truth;
`KNOWN_PREFIXES` is *derived* from them, and `new_id(prefix)` only mints ids for a
registered prefix. Adding an entity type means adding one prefix constant.

### Two id conventions (registered vs. free-form)

- **Registered prefixed ULIDs** name durable registry/graph objects (the table
  above) — including the capability `grant_id`, which is a durable grant record
  (`grant_`).
- **Free-form scoped ids** name transient inter-agent artifacts (`artifact_id`).
  The proposal's own §11.2 example uses `finding_...`, which is *not* a registered
  ULID prefix; these are messages, not registered durable objects, so they carry a
  conventional `<type>_<id>` string that Phase 0 does not constrain further.

Id fields are typed `str` in the models (not per-prefix-validated) — the
validation surface for "bad payload" rejection is required-fields, enums,
numeric bounds, and `extra="forbid"`, not id regexes. `identifiers.py` still
offers `is_valid_ulid`, `split_id`, and `is_valid_id` for callers that want to
check.

## Decision 3 — Identity files

Two idempotently-created JSON files anchor identity (proposal §5.4, §22.3):

- **machine-global** `~/.agent-os/identity.json` — `machine_id` (`mach_`),
  `name`, `os`, `created_at`, `schema_version`.
- **repository-local** `<repo>/.agent-os/identity.json` — `repository_id`
  (`repo_`), nullable `project_id` (`proj_`), nullable `canonical_remote`,
  nullable `machine_id`, `created_at`, `schema_version`.

`canonical_remote` is the normalized origin-remote fingerprint
(`canonical_remote_url`): the remote lowercased, credentials and any `.git`
suffix stripped, scp-style `git@host:owner/repo` rewritten to `host/owner/repo`.
Two checkouts of the *same* logical repo on different machines share this value,
so a later sync layer recognises them as one repo instead of partitioning tasks
per checkout; `machine_id` records which machine minted the checkout's identity.
Both are optional — Phase 0 defines the shape, and a caller supplies them when
known (they are stamped only on first creation).

The machine home honors the **`AGENT_OS_HOME`** environment override (default
`~/.agent-os`), mirroring the `AGENT_OS_MEMORY_HOME` precedent so tests never
touch a real home. The repo file lives under `.agent-os/`; `ensure_repository_identity`
**writes the self-protecting `.agent-os/.gitignore` (`*`) sentinel itself** when
creating the directory (mirroring the card-DB init in `mcp/server.py`), so a
fresh clone can never commit `identity.json` and later have a `git checkout` /
`reset` revert `repository_id` to a stale value (the 0.2.7 git-driven-loss class).

Creation (`ensure_machine_identity`, `ensure_repository_identity`) is:

- **idempotent** — a second call returns the same ids;
- **content-atomic** — the candidate is written to a uniquely-named temp file in
  the same directory and `fsync`ed, then the final path is *claimed* with an
  exclusive `os.link` (a lock-file + `os.replace` fallback covers filesystems
  without hard links). The final path only ever appears as a fully-written file,
  so a reader never observes a partial write;
- **safe under concurrent first-run** — exactly one racing process wins the
  claim; losers read the winner's completed file rather than minting a second id
  (verified by a 12-thread stress test);
- **self-healing** — if a winner is killed between claim and completion, the
  leftover final file is invalid. A *fresh* invalid file is treated as a live
  winner mid-write and, after a short seconds-scale claim budget with backoff,
  surfaces as an error; an invalid file that is also *stale* (mtime older than a
  few seconds) is treated as a dead winner's leftover — reclaimed (bounded) and
  re-claimed — so a killed winner can never permanently brick machine identity;
- **pure and local** — no network.

## Decision 4 — Capabilities are explicit and default-deny (policy)

`Capability` is the §12.1 **producer-side registry** (the documented capability
vocabulary the plugin ships with); `CapabilityGrant` records that a principal
(an `agent` or a `machine`) holds a capability, optionally `scope`d and
`expires_at`-bounded. The **default-deny** rule — a principal has only the
capabilities explicitly granted — is documented policy in Phase 0; it is *not
enforced* yet (no server/API exists). Enforcement arrives with Phase 1.

`CapabilityGrant.capability` and `AgentRegistration.capabilities` are **open,
pattern-constrained `domain.verb` strings**, not closed `Capability` enum fields
(§12.1 is explicitly an *example* set, and later machines add capabilities such
as `telegram.send` / `schedule.create`). They accept a registry value today and
an unknown capability tomorrow without a contract change. This is safe precisely
because enforcement is default-deny — an unrecognised capability grants nothing
until policy recognises it — whereas a closed enum field would instead break
parsing of any registration or grant that names a capability the local build has
not heard of (the same tolerant-reader rationale as `event_type`; see
[`EVENTS.md`](EVENTS.md) "Open event-type registry"). A producer that wants to
prove it only emits registry-declared capabilities validates them against
`Capability` on the producing side.

## Decision 5 — Versioning rules

- Every model carries `schema_version: int = 1`.
- Within a major version, changes are **additive only** (add optional fields /
  new enum members; never remove or repurpose a field, never change a field's
  type or an enum member's value). A breaking change bumps the major version.
- External clients reach the system through a **versioned API** with a `/v1`
  path prefix (proposal §13); the central DB is never queried directly by
  external clients.

## Model inventory

| Model | File | Proposal ref |
|-------|------|--------------|
| `EventEnvelope`, `EventType` | `events.py` | §10.2 / §10.3 |
| `AgentRegistration`, `AgentStatus` | `registry.py` | §11.3 / §5.2 |
| `Capability`, `CapabilityGrant` | `capabilities.py` | §12.1 |
| `TaskRecord`, `TaskSyncStatus` | `tasks.py` | §14 |
| `MemoryNode`, `MemoryEdge`, `MemoryApprovalState`, `MemoryEdgeType` | `memory.py` | §4.3 / §4.5 / §9.4 |
| `Claim`, `Task`, `Plan`, `Finding`, `Question`, `Blocker`, `DecisionRequest`, `Approval`, `PatchReference`, `TestReport`, `ExecutionReport`, `MemoryObservation`, `MemoryProposal`, `StatusReport` | `artifacts.py` | §11.1 / §11.2 |
| `ContextPackage` | `context.py` | §5.6 |
| `MachineIdentity`, `RepositoryIdentity` | `identity.py` | §5.4 / §22.3 |

`contracts.ALL_MODELS` is the introspected registry of all concrete models;
schema generation and example coverage both derive their required set from it,
so a newly-added model cannot silently skip either.

### Content fields added during the Phase 0 contract review

The contract review widened three models so later phases do not begin with a
forced contract break (all additions are optional / additive):

- `TaskRecord` (§14.1) — `title`, `description`, `priority`, `status_reason`,
  `blocked_reason`, so a central task can be projected back into a local card.
- `RepositoryIdentity` — `canonical_remote` and `machine_id` (see Decision 3).
- `MemoryNode` — `project_id`, `repository_id`, and `evidence` (free-form
  provenance reference strings, same convention as `Claim.evidence` /
  `MemoryProposal.evidence`). Provenance captured at write time is unrecoverable
  if omitted, so the reference list exists from day one; the remaining §4.4
  provenance facets are layered on additively in later phases.
