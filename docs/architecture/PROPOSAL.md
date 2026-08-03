# Agent OS — Proposed Architecture and Buildout Plan

**Repository:** `Elouderb/Claude-Delegation-Plugin`  
**Status:** Proposed major expansion from repository-local Claude Code plugin into a distributed, persistent agent coordination and memory system  
**Working name:** Agent OS

---

## 1. Executive Summary

The current repository already provides a strong local foundation for agentic software development:

- repository-local task cards backed by SQLite;
- a Graphify-derived code graph;
- a Microsoft SQL Server schema graph;
- lifecycle hooks for graph synchronization;
- a collection of specialist Claude Code agents and workflow skills;
- a Flask-based local graph and task UI;
- an MCP server that exposes task, code-graph, and database-graph tools.

The proposed buildout extends this foundation into a **distributed Agent OS** with two complementary planes:

1. **Execution plane** — Claude Code instances working inside repositories, with direct access to project files, tools, task cards, code graphs, database graphs, and project-local memory.
2. **Control plane** — an always-on, user-facing agent and shared memory service that coordinates work across machines and projects without directly editing code.

The system should preserve the current plugin as the primary repository execution environment while introducing several distinct services and adapters:

1. A central Microsoft SQL Server database.
2. An expanded version of the current Agent OS Claude Code plugin.
3. A separate communication-oriented Claude Code plugin for the always-on “Machine 3” agent.
4. An MCP or plugin interface for ChatGPT and potentially Codex.
5. A general ingestion pipeline for files, exports, images, and data feeds.
6. A project/repository graph-memory subsystem that can exchange selected knowledge with the central memory system.

The long-term objective is not merely shared chat history. It is a persistent, structured operating layer that understands:

- projects;
- repositories;
- code;
- database schemas;
- tasks and decisions;
- user-provided documents and conversations;
- agent activity;
- reusable knowledge across projects;
- permissions and execution boundaries.

---

## 2. Current Repository Baseline

The existing repository should remain the foundation of the execution plane rather than being replaced.

### Existing major components

| Component | Current role | Proposed future role |
|---|---|---|
| `mcp/server.py` | Unified MCP server for task cards and graph tools | Repository-local Agent OS gateway; may later be split into internal services while preserving one MCP facade |
| `.agent-os/cards.sqlite` | Local task-card persistence | Local cache and offline task store, optionally synchronized with the central database |
| `graphify-out/` | Repository code graph | Source of structural code knowledge and one input into project memory |
| `.agent-os/db/` | Database schema graph artifacts | Source of project-specific data-model knowledge |
| `hooks/` and `scripts/` | Graph freshness and generated-file protection | Event capture, memory proposals, task updates, and local-to-central synchronization |
| `agents/` | Specialist implementation, review, testing, research, and database agents | Execution workforce governed by task, memory, and permission contracts |
| `skills/` | Reusable workflow instructions | Procedural memory and standardized Agent OS behaviors |
| Flask graph UI | Local visualization for graphs and cards | Local project dashboard, later supplemented by a cross-project control-plane UI |

### Design principle

The current plugin should continue to work in a **local-only mode** when no central server is available.

This means the expansion should be additive:

- local task cards still work;
- local code and database graphs still work;
- hooks still function;
- central memory and coordination are optional capabilities;
- synchronization should be resilient to network loss;
- projects should never become unusable because Machine 3 or the main database is offline.

---

## 3. High-Level Architecture

```text
                                 CONTROL PLANE

                    ┌────────────────────────────────┐
                    │ Machine 3 / Always-On VM       │
                    │                                │
User interfaces ───▶│ Communication Agent            │
Telegram / RC /     │ Planning, discussion, routing  │
ChatGPT / Desktop   │ Monitoring, ideas, summaries   │
                    │                                │
                    │ Agent Registry / Event Worker   │
                    │ Memory Consolidation Worker     │
                    └───────────────┬────────────────┘
                                    │
                                    ▼
                    ┌────────────────────────────────┐
                    │ Main Microsoft SQL Server DB   │
                    │ Global memory, chunks, vectors │
                    │ events, tasks, agents, sources │
                    └───────────────┬────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
              ▼                     ▼                     ▼

                           EXECUTION PLANE

       ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
       │ Machine 1      │  │ Machine 2      │  │ Machine N      │
       │ Claude Code    │  │ Claude Code    │  │ Claude Code    │
       │ Agent OS plugin│  │ Agent OS plugin│  │ Agent OS plugin│
       │ repo memory    │  │ repo memory    │  │ repo memory    │
       │ code/DB graphs │  │ code/DB graphs │  │ code/DB graphs │
       └────────────────┘  └────────────────┘  └────────────────┘
```

### Control plane responsibilities

- user-facing conversation;
- cross-project awareness;
- monitoring task and agent state;
- routing requests to execution environments;
- maintaining the global memory substrate;
- consolidating project-local memory candidates;
- publishing summaries, ideas, blockers, and recommendations;
- enforcing high-level policies and capability boundaries.

### Execution plane responsibilities

- reading and editing repository files;
- running shell commands and tests;
- using specialist subagents;
- updating task cards;
- maintaining code and database graphs;
- building and querying project-local graph memory;
- emitting structured events and memory proposals;
- executing approved development work.

---

## 4. Component 1 — Main Microsoft SQL Server Database

The central database is the durable shared substrate for the entire system.

It should not be treated as one giant graph table or one giant vector store. It should combine normalized relational structures, graph-like edge tables, vector-capable retrieval, and immutable event history.

### 4.1 Core domains

The central database should contain at least the following domains:

1. **Identity and registry**
   - users;
   - machines;
   - agents;
   - repositories;
   - projects;
   - integrations;
   - capabilities.

2. **Tasks and coordination**
   - tasks/cards;
   - comments;
   - dependencies;
   - assignments;
   - approvals;
   - status history;
   - execution attempts.

3. **Global memory**
   - memory nodes;
   - memory edges;
   - claims;
   - observations;
   - procedures;
   - decisions;
   - evidence links;
   - confidence and verification metadata.

4. **Source ingestion**
   - source files;
   - source versions;
   - extracted text;
   - chunks;
   - images and image metadata;
   - conversations;
   - feed items;
   - parser results;
   - extraction failures.

5. **Embeddings and retrieval**
   - chunk embeddings;
   - node embeddings;
   - retrieval metadata;
   - lexical indexes;
   - similarity-cache records.

6. **Events and observability**
   - agent events;
   - hook events;
   - synchronization events;
   - memory proposals;
   - audit records;
   - health checks;
   - error records.

### 4.2 Suggested relational schema groups

```text
registry.*
  users
  machines
  agents
  agent_sessions
  repositories
  projects
  integrations
  capabilities
  agent_capabilities

work.*
  tasks
  task_comments
  task_dependencies
  task_assignments
  task_status_history
  approvals
  execution_runs

memory.*
  nodes
  edges
  claims
  observations
  procedures
  decisions
  evidence
  node_sources
  aliases
  contradictions
  promotion_candidates
  consolidation_runs

content.*
  sources
  source_versions
  documents
  document_sections
  chunks
  images
  conversations
  conversation_messages
  feeds
  feed_items
  extraction_jobs

vector.*
  chunk_embeddings
  node_embeddings
  query_cache

ops.*
  events
  sync_cursors
  audit_log
  errors
  heartbeats
```

SQL Server schemas such as `registry`, `work`, `memory`, `content`, `vector`, and `ops` would help keep the system intelligible as it grows.

### 4.3 Memory-node model

A global memory node should be a synthesized entity or concept, not merely a text chunk.

Example:

```json
{
  "node_id": "mem_01J...",
  "node_type": "architectural_pattern",
  "canonical_name": "Dedicated memory curator",
  "summary": "Durable memory writes should be mediated by a curator rather than written directly by every execution agent.",
  "scope": "global",
  "confidence": 0.91,
  "stability": "medium",
  "approval_state": "accepted",
  "created_by": "agent:machine3-memory-worker",
  "created_at": "2026-08-03T20:00:00Z",
  "last_verified_at": "2026-08-03T20:00:00Z"
}
```

### 4.4 Provenance requirements

Every durable memory claim should retain:

- source;
- source version;
- originating project or repository;
- authoring agent;
- timestamp;
- evidence chunks or events;
- confidence;
- verification state;
- contradiction state;
- supersession relationships;
- access policy.

A memory graph without provenance will eventually become difficult to trust.

### 4.5 Graph implementation on SQL Server

The first version can use conventional relational tables:

```sql
CREATE TABLE memory.nodes (...);
CREATE TABLE memory.edges (
    edge_id UNIQUEIDENTIFIER PRIMARY KEY,
    source_node_id UNIQUEIDENTIFIER NOT NULL,
    target_node_id UNIQUEIDENTIFIER NOT NULL,
    edge_type NVARCHAR(100) NOT NULL,
    weight FLOAT NULL,
    confidence FLOAT NULL,
    metadata_json NVARCHAR(MAX) NULL
);
```

Possible later options:

- SQL Server graph tables (`AS NODE`, `AS EDGE`);
- a parallel Neo4j projection for advanced traversal;
- Graphify-compatible exports;
- an application-layer graph abstraction that can swap storage backends.

The application should not make the first release dependent on Neo4j unless a concrete traversal requirement cannot reasonably be handled in SQL Server.

### 4.6 Vector storage

Possible implementations:

1. store vectors in SQL Server if the deployed version supports the required vector operations;
2. store serialized vectors in SQL Server and use an application-side ANN index;
3. use a sidecar vector database while SQL Server remains the system of record;
4. use a hybrid lexical + vector retrieval service.

The vector system should index both:

- raw or smart-chunked source material;
- synthesized memory nodes.

This permits both evidence retrieval and concept retrieval.

---

## 5. Component 2 — Expanded Repository Agent OS Plugin

The current Claude Code plugin becomes the execution-plane client for each repository.

### 5.1 Preserve existing behavior

The following should remain functional:

- repository-local card creation and updates;
- code-graph querying;
- database-schema graph querying;
- graph synchronization hooks;
- specialist agents;
- skills;
- graph UI;
- local-only operation.

### 5.2 New plugin responsibilities

The expanded plugin should add:

- central Agent OS connection configuration;
- repository and machine registration;
- local project-memory graph;
- memory retrieval tools;
- memory proposal tools;
- event publishing;
- task synchronization;
- agent heartbeat and status reporting;
- capability declarations;
- offline queueing and retry;
- context-package assembly.

### 5.3 Proposed new MCP tool groups

#### Global-context tools

```text
search_global_memory
get_global_memory_node
get_related_global_memory
request_context_package
```

#### Project-memory tools

```text
search_project_memory
get_project_memory_node
propose_project_memory
confirm_project_memory
mark_project_memory_stale
link_memory_to_code
link_memory_to_task
```

#### Coordination tools

```text
register_agent_session
report_agent_status
publish_agent_event
claim_task
release_task
request_approval
submit_execution_report
```

#### Synchronization tools

```text
sync_tasks
sync_memory_candidates
get_sync_status
retry_failed_sync
```

### 5.4 Local storage additions

A repository may evolve toward:

```text
.agent-os/
  cards.sqlite
  config.toml
  identity.json
  cache/
  events/
  memory/
    project-memory.sqlite
    graph.json
    embeddings/
    promotion-queue.jsonl
  sync/
    cursor.json
    outbox.jsonl
    dead-letter.jsonl
  db/
    ...existing DB graph artifacts...

graphify-out/
  ...existing code graph artifacts...
```

The exact storage format can change, but local artifacts should remain inspectable and recoverable.

### 5.5 Local outbox pattern

Every network-bound write should first be recorded locally.

```text
Agent action
  → append event to local outbox
  → apply local transaction
  → attempt central synchronization
  → record central acknowledgement
  → advance sync cursor
```

This prevents central-server outages from corrupting local workflows.

### 5.6 Context compiler

The plugin should introduce a context compiler that assembles bounded task-specific context from:

- active task card;
- relevant files;
- Graphify neighborhoods;
- database-schema nodes;
- local memory;
- selected global memory;
- prior decisions;
- recent task events;
- applicable skills;
- agent capability limits.

The output should be a structured context package rather than an uncontrolled dump.

Example:

```json
{
  "task": {...},
  "repo": {...},
  "relevant_files": [...],
  "code_graph_nodes": [...],
  "db_graph_nodes": [...],
  "project_memories": [...],
  "global_memories": [...],
  "recent_events": [...],
  "constraints": [...],
  "citations": [...],
  "token_budget": 30000
}
```

---

## 6. Component 3 — Communication Plugin for Machine 3

Machine 3 should run a separate Claude Code plugin or agent package rather than the repository execution plugin.

Its role is fundamentally different.

### 6.1 Responsibilities

The Machine 3 agent should:

- converse with the user;
- inspect global and project state;
- monitor tasks and agents;
- create and prioritize plans;
- route work to execution agents;
- summarize progress;
- identify blockers;
- propose new tasks and ideas;
- search global memory;
- request reports from repository agents;
- manage approvals;
- participate in memory consolidation.

### 6.2 Explicitly prohibited by default

The Machine 3 agent should not directly:

- edit repository files;
- commit code;
- deploy applications;
- execute unrestricted shell commands on development machines;
- modify project memory without provenance;
- send external messages without the necessary capability and user policy.

It is a control-plane agent, not a privileged coding agent.

### 6.3 User interfaces

Potential interfaces include:

- Claude Code Remote Control;
- Telegram bot;
- a local or hosted web chat;
- desktop notifications;
- ChatGPT adapter;
- command-line client;
- future mobile interface.

All interfaces should feed the same Machine 3 session/event system rather than creating separate silos.

### 6.4 Machine 3 tools

```text
list_projects
list_repositories
list_agents
get_agent_status
list_tasks
get_task
create_task
reprioritize_task
assign_task
request_agent_report
send_agent_instruction
pause_agent
resume_agent
search_global_memory
search_project_memory
get_project_summary
request_approval
record_user_decision
```

### 6.5 Machine coordinator boundary

Machine 3 should usually communicate with a machine or repository coordinator rather than every transient subagent.

```text
Machine 3 agent
  → Machine 1 coordinator
      → repository main agent
          → implementation/review/testing subagents
```

This prevents the control plane from becoming entangled with short-lived child-agent conversations.

---

## 7. Component 4 — ChatGPT / Codex MCP Integration

A separate MCP server should expose a controlled subset of Agent OS to ChatGPT, the ChatGPT desktop app where supported, and potentially Codex.

### 7.1 Primary use cases

- ask about project status from ChatGPT;
- inspect active and blocked tasks;
- search main memory;
- retrieve project summaries;
- create or update task cards;
- send a planning request to Machine 3;
- request a coding-agent report;
- submit files or notes for ingestion;
- record decisions from a ChatGPT conversation;
- compare information across projects.

### 7.2 Permission model

The ChatGPT adapter should not automatically expose all Agent OS capabilities.

Recommended default capabilities:

```text
read_global_memory
read_project_summaries
read_tasks
create_task
comment_on_task
submit_memory_candidate
send_message_to_machine3
```

Higher-risk actions such as task cancellation, agent interruption, external communication, or deployment should require explicit approval.

### 7.3 MCP server architecture

```text
ChatGPT / Codex
      │
      ▼
Agent OS External MCP Gateway
      │
      ├── authentication
      ├── user/session mapping
      ├── capability filtering
      ├── rate limiting
      ├── audit logging
      └── request normalization
      │
      ▼
Agent OS API / Main DB / Machine 3
```

The external gateway should not connect directly to SQL tables. It should call a versioned Agent OS service API.

### 7.4 Conversation capture

Conversation storage should be opt-in or policy-controlled.

Possible actions:

- store entire conversation export;
- store selected messages;
- store only a generated summary;
- extract explicit decisions and tasks;
- create memory candidates with links back to the source conversation.

---

## 8. Component 5 — File, Conversation, Image, and Data-Feed Ingestion

The ingestion system is the bridge between unstructured user information and the global memory system.

It should work largely without direct agent intervention.

### 8.1 Supported source classes

Initial high-value formats:

#### Text and documents

- `.txt`
- `.md`
- `.json`
- `.yaml` / `.yml`
- `.xml`
- `.csv`
- `.html`
- `.pdf`
- `.docx`
- `.pptx`
- `.xlsx`
- source-code files
- log files

#### Images

- `.png`
- `.jpg` / `.jpeg`
- `.webp`
- screenshots;
- diagrams;
- scanned documents where extraction quality is sufficient.

#### Conversation exports

- Claude exports;
- ChatGPT exports;
- Telegram exports;
- Slack or Teams exports;
- email archives;
- plain JSON or HTML message histories.

#### Data feeds

- RSS / Atom;
- watched folders;
- HTTP endpoints;
- periodic database queries;
- APIs;
- Git repositories;
- email inbox rules;
- scheduled file drops.

### 8.2 Ingestion pipeline

```text
Source discovered
  → identify format and source type
  → hash and deduplicate
  → store immutable source version
  → extract text / metadata / structure
  → segment into semantic units
  → smart chunk
  → create embeddings
  → classify entities and concepts
  → generate graph candidates
  → link to existing nodes
  → expose for retrieval
  → optionally queue memory consolidation
```

### 8.3 Parser contract

Each parser should emit a common intermediate representation:

```json
{
  "source_id": "src_...",
  "source_type": "document",
  "format": "pdf",
  "title": "Architecture Notes",
  "created_at": null,
  "authors": [],
  "sections": [
    {
      "section_id": "sec_...",
      "heading": "Memory Architecture",
      "content": "...",
      "page": 4,
      "metadata": {}
    }
  ],
  "assets": [],
  "parser": "pdf-parser-v1",
  "warnings": []
}
```

This keeps format-specific behavior outside the memory and retrieval layers.

### 8.4 Dynamic graph construction

The ingestion system should distinguish:

- extracted entities;
- extracted relationships;
- inferred concepts;
- durable memories;
- unresolved candidates.

It should not automatically elevate every named entity or semantic relation into trusted global memory.

Suggested states:

```text
extracted
candidate
linked
verified
accepted
rejected
superseded
stale
```

### 8.5 Automatic operation

Most ingestion should be deterministic and service-driven:

- parsers extract;
- chunkers segment;
- embedding workers vectorize;
- entity/linking models produce candidates;
- confidence rules decide whether agent review is needed.

Agent intervention should be reserved for:

- ambiguous schema changes;
- contradictory claims;
- low-confidence entity resolution;
- unusual formats;
- high-value consolidation;
- user-requested interpretation.

### 8.6 Image handling

For images, the system should preserve:

- original file;
- dimensions and metadata;
- perceptual hash;
- OCR or extracted text when useful;
- generated caption or description;
- detected diagram structure where possible;
- links to the conversation, task, or document that supplied the image.

A diagram such as the Agent OS architecture sketch should become a source artifact connected to architecture concepts, not merely a blob with a caption.

---

## 9. Component 6 — Project / Repository Graph Memory

Each project or repository should have a persistent memory graph distinct from both the raw code graph and the global memory graph.

### 9.1 Purpose

The project graph memory should answer questions such as:

- what is this repository for?
- what architectural decisions have been made?
- which modules own which responsibilities?
- why was a particular implementation chosen?
- what constraints must future agents preserve?
- what failed previously?
- which tasks, files, code nodes, and database entities are related?
- what knowledge from global memory is relevant here?

### 9.2 Distinction from existing graphs

| Graph | Represents |
|---|---|
| Code graph | Structural facts about source code |
| Database graph | Structural facts about a connected database schema |
| Task graph | Work items and dependencies |
| Project memory graph | Meaning, decisions, history, constraints, concepts, procedures, and links across the other graphs |

The project memory graph should reference nodes in the code and database graphs rather than duplicate all structural data.

### 9.3 Example node types

```text
Project
Repository
Component
Concept
Decision
Constraint
Requirement
Procedure
Failure
Lesson
Assumption
Environment
ExternalSystem
PersonOrRole
Task
Milestone
MemorySummary
```

### 9.4 Example edge types

```text
IMPLEMENTS
DEPENDS_ON
OWNS
DECIDED_BY
SUPERSEDES
CONTRADICTS
SUPPORTED_BY
DERIVED_FROM
APPLIES_TO
BLOCKS
RESOLVES
RELATED_TO
MENTIONS
USES
CONNECTS_TO
PROMOTED_FROM
IMPORTED_FROM_GLOBAL
```

### 9.5 Memory curator model

A repository memory should outlive any one Claude session. Therefore, the strongest design is a **repository-scoped curator process or subagent**.

Possible implementations:

1. a lightweight deterministic service plus scheduled agent review;
2. a designated memory-curator subagent invoked through hooks;
3. a persistent repository coordinator that owns memory writes;
4. a hybrid where deterministic writes are automatic and semantic consolidation is agent-assisted.

The hybrid is recommended.

### 9.6 Observation-to-memory pipeline

```text
Repository event
  → raw observation
  → local evidence link
  → candidate memory
  → deduplication and contradiction check
  → acceptance into project memory
  → optional promotion candidate for global memory
```

### 9.7 Local-to-global promotion

Similarity alone should never automatically merge project memory into global memory.

Similarity should trigger evaluation.

Promotion criteria may include:

- repeated appearance across projects;
- explicit user importance;
- reuse outside the originating repository;
- high confidence and stable evidence;
- architectural or procedural generality;
- repeated successful application;
- manual approval.

A promotion candidate should contain:

```json
{
  "candidate_id": "prom_...",
  "source_project": "agent-os",
  "source_memory_node": "pmem_...",
  "candidate_type": "procedure",
  "claim": "Use an outbox before central synchronization.",
  "evidence": ["event:...", "decision:...", "task:..."],
  "confidence": 0.94,
  "suggested_scope": "global",
  "reason": "Repeatedly useful for resilient distributed agent state."
}
```

### 9.8 Global-to-local import

Global memory retrieval should be contextual and reversible.

A global memory should not automatically become a local truth simply because it is semantically similar.

Imported memories should record:

- originating global node;
- retrieval reason;
- importing task;
- relevance score;
- whether the project accepted, rejected, or adapted it;
- local overrides.

---

## 10. Hooks and Event System

Hooks are likely the most natural bridge between the current plugin and the new memory/coordination architecture.

### 10.1 Useful event sources

- session start;
- task creation;
- task status change;
- tool invocation;
- file edit completion;
- test execution;
- commit creation;
- graph refresh;
- database graph refresh;
- subagent spawn;
- subagent completion;
- session end;
- user decision;
- error or blocked state.

### 10.2 Event envelope

```json
{
  "event_id": "evt_...",
  "event_type": "task.completed",
  "occurred_at": "2026-08-03T20:00:00Z",
  "machine_id": "machine-1",
  "agent_id": "agent-...",
  "session_id": "session-...",
  "project_id": "project-...",
  "repository_id": "repo-...",
  "task_id": "task-...",
  "payload": {},
  "schema_version": 1
}
```

### 10.3 Initial event taxonomy

```text
task.created
task.updated
task.claimed
task.blocked
task.completed

agent.started
agent.status_changed
agent.waiting
agent.failed
agent.finished

repo.file_changed
repo.graph_updated
repo.commit_created
repo.tests_completed

db.graph_updated

memory.observation_created
memory.project_updated
memory.promotion_requested
memory.global_updated

sync.started
sync.completed
sync.failed
```

### 10.4 Event transport

MVP options:

- HTTPS API writes directly to the Agent OS service;
- SQL-backed queue table;
- local JSONL outbox with polling;
- lightweight message broker later.

A full broker such as RabbitMQ, NATS, or Kafka is not required for the first version. A durable database-backed event queue is likely sufficient until event volume proves otherwise.

---

## 11. Agent Communication Contract

Agents should exchange structured artifacts rather than relying on unrestricted transcript passing.

### 11.1 Core artifact types

```text
Task
Plan
Finding
Question
Blocker
DecisionRequest
Approval
ExecutionReport
PatchReference
TestReport
MemoryObservation
MemoryProposal
StatusReport
```

### 11.2 Example finding

```json
{
  "artifact_type": "finding",
  "artifact_id": "finding_...",
  "author_agent": "database-engineer",
  "task_id": "TASK-184",
  "claims": [
    {
      "statement": "CustomerOrder.CustomerId has no foreign-key constraint.",
      "confidence": 0.99,
      "evidence": ["dbgraph://CustomerOrder/CustomerId"]
    }
  ],
  "recommended_actions": [
    "Determine whether the missing constraint is intentional before modifying the schema."
  ]
}
```

### 11.3 Agent registry

Each active agent session should register:

- agent type;
- machine;
- repository;
- parent agent;
- capabilities;
- current task;
- status;
- heartbeat;
- model/runtime;
- start time;
- expected expiration.

---

## 12. Capability and Permission Model

Agent OS should use explicit capabilities rather than broad trust categories.

### 12.1 Example capabilities

```text
repo.read
repo.write
repo.commit
shell.execute
shell.network
cards.read
cards.write
project_memory.read
project_memory.propose
project_memory.commit
global_memory.read
global_memory.propose
global_memory.commit
database_schema.read
database_rows.read
database.write
email.read
email.draft
email.send
task.assign
agent.interrupt
deploy.execute
```

### 12.2 Default roles

#### Repository coding agent

- repo read/write;
- shell execution within project policy;
- cards read/write;
- project-memory read/propose;
- global-memory read;
- no direct global-memory commit.

#### Memory curator

- project-memory read/write;
- graph and event read;
- global-memory promotion proposal;
- no repository edits by default.

#### Machine 3 communication agent

- task and memory read;
- task creation/routing;
- project/global memory proposal;
- agent status and messaging;
- no repository edits;
- no unrestricted shell.

#### ChatGPT gateway

- read-only project summaries and memory by default;
- task creation/commenting;
- message forwarding to Machine 3;
- no direct code execution.

### 12.3 Approval records

High-impact actions should generate explicit approval objects with:

- requested action;
- requesting agent;
- reason;
- estimated impact;
- expiration;
- approving user or policy;
- resulting execution event.

---

## 13. API Boundary

All external clients should interact with a versioned Agent OS API rather than directly querying the central database.

### 13.1 Candidate service modules

```text
agent-os-api/
  auth/
  registry/
  tasks/
  memory/
  content/
  ingestion/
  retrieval/
  events/
  sync/
  approvals/
```

### 13.2 Example endpoints

```text
POST   /v1/agents/register
POST   /v1/agents/{id}/heartbeat
GET    /v1/agents

GET    /v1/projects
GET    /v1/projects/{id}/summary

GET    /v1/tasks
POST   /v1/tasks
PATCH  /v1/tasks/{id}
POST   /v1/tasks/{id}/events

POST   /v1/memory/search
POST   /v1/memory/project/proposals
POST   /v1/memory/global/promotions

POST   /v1/events/batch
GET    /v1/sync/{client_id}

POST   /v1/ingestion/sources
GET    /v1/ingestion/jobs/{id}
```

### 13.3 Authentication

Initial private deployment options:

- mutual TLS between known machines;
- machine API keys stored in environment variables or OS secret stores;
- short-lived signed tokens;
- VPN-restricted API exposure.

Avoid storing long-lived secrets inside repository files.

---

## 14. Task-System Evolution

The current local card system is useful and should remain simple, but the distributed architecture introduces new requirements.

### 14.1 Central task fields

Potential additions:

- global task ID;
- local card ID;
- project and repository IDs;
- parent task;
- dependency graph;
- assigned agent or machine;
- requested capability set;
- approval state;
- source conversation;
- status reason;
- blocked reason;
- due or review date;
- synchronization version;
- completion evidence.

### 14.2 Local and central IDs

A task can retain the existing short local card ID while also receiving a global UUID.

```text
Local display ID:  ab12cd34
Global ID:         6A56...-...
```

### 14.3 Synchronization conflicts

Use optimistic concurrency with:

- version number;
- updated timestamp;
- originating client;
- immutable status-history events.

Conflicting edits should produce a reconciliation event instead of silently overwriting one another.

---

## 15. Suggested Repository Organization

The project may eventually be cleaner as a monorepo or related repository family.

### Option A — Monorepo

```text
agent-os/
  plugins/
    claude-execution/
    claude-control-plane/
  gateways/
    external-mcp/
    telegram/
  services/
    api/
    ingestion/
    memory/
    event-worker/
    vector-index/
  packages/
    contracts/
    client-python/
    graph-model/
    auth/
  database/
    migrations/
    seed/
  docs/
    architecture/
    protocols/
  current-plugin-files...
```

### Option B — Repository family

```text
Claude-Delegation-Plugin       # execution-plane plugin
Agent-OS-Control-Plugin        # Machine 3 plugin
Agent-OS-Server                # API, DB migrations, workers
Agent-OS-External-MCP          # ChatGPT/Codex gateway
Agent-OS-Ingestion             # parsers and data feeds
Agent-OS-Contracts             # shared schemas/client libraries
```

### Recommendation

Start in the current repository with clear internal package boundaries. Split repositories only after contracts stabilize and deployment independence becomes useful.

Premature separation would increase versioning and coordination overhead during the most fluid phase of design.

---

## 16. Incremental Build Plan

This is a large buildout and should be delivered in vertical slices.

## Phase 0 — Architecture Contracts

**Goal:** Define stable interfaces before building distributed behavior.

Deliverables:

- event envelope schema;
- agent registry schema;
- task synchronization schema;
- memory node/edge schema;
- memory proposal schema;
- capability schema;
- context-package schema;
- API versioning rules;
- repository and machine identity format.

Success criteria:

- schemas are documented and validated;
- existing plugin can emit example payloads;
- no central infrastructure is required yet.

---

## Phase 1 — Central Server and Agent Registry

**Goal:** Allow repository agents to register and report status.

Deliverables:

- SQL Server migrations;
- minimal Agent OS API;
- machine/repository/agent registration;
- heartbeat endpoint;
- event ingestion endpoint;
- local outbox and retry logic;
- Machine 3 read-only status tools.

Success criteria:

- two machines can register;
- Machine 3 can list active agents and repositories;
- temporary network failure does not lose events.

---

## Phase 2 — Task Synchronization

**Goal:** Connect existing repository-local cards to the central control plane.

Deliverables:

- global task schema;
- local/global ID mapping;
- card-event publication;
- central task querying;
- Machine 3 task creation and routing;
- conflict handling;
- local-only fallback.

Success criteria:

- a task created on Machine 3 appears in the correct repository;
- repository task updates appear centrally;
- duplicate or conflicting updates are detected.

---

## Phase 3 — Project Memory MVP

**Goal:** Introduce persistent repository memory without global promotion.

Deliverables:

- local memory node/edge store;
- memory search tools;
- memory observation tool;
- code/task/database evidence links;
- curator workflow;
- hooks for task completion and session end;
- local memory UI or graph overlay.

Success criteria:

- a repository preserves decisions across Claude sessions;
- memory entries cite evidence;
- agents retrieve useful prior decisions for new tasks.

---

## Phase 4 — Global Memory and Context Retrieval

**Goal:** Allow project agents and Machine 3 to retrieve shared knowledge.

Deliverables:

- source/chunk ingestion tables;
- vector retrieval;
- global memory node/edge tables;
- global search API;
- project-context imports;
- context compiler MVP;
- provenance display.

Success criteria:

- a repo agent can retrieve relevant user-provided knowledge;
- retrieved context identifies source and scope;
- global information does not overwrite project-specific decisions.

---

## Phase 5 — Promotion and Consolidation

**Goal:** Promote durable project knowledge into global memory safely.

Deliverables:

- promotion candidate queue;
- similarity and recurrence detection;
- contradiction handling;
- consolidation worker;
- approval rules;
- accepted/rejected/superseded states.

Success criteria:

- repeated cross-project knowledge is recognized;
- similar but distinct concepts are not blindly merged;
- every global memory remains traceable to evidence.

---

## Phase 6 — Machine 3 Communication Plugin

**Goal:** Deliver the always-on planning and monitoring agent.

Deliverables:

- separate control-plane plugin;
- project/task/agent tools;
- planning and routing skills;
- user-decision recording;
- report and summary generation;
- Telegram or Remote Control interface;
- explicit no-code-edit permissions.

Success criteria:

- the user can discuss all projects with one persistent agent;
- Machine 3 can delegate tasks without direct repository access;
- Machine 3 can identify blockers and propose relevant ideas.

---

## Phase 7 — General Ingestion Pipeline

**Goal:** Support file dumps, exports, images, and feeds.

Deliverables:

- source registry;
- parser interface;
- high-value format parsers;
- watched-folder ingestion;
- duplicate detection;
- chunking and embedding;
- image metadata/caption handling;
- conversation import;
- feed scheduler;
- extraction monitoring UI.

Success criteria:

- common documents and conversation exports become searchable;
- re-imported sources are versioned or deduplicated;
- failed parsers are visible and recoverable.

---

## Phase 8 — ChatGPT / External MCP Gateway

**Goal:** Expose safe Agent OS interaction outside Claude Code.

Deliverables:

- authenticated external MCP gateway;
- ChatGPT-safe tool subset;
- task and memory tools;
- source submission;
- Machine 3 message routing;
- audit logging;
- rate and capability enforcement.

Success criteria:

- ChatGPT can inspect projects and submit tasks;
- external tools cannot bypass Agent OS permissions;
- all actions are attributable and auditable.

---

## Phase 9 — Operational Hardening

**Goal:** Make the system reliable enough for persistent use.

Deliverables:

- migrations and backup strategy;
- dead-letter queues;
- metrics and health dashboards;
- distributed tracing or correlation IDs;
- secret rotation;
- access reviews;
- retention policies;
- memory cleanup and stale-data detection;
- disaster recovery documentation.

---

## 17. Recommended First Vertical Slice

The best first slice is smaller than “build memory.”

Implement:

1. machine, repository, and agent registration;
2. a shared event envelope;
3. local outbox persistence;
4. central event ingestion into SQL Server;
5. Machine 3 read-only tools for agent status and task monitoring;
6. synchronization of existing task-card lifecycle events.

This slice immediately proves:

- cross-machine communication;
- durable central state;
- compatibility with the current plugin;
- always-on monitoring;
- resilience to temporary network failure.

It also creates the event history needed to build meaningful memory later.

Attempting semantic memory before stable identity, events, and provenance would likely create data that later needs to be reworked.

---

## 18. Key Design Decisions to Resolve

### 18.1 Main database versus service ownership

Decide whether the main DB is accessed only through an API. The recommendation is **yes**.

### 18.2 Task source of truth

Possible models:

- local cards are authoritative;
- central tasks are authoritative;
- event-sourced reconciliation with local cached projections.

Recommended: central canonical identity with local durable projections and event-based synchronization.

### 18.3 Project memory storage

Possible first implementations:

- SQLite node/edge tables;
- JSON graph artifacts;
- local Neo4j;
- Graphify extension;
- central-only project-scoped tables.

Recommended: local SQLite or similarly simple durable store plus graph export, with central synchronization of selected nodes.

### 18.4 Memory-write authority

Recommended rule:

- execution agents create observations and proposals;
- deterministic hooks create factual events;
- curator logic commits project semantic memory;
- consolidation logic commits global memory;
- user decisions can override or approve.

### 18.5 Agent transport

Start with HTTPS and polling/heartbeats. Introduce persistent sockets or a broker only when real-time needs justify the operational cost.

### 18.6 File ownership

Define whether source files are copied into managed storage or referenced in place.

Recommended hybrid:

- immutable copy for explicit dumps and imports;
- reference plus hash for watched local files;
- configurable retention for large binaries.

---

## 19. Risks and Mitigations

### Risk: Global memory becomes noisy

Mitigations:

- promotion candidates rather than direct writes;
- provenance requirements;
- confidence and stability fields;
- contradiction tracking;
- consolidation review;
- retention and stale-memory policies.

### Risk: Over-centralization makes local work fragile

Mitigations:

- local-first execution;
- durable outbox;
- local task and memory cache;
- asynchronous synchronization;
- no mandatory central dependency for coding.

### Risk: Agent permissions become too broad

Mitigations:

- capability-based access;
- default-deny policy;
- explicit approval records;
- separate execution and control-plane plugins;
- audit logs.

### Risk: Cross-agent conversation becomes unmanageable

Mitigations:

- structured artifacts;
- machine/repository coordinators;
- event-driven status updates;
- summaries rather than transcript forwarding.

### Risk: Ingestion produces incorrect graph relationships

Mitigations:

- separate extracted, candidate, and accepted states;
- confidence thresholds;
- evidence links;
- deterministic parsing before semantic inference;
- human or agent review for ambiguous merges.

### Risk: Scope becomes too large for a coherent release

Mitigations:

- vertical phases;
- stable contracts first;
- preserve the current plugin;
- postpone advanced graph and broker infrastructure;
- validate each layer with two-machine real usage.

---

## 20. Testing Strategy

### Unit tests

- schema validation;
- local outbox behavior;
- sync conflict handling;
- memory deduplication;
- parser outputs;
- capability checks;
- event serialization.

### Integration tests

- plugin to API to SQL Server;
- Machine 3 to central task store;
- local card synchronization;
- project-memory proposal flow;
- source ingestion through retrieval;
- ChatGPT MCP permission filtering.

### Failure tests

- central server unavailable;
- partial event batch failure;
- duplicate event delivery;
- machine clock skew;
- stale agent heartbeat;
- task conflict;
- malformed source document;
- embedding worker failure;
- memory contradiction;
- permission denial.

### End-to-end scenario

```text
1. User asks Machine 3 to implement a feature.
2. Machine 3 creates a central task and routes it to Machine 1.
3. Machine 1 receives a local task card.
4. Claude Code claims the task and retrieves project/global context.
5. Subagents implement, test, and review the change.
6. Hooks publish progress and completion events.
7. Project memory records the architectural decision.
8. A promotion candidate is generated if broadly reusable.
9. Machine 3 summarizes the result to the user.
10. ChatGPT can later retrieve the same task and decision through MCP.
```

---

## 21. Documentation to Add

Suggested documentation files:

```text
ARCHITECTURE.md
ROADMAP.md
CONTRACTS.md
EVENTS.md
MEMORY.md
SYNC.md
SECURITY.md
INGESTION.md
CONTROL_PLANE.md
EXTERNAL_MCP.md
DATABASE.md
```

The current integration and task-card documentation should remain, but the new documents should clearly separate:

- local plugin installation;
- optional central Agent OS integration;
- Machine 3 setup;
- server deployment;
- external gateway setup.

---

## 22. Proposed Near-Term Repository Tasks

1. Add `docs/architecture/` and move this proposal there.
2. Define JSON Schemas or Pydantic models for events, agents, tasks, capabilities, memory proposals, and context packages.
3. Add stable machine, repository, project, agent, and session identifiers.
4. Add a repository-local event outbox.
5. Extend hooks to publish structured events into the outbox.
6. Create a minimal SQL Server migration project.
7. Create a small Agent OS HTTP API for registration, heartbeat, and batch events.
8. Add a synchronization worker to the existing MCP server or a companion daemon.
9. Build a read-only Machine 3 proof of concept that lists projects, active agents, and task status.
10. Synchronize the existing card lifecycle before implementing semantic memory.
11. Add local project-memory tables and MCP read/propose tools.
12. Implement provenance links between memory, tasks, code nodes, database nodes, and source chunks.

---

## 23. Final Architectural Position

Agent OS should be treated as a **model-agnostic coordination, memory, and context platform for persistent software agents**.

Claude Code remains the first and most capable execution runtime, but the system boundary is broader:

- local coding agents execute;
- project memory preserves repository understanding;
- the central server coordinates and stores shared state;
- Machine 3 provides persistent user-facing reasoning;
- ingestion turns external information into retrievable evidence;
- global memory consolidates durable cross-project knowledge;
- ChatGPT and other clients connect through controlled interfaces.

The current repository already supplies the core execution substrate. The proposed buildout does not discard that work; it organizes it into a distributed system with durable identity, events, memory, permissions, and cross-agent communication.

The most important implementation order is:

```text
Identity → Events → Synchronization → Tasks → Project Memory
→ Global Retrieval → Consolidation → Ingestion → External Interfaces
```

This order gives every later memory or agent feature a reliable foundation and keeps the buildout incremental rather than requiring a single high-risk rewrite.
