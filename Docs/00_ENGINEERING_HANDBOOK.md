# 00 — Engineering Handbook

Status: **Living document.** This is the constitution of the project. Every phase, every PR, every architectural decision must be consistent with what's written here. If code and this document disagree, the code is wrong — fix the code or open an ADR to change this document, in that order of preference.

---

## 1. Executive Vision

AI Project Vault is not a storage cleaner. Storage optimization is one capability it has — not the product.

The product is an **AI Employee**: a system that builds and maintains a working understanding of a company's complete knowledge as it lives inside Google Workspace (and, later, other storage providers), and acts on that understanding under explicit human control.

Concretely, the platform continuously:

- **Understands** — builds structured knowledge (metadata, content, relationships) out of unstructured storage.
- **Organizes** — surfaces and, when approved, applies structure (naming, location, archival) that storage left implicit.
- **Learns** — refines its understanding of a department's and organization's norms over time, from the same signals a new hire would use.
- **Connects** — relates files, people, departments, and topics to each other rather than treating each file as an isolated object.
- **Recommends** — turns understanding into concrete, explainable, reviewable proposals.
- **Executes** — carries out approved proposals against real storage, safely and auditably.
- **Protects** — treats every read and every write as something that must be authorized, logged, and, where the action is destructive, explicitly approved by a human.

Storage cleanup (duplicate detection, staleness flags, archive candidates) is the first slice of this because it is the cheapest to make trustworthy and the fastest way to prove the pipeline end-to-end. It is not the ceiling of what the architecture is built for. Every module described in this handbook is designed so that "understand the organization's knowledge" generalizes past file hygiene — into search, department-aware assistance, and eventually proactive knowledge work — without an architectural rewrite. That constraint is why the system is built as a pipeline of discrete, replaceable stages (§6, §8) rather than a single "AI that looks at Google Drive" feature.

## 2. Purpose

This handbook exists so every future phase can be implemented independently, by anyone (or any agent), without re-deriving architectural decisions from scratch. It is the single source of truth for how AI Project Vault is built, not what it does day-to-day for a user — see [`01_PROJECT_MASTER.md`](01_PROJECT_MASTER.md) for product vision framing and the phase roadmap. §1 above is the exception: it states the long-run product intent because every architectural choice from §6 onward is evaluated against it.

## 3. Roles

| Role | Owner | Responsibilities |
|---|---|---|
| CTO / Product Architect / Security Architect / AI Architect / Code Reviewer / Sprint Planner | ChatGPT | Writes phase documents, reviews architecture and code, plans sprints, owns this handbook's evolution. |
| Senior Software Engineer & Implementer | Claude | Implements phases against their spec, writes tests, keeps documentation in sync with what was built. |
| Founder / Product Owner / QA Tester / Final Approver | Human | Approves phase documents before implementation, manually tests delivered work, gives final sign-off. |

Roles do not overlap. The implementer does not redefine architecture mid-phase; if a phase spec is wrong or underspecified, that's raised as a blocker, not silently worked around.

## 4. Development Lifecycle

Every sprint follows this loop, and no step is skipped:

```text
CTO writes Phase Document
        ↓
Founder reviews and approves
        ↓
Claude implements against the spec
        ↓
Founder manually tests
        ↓
Bug Fix Sprint (if needed) ──┐
        ↓                    │
CTO Review ←──────────────────┘
        ↓
Documentation updated (phase doc status, ADRs, CTO Dashboard)
        ↓
Next Phase
```

A phase is not "done" until its documentation is updated — an implementation that works but leaves the docs stale is incomplete.

## 5. Repository Organization

```text
apps/
    frontend/     Vite + React web client — presentation layer only
    backend/      API + application services — orchestration, RBAC, AI Gateway boundary
    worker/       background jobs — scanning, intelligence, execution engine

packages/
    shared/       AI Gateway, logging, error types, auth helpers — framework-agnostic
    types/        shared TS types / API contracts — no runtime logic
    ui/           shared presentation components — no business logic
    config/       shared lint/tsconfig/env-schema — consumed by everything

Docs/
    phases/       one document per implementation phase

infrastructure/
    docker/       Dockerfiles, compose files
    scripts/      bootstrap, migration, deploy scripts — orchestration only

.github/
    workflows/    CI/CD pipeline definitions

tests/
    unit/         single module, dependencies mocked
    integration/  real boundaries, local/dockerized dependencies
    e2e/          full system, driven like a real caller

tools/            internal dev tooling — codegen, repo consistency checks
```

**Monorepo philosophy.** One repository, many deployables. A monorepo is chosen because `apps/*` share types, auth logic, and the AI Gateway constantly — splitting them into separate repos would mean either duplicating that code or standing up a package registry before Phase 1 even starts. The tradeoff (larger repo, need for workspace tooling) is acceptable at this project's scale; it gets revisited in an ADR if `apps/*` ever need independent release cadences or ownership boundaries strong enough to justify the split.

**Dependency direction is one-way:** `apps/* → packages/*`. Packages never import from an app. Apps do not import from each other's internals — cross-app communication happens over the documented API/queue contracts, never by reaching into another app's source tree.

### 5.1 Folder Philosophy

Each top-level folder exists to answer one question about any given file at a glance, before reading a line of it:

- **`apps/`** — "Is this something that runs as its own deployable process?" Anything in here has a process boundary: it can be deployed, scaled, and crash independently of the others. If two things must always deploy together, they are not two apps.
- **`packages/`** — "Is this shared, but not itself deployable?" Code here has no process of its own; it exists only to be imported. This is where cross-cutting concerns (types, the AI Gateway, UI primitives, shared config) live so `apps/*` don't reinvent or duplicate them.
- **`Docs/`** — "Where do I find out why, not just what?" Source code shows what the system does; `Docs/` is the only place that records why it's built this way and what's supposed to happen next. Kept at the repository root, not inside any app, because it governs all of them.
- **`tools/`** — "Is this for developers working on the repo, not for the product itself?" Codegen, consistency checks, and one-off maintenance scripts live here specifically so they're never mistaken for something that ships.
- **`tests/`** — "What kind of confidence does this test buy?" Split by test type (unit/integration/e2e), not by app, because the question "how much of the real system does this exercise" matters more for triage than which app the code under test lives in — see §28.
- **`infrastructure/`** — "How does this get run, not what does it do?" Docker, deploy, and operational scripts are kept separate from `apps/*` so that changing how something is deployed never requires touching its business logic, and vice versa.
- **`.github/`** — "What does GitHub itself need to know?" CI/CD workflow definitions and PR process files, scoped to what GitHub's automation reads — nothing product-related belongs here.

If a new file doesn't have an obvious answer to its folder's question, that's a signal it's misplaced, not a signal to add a new top-level folder without an ADR (§30).

## 6. System Architecture

### 6.1 Architectural Patterns

These three patterns apply across every module in §8; they are how modules are built, not modules themselves.

#### 6.1.1 Modular architecture
Each `apps/*` service and each `packages/*` package is a module with one clear responsibility and an explicit public surface (its exported API/types). Internals of a module are never imported directly by another module — only its declared exports are.

#### 6.1.2 Layered architecture
Within `apps/backend` and `apps/worker`, code is organized into layers, each depending only downward:

```text
Presentation   (HTTP controllers / job entrypoints)
      ↓
Application    (use-case orchestration, request/response shaping)
      ↓
Domain         (business rules, entities — no framework, no I/O)
      ↓
Infrastructure (DB access, external APIs, AI Gateway calls, queue producers/consumers)
```

Domain code has zero framework or I/O dependencies — it is the layer that stays stable while frameworks and providers change underneath it. This is what makes the AI Gateway swap-in-a-provider guarantee (§12) actually hold: if domain logic called an LLM SDK directly, changing providers would mean touching business rules.

#### 6.1.3 Event-driven processing
Long-running or fan-out work (scanning a storage account, running intelligence over a batch of files, executing a multi-step action) is never done inline in an HTTP request. `apps/backend` enqueues a job; `apps/worker` consumes it, emits progress/completion events, and the backend/frontend observe those events (polling or push, decided in the relevant phase) rather than blocking on a request. This keeps request latency bounded and makes long jobs resumable/retryable.

### 6.2 High-Level Component Flow

This is the canonical end-to-end shape of the platform. §8 (Core Platform Modules) gives each block its own detailed card; this section exists so the whole shape can be held in one head at once.

```text
Google Workspace
      ↓
Google Drive API
      ↓
Storage Connector      — normalizes a provider's API into one internal shape
      ↓
Storage Scanner        — walks the connector, builds the raw inventory
      ↓
Metadata Engine         — extracts structured facts about each item
      ↓
Knowledge Builder       — relates items to each other and to departments/people
      ↓
Embedding Engine        — turns content/metadata into vectors for semantic operations
      ↓
Recommendation Engine   — turns knowledge + embeddings into proposed actions
      ↓
Execution Engine        — carries out approved actions against storage
      ↓
Dashboard               — surfaces inventory, knowledge, recommendations, status
      ↓
Founder / Employees     — review, approve, and consume the above
```

Each arrow is a durable, inspectable handoff (a database write, a queue message, an API response) — never an in-memory pass-through that disappears if a process restarts mid-flow. That property is what makes §7's data flow resumable and §23's observability possible: every stage's output is something that can be logged, measured, and replayed independently of the stages around it.

## 7. End-to-End Data Flow

This is the lifecycle of a single file from the moment it exists in connected storage to the moment it can be acted on. Every engineer should be able to place any bug report on this timeline.

```text
File Uploaded / Detected
      ↓
Scanner detects              (Storage Scanner, §8.2 — via a delta or full walk)
      ↓
Metadata extracted           (Metadata Engine, §8.3)
      ↓
Content analyzed             (Metadata Engine + AI Gateway, §8.3 / §12)
      ↓
Embeddings generated         (Embedding Engine, §8.5)
      ↓
Knowledge updated            (Knowledge Builder, §8.4)
      ↓
Recommendation recalculated  (Recommendation Engine, §8.6 — only for affected items, not a full re-run)
      ↓
Dashboard refreshed          (Founder/Employee Dashboard, §8.9 / §8.10 — via the event from §6.1.3)
      ↓
Search updated               (Search Engine, §8.8 — index write)
      ↓
Execution possible           (Execution Engine, §8.7 — only after approval, §17)
```

Two properties hold at every step:

- **Incremental by default.** A single file change triggers work scoped to that file and whatever it's related to (its duplicate group, its department's recommendation set) — never a full-inventory recompute. Full recompute is a fallback (e.g. connector reconnection, schema migration), not the steady-state path.
- **Each step is independently retryable.** A failure at "Embeddings generated" does not lose the metadata already extracted, and does not have to be diagnosed by replaying the whole chain from "Scanner detects" — this is a direct consequence of durable handoffs (§6.2) and the background worker model (§8.15).

## 8. Core Platform Modules

Every module below is a bounded unit of responsibility. A module's card states its contract — Purpose, Responsibilities, Inputs, Outputs, Dependencies, Future Expansion — so that implementing or modifying one module never requires reading another module's internals to know how to integrate with it, per the Caveman Repository Workflow (§29).

### 8.1 Storage Connector

- **Purpose.** Normalize one storage provider's API into the single internal shape the rest of the pipeline consumes.
- **Responsibilities.** Auth/token lifecycle for the provider; pagination; rate-limit handling; translating provider-native objects (a Google Drive `File` resource, etc.) into the connector-agnostic inventory shape defined in `packages/types`; exposing change tokens/deltas where the provider supports them.
- **Inputs.** Provider OAuth credentials (scoped per §13's least-privilege rule); provider API responses.
- **Outputs.** Normalized file/folder/permission records consumed by the Storage Scanner.
- **Dependencies.** None inside the platform — a connector is a leaf module. It is the only place provider-specific SDKs/API clients are imported.
- **Future Expansion.** Every additional storage source (§18) is a new connector implementing the same interface; the rest of the pipeline does not change when a connector is added.
- **Implementation (ADR-025).** This module now exists as code, not just as an aspiration: `vault_shared.storage.StorageAdapter` (a `Protocol`) is the common interface, `vault_shared.storage.adapters.GoogleDriveAdapter` is the one concrete implementation (wrapping the pre-existing `GoogleDriveClient`, which remains the only module calling Drive's HTTP API), and `StorageAdapterRegistry`/`build_storage_registry` is the factory every business service and worker composition root gets an adapter from — no service constructs a provider client directly. What varies per provider is declared explicitly as a `StorageCapabilities` flag set (`supports_trash`, `supports_copy`, `supports_change_feed`, …), checked via `capabilities.require(...)`, never via `if provider == "..."` in business logic (enforced by static architecture tests). Every provider failure arrives as one of the normalized `vault_shared.storage.errors` types, which also subclass the pre-existing application error they replace, so no caller-side exception handling changed. Items are addressed by `ProviderFileId` (a distinct type from the database's own file id) and read as streams (`open_read`/`export`), bounded explicitly via `read_bounded(..., max_bytes=...)` when a caller needs the bytes in memory. See ADR-025 for the full design and `tests/storage_contract.py` for the adapter conformance suite every future provider must pass.

### 8.2 Storage Scanner

- **Purpose.** Walk a connected storage source via its connector and produce a complete, current inventory.
- **Responsibilities.** Full and incremental (delta) walks; write-through of the raw inventory to the primary datastore; scan-run tracking (status, progress, errors) for observability (§23) and dashboard progress display.
- **Inputs.** A configured `StorageConnector` instance; the previous scan's change token, when doing an incremental walk.
- **Outputs.** Inventory records (files, folders, metadata, permissions) in the primary datastore; a `scan-completed` (or per-item) event that triggers the Metadata Engine.
- **Dependencies.** Storage Connector (§8.1); runs inside Workers (§8.15).
- **Future Expansion.** Read-only by construction today; this does not change as new connectors are added — no future connector is granted write access to satisfy the scanner.

### 8.3 Metadata Engine

- **Purpose.** Turn a raw inventory record into structured, queryable facts about that item.
- **Responsibilities.** Extract deterministic metadata (size, type, owner, timestamps, path, sharing state) directly from the inventory record; extract content-derived metadata (detected language, document type, extracted text) where the item type supports it; invoke the AI Gateway for content understanding that isn't reducible to a deterministic rule (topic, sensitivity classification, summary).
- **Inputs.** A Storage Scanner inventory record; file content, when available and needed for content analysis.
- **Outputs.** A metadata record per item, feeding the Knowledge Builder and the Embedding Engine.
- **Dependencies.** Storage Scanner (§8.2); AI Gateway (§8.14) for the non-deterministic portion only — see §10 for exactly which parts of metadata extraction require AI versus a deterministic rule.
- **Future Expansion.** New metadata dimensions (e.g. a compliance-relevant classification) are added as new extraction rules/AI calls within this module — they do not require changes downstream, since downstream modules consume the metadata record's schema, not the extraction logic.

### 8.4 Knowledge Builder

- **Purpose.** Relate individual metadata records to each other and to organizational structure (departments, people, topics), turning a pile of per-file facts into a queryable knowledge graph/model.
- **Responsibilities.** Link files to detected owners/departments (§16); group related files (duplicate groups, versions, same-topic clusters); maintain the relationships described in §9's Database Philosophy.
- **Inputs.** Metadata Engine records; Embedding Engine vectors (for similarity-based relationships); department profile data (§16).
- **Outputs.** Knowledge records (relationships, groupings, department associations) that the Recommendation Engine and Search Engine both read.
- **Dependencies.** Metadata Engine (§8.3); Embedding Engine (§8.5).
- **Future Expansion.** This is the module that generalizes the platform past storage cleanup (§1) — richer relationship types (project-level knowledge, cross-department topic tracking) are added here without changing the Scanner, Metadata Engine, or Storage Connector.

### 8.5 Embedding Engine

- **Purpose.** Produce vector representations of content and metadata for semantic operations (similarity, search, clustering) that keyword/deterministic matching cannot do.
- **Responsibilities.** Generate embeddings via the AI Gateway; store and version them (embedding-model changes must not silently corrupt comparisons against previously-generated vectors); expose similarity queries to the Recommendation Engine and Search Engine.
- **Inputs.** Extracted content/metadata from the Metadata Engine.
- **Outputs.** Vector records, keyed to the source item and the embedding model/version used to produce them.
- **Dependencies.** Metadata Engine (§8.3); AI Gateway (§8.14) for the embedding call itself — never a direct provider SDK call, per §12.
- **Future Expansion.** A dedicated vector store is the expected evolution at Petabyte Scale (§22) if Postgres extensions stop being sufficient — tracked as a future ADR trigger, not solved now (§9).

### 8.6 Recommendation Engine

- **Purpose.** Turn knowledge and embeddings into concrete, explainable proposed actions.
- **Responsibilities.** See §17 for the full deep dive — categories, required fields, and confidence/impact rules.
- **Inputs.** Knowledge Builder relationships; Embedding Engine similarity data; department profiles (§16).
- **Outputs.** Recommendation records (never executed directly) consumed by the dashboards (§8.9, §8.10) and, once approved, the Execution Engine.
- **Dependencies.** Knowledge Builder (§8.4); Embedding Engine (§8.5); AI Gateway (§8.14) for rationale generation where needed.
- **Future Expansion.** New recommendation categories are additive — each is a new rule/prompt producing the same record shape, so dashboard and execution-engine code never changes to support a new category.

### 8.7 Execution Engine

- **Purpose.** The only module permitted to perform a mutating action against connected storage.
- **Responsibilities.** Execute an approved recommendation exactly as approved; capture before/after state; write the audit record (§8.12) before and after execution; support rollback where the storage provider and action type allow it.
- **Inputs.** An approved recommendation (approval status set via the Founder/Employee Journey, §14/§15).
- **Outputs.** Mutated storage state (via the Storage Connector); an execution record and an audit log entry.
- **Dependencies.** Storage Connector (§8.1, for the actual mutating call); Audit Engine (§8.12).
- **Future Expansion.** No future phase relaxes the approval requirement (§12) — expansion here means more action types (currently: move, delete, archive, share-change), not less oversight.
- **Implementation (Phase 0 hardening + ADR-025).** `ExecutionService` (`apps/worker/worker/execution/execution_service.py`) is the *only* code in the platform permitted to call a `StorageAdapter`'s mutating methods (`rename`/`move`/`copy`/`create_folder`/`trash`/`restore`/`permanent_delete`/`update_metadata`/`upload` — `vault_shared.storage.MUTATING_METHODS`), enforced by a static test, not just this sentence. It receives a `StorageAdapterProvider` rather than constructing or depending on any concrete provider client, so its guarantees hold for any provider, not just Google Drive: every mutation is re-scoped to the plan's own organization immediately before it runs (`_get_plan_file`, defense-in-depth against a step whose file id was never actually vetted); a retry after a worker crash reuses the original `RollbackRecord` and inspects the item's live state (not-applied / already-applied / conflict) rather than blindly repeating or blindly refusing; a per-plan Postgres advisory lock (`plan_lock_key`) stops two workers executing the same plan concurrently; and a plan applied to a file whose provider-reported `Revision` has changed since the plan was created is refused (`StorageStaleRevisionError`) rather than applied to content nobody reviewed. Rollback is retryable and conflict-aware in the same way — it will not overwrite a change some other actor made to the file after this platform's own mutation.

### 8.8 Search Engine

- **Purpose.** Let founders and employees find things by meaning, not just by filename.
- **Responsibilities.** Maintain a searchable index over metadata and embeddings; serve keyword and semantic queries within the Performance Goals in §20; respect the requesting user's permission scope (a search must never surface an item the user couldn't already see in the source storage).
- **Inputs.** Metadata Engine records; Embedding Engine vectors; the End-to-End Data Flow's "Search updated" step (§7).
- **Outputs.** Ranked search results to the Founder/Employee Dashboards.
- **Dependencies.** Metadata Engine (§8.3); Embedding Engine (§8.5); Authentication (§8.11) for permission-scoped results.
- **Future Expansion.** Multi-connector search (once §18's chain has more than one active connector) is a query-fanout concern inside this module, not a change to how any other module produces data.

### 8.9 Founder Dashboard

- **Purpose.** The founder's single view into storage state, knowledge, recommendations, and execution history.
- **Responsibilities.** Surface scan status, inventory analytics, recommendations awaiting approval, execution history/reports; capture approval/rejection decisions. See §14 for the full journey.
- **Inputs.** Data from every module above, read-only except for approval decisions.
- **Outputs.** Approval/rejection events, feeding the Execution Engine.
- **Dependencies.** `apps/backend` API surface only — the dashboard never queries a datastore or module directly (§6.1.2).
- **Future Expansion.** Reporting depth grows over time; the underlying data contract (recommendation/execution records) does not need to change to support new views on top of it.

### 8.10 Employee Dashboard

- **Purpose.** A department-scoped view for non-founder users. See §15 for the full journey.
- **Responsibilities.** Surface department-relevant recommendations and an "ask AI" interface scoped to what the employee's role and department permit; capture approval decisions within the employee's authorized scope only.
- **Inputs/Outputs.** Same shape as the Founder Dashboard, filtered by RBAC (§8.11) and department profile (§16).
- **Dependencies.** `apps/backend` API surface; Authentication/RBAC.
- **Future Expansion.** Any capability added to the Founder Dashboard is available to the Employee Dashboard by relaxing a permission check, not by building a parallel feature — the two dashboards share components (`packages/ui`) and differ only in scope.

### 8.11 Authentication

- **Purpose.** Establish who is making a request and what they're allowed to do.
- **Responsibilities.** Session/credential lifecycle; RBAC enforcement (§13); the identity boundary every other module trusts instead of re-implementing.
- **Inputs.** User credentials/session tokens.
- **Outputs.** An authenticated, role-resolved identity attached to every request that reaches the Application layer (§6.1.2).
- **Dependencies.** None inside the platform — this is a foundational module every other module depends on, directly or via the presentation layer.
- **Future Expansion.** Multi-tenant isolation (§22) is enforced here first — tenant resolution becomes part of the identity this module establishes.

### 8.12 Audit Engine

- **Purpose.** An immutable record of who did what, to what, and when — distinct from operational logging (§23).
- **Responsibilities.** Append-only writes for every mutating action and every sensitive read; before/after state capture for executions (§8.7); guarantee audit records themselves are never mutated or deleted by any code path.
- **Inputs.** Events from Execution Engine, Authentication (auth events), and any module performing a sensitive read.
- **Outputs.** Audit log records, queryable by the Founder Dashboard for compliance/history views.
- **Dependencies.** None — every other module depends on this one when it needs to record an auditable event, never the reverse.
- **Future Expansion.** Compliance-grade retention/export requirements (relevant at Multi Tenant / Enterprise scale, §22) are additive constraints on this module's storage policy, not a redesign.

### 8.13 Notification Engine

- **Purpose.** Tell the right human the right thing at the right time.
- **Responsibilities.** Route events (scan complete, recommendation ready, approval needed, execution complete/failed) to the appropriate channel (in-dashboard, email — specific channels decided per phase); respect user/department notification preferences.
- **Inputs.** Events from any module via the event-driven backbone (§6.1.3).
- **Outputs.** Delivered notifications; delivery status feeding Observability (§23).
- **Dependencies.** Authentication (to resolve who should be notified and how); event bus/queue (§8.15).
- **Future Expansion.** New event types plug into existing routing/preference logic without this module needing to know the internal meaning of the event — it treats events as addressed messages, not domain objects it interprets.

### 8.14 AI Gateway

- **Purpose.** The sole boundary between this platform and any LLM/embedding provider. See §12 for the full philosophy.
- **Responsibilities.** Expose capability-based methods (`complete`, `embed`, `classify`, …); route each call to a configured provider; keep every caller provider-agnostic.
- **Inputs.** Capability calls from Metadata Engine, Knowledge Builder, Embedding Engine, Recommendation Engine.
- **Outputs.** Provider responses, normalized to a provider-agnostic shape.
- **Dependencies.** None inside the platform — like a connector, it is a leaf module and the only place a provider SDK is imported.
- **Future Expansion.** Adding, removing, or re-weighting providers happens entirely inside this module and its adapters (ADR-007) — no caller changes.

### 8.15 Workers

- **Purpose.** The runtime that hosts every background/asynchronous stage of the pipeline (Storage Scanner, Metadata Engine, Knowledge Builder, Embedding Engine, Recommendation Engine, Execution Engine) as queue-consuming jobs.
- **Responsibilities.** Consume jobs from the durable queue; emit progress/completion events; remain stateless between jobs so any worker instance can pick up any job, including a retried one after a crash.
- **Inputs.** Jobs enqueued by `apps/backend` (user-triggered) or scheduled triggers.
- **Outputs.** Whatever the specific job stage produces (see that stage's own module card), plus lifecycle events for Observability (§23) and Notification Engine (§8.13).
- **Dependencies.** The queue/cache infrastructure (Redis, ADR-005); every module it hosts.
- **Future Expansion.** Horizontal scaling of Workers is the primary lever for the Performance Goals in §20 as inventory size grows — this is a deployment/scaling concern, not an architectural one, precisely because job state never lives in worker memory.

## 9. Database Philosophy

This section explains why each category of information exists and how it relates to the others — not a schema. Schema is defined per-phase, when the phase that needs it is scoped (per §27), against PostgreSQL (ADR-006).

- **Files & Folders.** The core inventory produced by the Storage Scanner (§8.2). Everything else in the system ultimately traces back to a file or folder record — it's the anchor entity.
- **Departments.** Organizational groupings used to scope recommendations, dashboards, and RBAC (§16). A department is a first-class entity, not an inferred label, because Department Intelligence (§16) depends on having a stable profile to reason against.
- **Users.** People who can authenticate (§8.11), belong to one or more departments, and hold roles.
- **Permissions.** The RBAC model (§13) governing what a user can see and do — modeled independently of storage-provider-native permissions (which live on File/Folder records as metadata), because platform permissions and source-storage permissions answer different questions ("can this user use the platform to see this recommendation" vs "can this user open this file in Drive").
- **Embeddings.** Vector representations produced by the Embedding Engine (§8.5), keyed to the item and the model/version that produced them — versioned explicitly so a future embedding-model change doesn't silently corrupt similarity comparisons.
- **Recommendations.** Proposed actions from the Recommendation Engine (§8.6), carrying the fields defined in §17. Recommendations are data the Execution Engine reads, never something it generates itself.
- **Executions.** The record of an approved recommendation actually being carried out — before/after state, outcome, and a reference back to the recommendation and the approval decision that authorized it.
- **Audit Logs.** Immutable, append-only records from the Audit Engine (§8.12) — deliberately modeled and (where practical) stored separately from operational data so a bug in application logic can never retroactively alter audit history.
- **Activity.** User-facing activity/history (what a founder or employee did in the dashboard) — distinct from Audit Logs, which exist for compliance and security review rather than UX.
- **Analytics.** Aggregated/derived views over Files, Recommendations, and Executions (storage growth trends, recommendation acceptance rate) — computed from the entities above, never a second source of truth for them.
- **Relationships.** The Knowledge Builder's (§8.4) output — links between Files, Departments, Users, and each other (duplicate groups, versions, topic clusters). Modeled as first-class relationship records (not just foreign keys on File) because a file can participate in many relationships of different kinds simultaneously.

Every one of the above is tenant-scoped from the first phase that introduces it, per §13's multi-tenant-isolation rule, even while only one tenant exists.

## 10. AI Intelligence Pipeline

```text
Storage
      ↓
Metadata
      ↓
Content Understanding
      ↓
Knowledge
      ↓
Recommendations
      ↓
Execution
      ↓
Reporting
```

This is the same flow as §6.2/§7 viewed from the angle of "where does intelligence get applied, and by what means." At each stage, prefer a deterministic algorithm over an AI Gateway call whenever the answer is computable with certainty — AI is reserved for judgment calls a deterministic rule cannot make:

| Stage | Deterministic where possible | AI Gateway where necessary |
|---|---|---|
| Metadata | Size, type, timestamps, path, owner, sharing state — read directly off the inventory record | Detected topic, sensitivity classification, content summary |
| Content Understanding | Exact-duplicate detection via content hash | Near-duplicate/similarity detection, semantic classification |
| Knowledge | Explicit relationships (folder hierarchy, sharing graph) | Inferred relationships (same-topic clustering across unrelated folders) |
| Recommendations | Rule-based candidates (old-and-unopened, exact duplicates, size thresholds) | Rationale generation, confidence scoring on ambiguous cases, ranking |
| Execution | Always deterministic — an approved action is carried out exactly as specified | Never — the Execution Engine (§8.7) does not call the AI Gateway |

**The LLM is a reasoning component, not the product.** It is invoked, through the AI Gateway (§8.14, §12) only, at the specific points in this table where a rule cannot substitute for judgment. Every other stage is ordinary software, and stays that way — the intelligence pipeline is designed so removing AI from any single stage degrades that stage's output quality, but never breaks the pipeline's structure. This is what §1's "the AI is a reasoning component" and the existing AI Philosophy in §12 mean in architectural terms.

## 11. Technology Stack

A **polyglot monorepo** as of ADR-012: TypeScript for `apps/frontend` and its frontend-facing packages, Python for `apps/backend`/`apps/worker` and their shared package. `packages/types`'s "zero translation layer" promise (as originally scoped in ADR-002) applies only within the TypeScript half of the tree; API contracts between the Python backend and the TS frontend are kept in sync manually against the backend's OpenAPI schema until a codegen step is introduced. Full rationale for each choice is in its corresponding ADR in [`03_ARCHITECTURE_DECISIONS.md`](03_ARCHITECTURE_DECISIONS.md) — this table is the summary, the ADR is the record.

| Layer | Choice | ADR |
|---|---|---|
| Frontend language / runtime | TypeScript on Node.js (LTS) | ADR-002 (superseded in part), ADR-012 |
| Backend / worker language / runtime | Python 3.13 | ADR-012 |
| Monorepo tooling | pnpm workspaces + Turborepo, scoped to the TS portion of the tree | ADR-001, ADR-012 |
| Frontend | Vite + React | ADR-003 (superseded), ADR-012 |
| Backend API | FastAPI + SQLAlchemy 2 + Alembic + Pydantic v2 | ADR-004 (superseded), ADR-012 |
| Background worker | Python + Celery | ADR-005 (queue library superseded), ADR-012 |
| Queue / cache | Redis | ADR-005 |
| Primary datastore | PostgreSQL | ADR-006 |
| AI Gateway | Internal abstraction over provider SDKs (Claude, GPT, Gemini, Ollama, Qwen, Mistral) | ADR-007 |
| Containerization | Docker + Docker Compose (local), target orchestrator TBD | ADR-009 |
| CI/CD | GitHub Actions | ADR-009 |

No technology on this list is used until the phase that needs it is reached. Listing it here is a decision to converge on, not an instruction to scaffold it early.

## 12. AI Philosophy

**The LLM is not the product. The product is the intelligence pipeline.** The LLM is a replaceable component inside that pipeline, which is why every AI interaction in this system passes through one abstraction:

```text
Project Vault
      ↓
  AI Gateway
      ↓
  ┌────┬────┬────────┬────────┬────────┐
Claude  GPT  Gemini  Ollama    Qwen   Mistral
```

Rules that follow from this, and are enforced by code review, not convention alone:

- Only `packages/shared`'s AI Gateway module may import a provider SDK (`@anthropic-ai/sdk`, `openai`, etc.). Nothing in `apps/*` imports a provider SDK directly.
- The Gateway exposes capability-based methods (`complete`, `embed`, `classify`, …), not provider-specific method names or request shapes. Callers do not know which provider answered.
- Provider selection (which model answers a given call) is a Gateway-internal routing decision — configurable, not hardcoded into call sites.
- Swapping or adding a provider is a change inside the Gateway plus its adapter, never a change to callers. If a provider swap requires touching `apps/backend` or `apps/worker` business logic, that is a bug in the abstraction.
- Local/self-hosted models (Ollama) are first-class citizens in the abstraction, not an afterthought — this keeps a no-vendor-lock-in escape hatch real, not theoretical.

### 12.1 AI Principles

These are engineering rules specifically about how AI-derived output is allowed to behave in this system, binding on every module in §8 that calls the AI Gateway:

- **Never execute destructive actions automatically.** No AI Gateway response, regardless of confidence, is ever wired directly to the Execution Engine (§8.7). A recommendation is always an intermediate, human-reviewable artifact.
- **Always require approval.** See §17 — every recommendation carries an approval status, and execution is gated on it being `approved`.
- **Always explain reasoning.** A recommendation without a rationale a non-technical founder can read is not a valid recommendation — see §17's required fields.
- **Every action must be reversible where possible.** The Execution Engine captures before/after state specifically so rollback is possible whenever the storage provider allows it; where it is not possible, the recommendation must say so before approval, not after execution.
- **Every execution must be logged.** Via the Audit Engine (§8.12), unconditionally.
- **Recommendations must contain confidence scores.** A rationale without a confidence signal doesn't let a founder calibrate trust across recommendations — see §17.
- **The AI must never hallucinate storage state.** Any claim a recommendation or dashboard makes about what exists in storage must be grounded in Metadata Engine (§8.3) records, never generated by the AI Gateway from its own assumptions. The AI Gateway is given retrieved facts to reason over; it is never asked to recall storage state from its own training or memory.

## 13. Security Philosophy

- **Zero Trust.** No component trusts another by network location alone; every internal call is authenticated and authorized as if it crossed a trust boundary, because eventually it will.
- **Principle of Least Privilege.** Every service, worker, and connector credential is scoped to only what that component needs. A storage connector's OAuth scope is the narrowest scope that satisfies the current phase's features.
- **RBAC.** Authorization is role-based from the first phase that has users at all; roles and permissions are explicit, not inferred from "is this an admin call."
- **Audit logging.** Every read of sensitive data and every mutating action (especially anything the execution engine performs) is logged with who/what/when, before and after state where applicable. Audit logs are append-only and never contain secrets. See §8.12.
- **Secure secrets management.** Secrets live in environment variables sourced from a secrets manager in deployed environments, never in source control. `.env.example` documents required variables with placeholder values only.
- **Input validation.** Every boundary that accepts external input (HTTP request, queue message, connector API response) validates shape and constraints before it reaches domain logic.
- **Encryption in transit.** TLS everywhere, including internal service-to-service calls once there is more than one deployment target.
- **Encryption at rest.** Datastore-level encryption at rest for the primary database and any object storage the system itself owns (e.g. cached file content).
- **Approval workflow for destructive actions.** Any action the execution engine can take against user storage requires explicit human approval captured before execution — see §8.7. There is no phase in the current roadmap where this requirement is relaxed.
- **No hardcoded credentials.** Enforced by code review and, once CI exists, by a secret-scanning check in `ci.yml`.
- **Multi-tenant isolation (future-ready).** Even while the system serves a single tenant, data models and query patterns are written as if a `tenant_id` boundary exists (scoped queries, no implicit "there's only one org" assumptions) so multi-tenancy is a config/enforcement change later, not a data-model rewrite. See §22.

### 13.1 Security Layers

The bullets above are principles; this table states which concrete layer of the system is responsible for enforcing each one, so "which layer should catch this" has one answer during review.

| Layer | Responsibility | Primarily enforced at |
|---|---|---|
| Authentication | Establish identity | Authentication module, §8.11 |
| Authorization | Decide if an already-identified user may perform this action | Application layer (§6.1.2), using RBAC data from §8.11 |
| RBAC | Define the roles/permissions authorization checks against | Authentication module + database (§9) |
| Audit | Immutable record of sensitive reads and all mutations | Audit Engine, §8.12 |
| Encryption | Protect data at rest and in transit | Infrastructure layer (datastore config, TLS termination) |
| Rate Limiting | Bound abuse and runaway load, including from a misbehaving worker retry loop | Presentation layer (API gateway/controller level) |
| Input Validation | Reject malformed/malicious input before it reaches domain logic | Presentation layer, at every external boundary |
| Secret Management | Keep credentials out of source and out of logs | Deployment/infrastructure config, never application code |
| Approval Workflow | Gate destructive actions on explicit human sign-off | Recommendation → Execution boundary, §17 → §8.7 |
| Least Privilege | Bound the blast radius of any single credential | Connector/provider credential scoping, §8.1 |
| Zero Trust | No implicit trust between internal components | Every service-to-service call, regardless of network topology |

## 14. Founder Journey

The founder is the platform's primary operator and final approver (§3). Their path through the system:

```text
Founder logs in
      ↓
Connect Google Workspace          (Authentication + Storage Connector, §8.11 / §8.1)
      ↓
Scanner starts                    (Storage Scanner, §8.2 — progress surfaced via Workers events, §8.15)
      ↓
Dashboard loads                   (Founder Dashboard, §8.9)
      ↓
Storage analytics                 (Metadata Engine + Knowledge Builder output, §8.3 / §8.4)
      ↓
Recommendations                   (Recommendation Engine, §8.6, with rationale/confidence per §17)
      ↓
Approval                          (Founder Dashboard capture, feeding §8.7)
      ↓
Execution                         (Execution Engine, §8.7 — audit-logged per §8.12)
      ↓
Reports                           (Analytics, §9, and execution history)
```

Design intent: the founder should never be blocked waiting on a long-running step. Connecting a workspace returns immediately; scanning happens in the background with visible progress; the dashboard is usable (showing partial results) before a full scan completes, per the incremental philosophy in §7. Approval is a deliberate, unhurried step — nothing in the UI nudges toward faster approval at the cost of a founder actually reading the rationale.

## 15. Employee Journey

Employees are scoped users operating within their department's context, not full administrators:

```text
Login                              (Authentication, §8.11)
      ↓
Department dashboard               (Employee Dashboard, §8.10, scoped by RBAC + department profile, §16)
      ↓
Ask AI                             (AI Gateway, §8.14, via the backend — never a direct provider call from the frontend)
      ↓
Receive recommendations            (Recommendation Engine, §8.6, filtered to the employee's department/permissions)
      ↓
Approve actions                    (Within the employee's authorized scope only — see RBAC, §13.1)
      ↓
Updated storage                    (Execution Engine, §8.7)
```

An employee's approval authority is a subset of a founder's, defined by role/permission configuration (§13), not a separate code path — the Employee Dashboard and Founder Dashboard share the same underlying approval mechanism in §8.7, differing only in which recommendations and which approval actions RBAC exposes to that user.

## 16. Department Intelligence

The platform understands organizational structure — Marketing, Finance, HR, Operations, Design, Management, and any other department a tenant defines — without running a separate AI system per department.

This is **profile-based intelligence, not separate AI agents.** A department is a data entity (§9) carrying a profile: typical file types and naming patterns, retention expectations, and sensitivity baseline. The Metadata Engine, Knowledge Builder, and Recommendation Engine all read this profile as configuration/context when processing an item associated with that department — the same AI Gateway, the same pipeline code, parameterized differently per department rather than forked per department.

Practical consequence: adding a new department, or refining an existing department's profile, is a data change (updating profile records) — it never requires a code change to any module in §8. If a department's needs genuinely require different pipeline *behavior* (not just different profile data), that is an architectural gap and gets raised as an ADR (§30), not solved by branching module logic per department.

## 17. Recommendation Engine — Deep Dive

The Recommendation Engine (§8.6) turns knowledge into proposed action. Recommendation categories at MVP scope:

- **Duplicates** — exact and near-duplicate files.
- **Unused files** — never or rarely accessed since creation.
- **Old files** — stale by age against a department/tenant-configurable threshold.
- **Large files** — outsized storage consumers relative to their access frequency.
- **Similar creatives** — near-duplicate media/design assets (a superset of generic duplicate detection, tuned for creative-asset workflows).
- **Expired documents** — content with a detected or declared validity window that has passed.
- **Archive candidates** — items matching an organization's own archival policy signals.
- **Storage growth** — trend-based flags (a folder/department growing unusually fast) rather than single-item recommendations.
- **Business knowledge** — gaps or consolidation opportunities surfaced by the Knowledge Builder (§8.4) rather than raw storage hygiene — the clearest expression of §1's "beyond a storage cleaner" intent.

Every recommendation, regardless of category, is invalid without all five of the following fields:

| Field | Meaning |
|---|---|
| Reason | Human-readable rationale — why this recommendation was generated, referencing the specific metadata/knowledge/embedding signal(s) behind it. Never a bare category label. |
| Confidence | A calibrated signal (scale decided when Phase 5 is scoped) reflecting how certain the engine is, so a founder can triage high-confidence items faster than ambiguous ones. |
| Estimated impact | What changes if this is approved — storage reclaimed, risk introduced, or knowledge-quality improvement, stated concretely enough to inform an approval decision. |
| Rollback possibility | Whether, and how, this action can be undone if approved and later regretted — determined by action type and storage-provider capability (§8.7). |
| Approval status | `pending` / `approved` / `rejected` / `executed` — the field that gates the Execution Engine (§8.7) and is the enforcement point for §12.1's "always require approval" rule. |

This table is the contract every recommendation category is validated against — a new category (§8.6's Future Expansion) is only complete once it populates all five fields, not before.

## 18. Future Connector Architecture

```text
Google Drive
      ↓
OneDrive
      ↓
Dropbox
      ↓
S3
      ↓
NAS
      ↓
Mega
      ↓
Local Disk
```

This ordering reflects rollout priority, not a committed timeline — each is added when a phase is scoped for it. Every connector in this chain implements the single `StorageConnector` interface defined in `packages/types` (§8.1, §6.1.1) — concretely, `vault_shared.storage.StorageAdapter` (ADR-025) — registered with `StorageAdapterRegistry` under its `ConnectorProvider` value. What varies per connector: authentication model, pagination mechanics, native permission model, and delta/change-token support — declared explicitly via `StorageCapabilities`, not inferred or special-cased by callers. What must never vary: the normalized inventory shape every downstream module (§8.2 onward) consumes — a connector that needs the rest of the pipeline to special-case its identity is a design defect in that connector, not a reason to weaken the interface. Google Drive is the only implemented adapter as of Phase 1 (ADR-025); it and any adapter added later must pass the shared conformance suite in `tests/storage_contract.py` before its capabilities are trusted by the rest of the platform.

Adding a connector to this chain is ordinary phase work, not inherently an ADR-worthy decision — the interface is already designed to accommodate it (ADR-007's sibling decision for storage, effectively, made concrete by ADR-025). It becomes ADR-worthy only if a new connector reveals an assumption baked into the `StorageAdapter` interface that doesn't generalize (e.g. a provider with no stable item identity across renames, or one that cannot supply any form of `Revision` token for stale-state detection) — in that case, the interface itself is what's changing, and that goes through §30.

## 19. Design Principles

- **AI First.** Where AI can turn a manual, judgment-heavy task into an assisted one (per §10's deterministic-vs-AI table), design for that from the start rather than bolting it on later.
- **Security First.** Every new module is designed against §13/§13.1 before it's designed against its feature requirements — security is not a pass applied afterward.
- **Enterprise Ready.** Multi-tenant, RBAC, and audit-log assumptions (§9, §13) are built in from Phase 1, even at single-tenant scale, so "enterprise-ready" is never a separate future migration.
- **Plugin Based.** Any component that varies by external integration (storage provider, AI provider) is written behind an interface with the variation isolated to an adapter — see AI Gateway (§8.14/§12) and Storage Connector (§8.1/§18).
- **Provider Agnostic.** No module outside a Gateway/Connector adapter knows or cares which concrete provider it's talking to.
- **Event Driven.** Cross-module handoffs are events/queue messages, not synchronous in-process calls, per §6.1.3 — this is what makes §7's data flow resumable.
- **Simple UX.** The Founder and Employee Dashboards (§8.9, §8.10) surface what a non-technical user needs to make an approval decision — rationale, confidence, impact — and nothing that requires engineering context to interpret.
- **Scalable.** Every module's Future Expansion note in §8 exists because scale was considered at design time, not retrofitted — see §22.
- **Modular.** §6.1.1's rule: a module's internals are never reached into. This is what keeps the system's growing module count (§8 already lists fifteen) tractable.
- **Observable.** No module ships without the logging/metrics/health-check baseline in §23.
- **Maintainable.** A new senior engineer should be able to read this handbook once and know where a given piece of logic belongs — see §31.
- **Offline-capable where applicable.** Background jobs (§8.15) are designed to tolerate a worker or connector being temporarily unreachable, resuming rather than failing outright, wherever the underlying operation is naturally resumable (e.g. a paginated scan).

## 20. Performance Goals

Engineering targets to design toward, not guarantees — each is revisited with real measurements once the relevant phase has working code, and updated here (or superseded by an ADR if a target proves architecturally significant, §30).

| Target | Goal |
|---|---|
| Reference scale | 100,000 files in a single connected account |
| Initial full scan | Complete within a defined, phase-scoped duration budget for the reference scale |
| Incremental scan | Materially faster than a full scan for a typical day's change volume — full re-scan is the fallback path, not the steady state (§7) |
| Search latency | Fast enough to feel interactive for indexed queries (§8.8) |
| Dashboard load | Fast enough for interactive use, including before a full scan completes (§14) |
| Background jobs | Resumable after a worker crash or restart, with no lost job state (§8.15) |
| Concurrent users | Support the concurrency a single organization's founder + employee base generates at MVP scale; revisited when Multi Tenant (§22) changes the concurrency profile |

## 21. Engineering Rules

- **No hardcoded secrets.** Enforced by review now, by CI secret-scanning once it exists (§13).
- **No duplicated business logic.** If the same domain rule is needed in two places, it belongs in one shared location (typically `packages/shared` or a shared domain module), not copy-pasted.
- **Single Responsibility Principle.** A module or class that's doing more than one job (per its §8 card, where applicable) gets split.
- **Dependency Injection.** Infrastructure dependencies (datastore, AI Gateway, queue) are injected, never constructed inline in domain/application code — this is what makes those layers testable in isolation (§28) and swappable (§12).
- **Typed APIs.** `packages/types` is the single source of truth for any shape crossing a module boundary — never hand-duplicated request/response types.
- **No circular imports.** A direct consequence of the dependency direction in §5 — if two modules need each other, the shared concern belongs in a third module they both depend on.
- **Small modules.** A module that's hard to summarize in its §8-style card is a module that should be split.
- **Unit tests mandatory** for new logic (§28).
- **Integration tests mandatory** for new module boundaries (§28).
- **Architecture changes require an ADR.** No exceptions — see §30.

## 22. Scalability Philosophy

```text
MVP
      ↓
Single Organization
      ↓
Multi Department
      ↓
Multi Tenant
      ↓
Multiple Storage Providers
      ↓
Petabyte Scale
```

Each stage is a real technical implication, not just a marketing label:

- **MVP / Single Organization.** The current default — one tenant, one Google Workspace connection, the pipeline in §6.2 running end-to-end.
- **Multi Department.** Department Intelligence (§16) layered on top of single-tenant data — a data/config change, not a pipeline fork.
- **Multi Tenant.** The `tenant_id` scoping already present from day one (§13, §9) becomes actively enforced and routed at every query boundary, rather than a no-op with one implicit tenant.
- **Multiple Storage Providers.** The connector chain in §18 executed — each new provider is a new `StorageConnector` implementation, no change to Scanner/Metadata/Knowledge/Recommendation/Execution.
- **Petabyte Scale.** Likely the point where §9's single-Postgres assumption needs a dedicated object/vector store alongside it (flagged in §8.5's Future Expansion) — explicitly a future ADR trigger, not something this handbook pre-solves.

Every architectural decision in this handbook is evaluated against "does this block the next stage," not just "does this work today" — that standard is why, for example, tenant scoping (§13) is enforced from Phase 1 instead of retrofitted at the Multi Tenant stage.

## 23. Observability

| Signal | Answers | Why it exists |
|---|---|---|
| Logging | "What happened?" | Structured, per-request/per-job record of execution — the first thing read when diagnosing a specific failure (§11's logging standard). |
| Metrics | "How much, how often, trending how?" | Aggregate signals (scan duration, recommendation volume, queue depth) that reveal degradation before a single request fails. |
| Tracing | "Where did this request/job spend its time across module/service boundaries?" | Needed once a single user action spans Presentation → Application → Domain → Infrastructure and possibly a worker job (§6.1.2, §6.1.3) — a log line alone won't show the cross-boundary picture. |
| Audit Logs | "Who did what to user data, immutably?" | Compliance and security review — deliberately distinct from operational logging (§8.12, §13). |
| Health Checks | "Is this instance alive and ready to serve traffic?" | What deployment orchestration uses to route traffic and restart failed instances. |
| Error Reporting | "What broke unexpectedly, and how often?" | Surfaces and groups failures beyond a single log line, so a regression is visible before a founder reports it. |
| Monitoring / Alerting | "Should a human be paged right now?" | Turns metrics/health/error signals into action — the difference between data existing and someone finding out in time to act on it. |

Observability is planned from Phase 1 (structured logging, health checks) and deepens as the number of services and background jobs grows (tracing, dashboards, alerting) — it is never added retroactively to a module that shipped without it, per §21's "no exceptions" stance on discipline items.

## 24. Git Strategy

```text
main          ← production-released code only, tagged per release
  ↑
production    ← what's currently deployed to production, promoted from develop
  ↑
develop       ← integration branch, always in a working/deployable state
  ↑
feature/*     ← one branch per unit of work, branched from develop
```

**Merge policy:**
- `feature/*` → `develop`: PR required, CI must pass, at least one review (CTO/reviewer role) approved, PR template checklist complete.
- `develop` → `production`: promoted when a sprint's phase work is CTO-reviewed and the founder has manually tested it. No direct commits to `production`.
- `production` → `main`: tagged release once production has been verified stable. `main` always reflects exactly what's live.
- No direct pushes to `main`, `production`, or `develop` — everything arrives via PR, including hotfixes (`hotfix/*` branched from `production`, merged back to both `production` and `develop`).
- Force-push and history rewriting are not used on `main`, `production`, or `develop` under any circumstance.

## 25. Branch Naming

```text
feature/auth
feature/google-drive
feature/storage-scanner
feature/dashboard
feature/chat
feature/recommendation-engine
feature/security
hotfix/<short-description>
```

Pattern: `feature/<kebab-case-scope>`. The scope names the capability being built, not the phase number — a phase may span multiple feature branches, and a branch name should still make sense read in isolation a year later.

## 26. Coding Standards

**Naming.**
- Files: `kebab-case.ts`. Classes/types/interfaces: `PascalCase`. Variables/functions: `camelCase`. Constants that are truly constant: `SCREAMING_SNAKE_CASE`.
- No abbreviations except universal ones (`id`, `url`, `db`). A name should tell a reader what it holds without needing the declaration line.

**Folder conventions.** Mirror the layered architecture (§6.1.2) inside each app: `presentation/`, `application/`, `domain/`, `infrastructure/`. A file's folder tells you its layer before you read a line of it.

**API conventions.** REST resources are nouns, plural, kebab-case (`/storage-connectors`, not `/getConnectors`). Versioned from the first exposed endpoint (`/v1/...`) since a breaking change is guaranteed eventually. Request/response shapes are defined in `packages/types` and imported by both `apps/backend` and `apps/frontend` — never hand-duplicated.

**Error handling.** Domain and application layers throw typed errors (defined in `packages/shared`), never raw strings or provider-specific error objects. The presentation layer is the only layer that translates an error into an HTTP status/response shape. Errors are never swallowed silently — a caught error is either handled meaningfully or re-thrown, never dropped.

**Logging.** Structured logging (JSON) via the shared logger in `packages/shared`, never `console.log` in committed code. Log levels are used meaningfully (`debug`/`info`/`warn`/`error`); secrets, tokens, and PII are never logged — see §13.

**Comments.** Default is no comments. Write one only when the *why* isn't obvious from the code — a non-obvious constraint, a workaround for a specific known issue, an invariant a reader could otherwise break. Never comment what the code already says.

**Commit messages.** Imperative, present tense, under 72 characters for the summary line: `Add IRA triage classifier`, not `Added` or `Adding`. One logical change per commit.

**Pull requests.** Use `.github/PULL_REQUEST_TEMPLATE.md`. A PR maps to one phase or one clearly-scoped fix — not a grab-bag of unrelated changes.

**Review checklist.** Reviewer confirms: scoped diff, naming/folder/layering conventions followed, no direct provider SDK usage outside the AI Gateway, no secrets, tests present for new logic, docs updated if architecture changed. Full checklist lives in the PR template so it travels with every review, not just in this handbook.

## 27. Documentation Standards

Every phase document under `Docs/phases/` must include, in this order:

1. **Objective** — what this phase delivers and why, in plain language.
2. **Architecture** — how this phase's work fits the system architecture in §6/§8; any deltas to it.
3. **API changes** — new/changed endpoints, request/response shapes.
4. **Database changes** — schema additions/migrations, consistent with §9's entity model.
5. **Folder changes** — new files/directories this phase introduces, mapped to §5.
6. **Backend changes** — what changes in `apps/backend`.
7. **Frontend changes** — what changes in `apps/frontend`.
8. **Security** — new attack surface introduced and how it's mitigated, per §13/§13.1.
9. **Tests** — what's covered at unit/integration/e2e level.
10. **Manual checklist** — concrete steps the founder runs to verify the phase by hand.
11. **Definition of Done** — the checklist that must be fully checked before the phase is considered complete.

A phase document that's missing a section is not ready for implementation — send it back rather than filling gaps with assumptions during implementation.

## 28. Testing Strategy

Three layers, matching `tests/`:

- **Unit (`tests/unit/`).** One module in isolation. All external dependencies (network, filesystem, datastore, AI Gateway) mocked or faked. Fast, run on every save/PR. Tests behavior, not implementation details — one assertion's worth of behavior per test, named for what it proves (`blocks_action_when_approval_missing`, not `test1`).
- **Integration (`tests/integration/`).** Real boundaries between modules/services (backend ↔ database, backend ↔ worker queue) against local/dockerized dependencies rather than mocks. Verifies the seams, not just the units either side of them.
- **E2E (`tests/e2e/`).** Drives the system the way a real caller would — API surface or UI — against a fully assembled environment. Fewest tests, highest confidence; reserved for critical paths (auth, storage scan → recommendation → approved execution).

TDD is the default workflow: write the test (or describe the expected behavior) before the implementation, make it pass with the smallest change that does so, then refactor with the test as a safety net. A phase is not done if it shipped without tests for its new logic — see Definition of Done in each phase document.

## 29. Caveman Repository Workflow

Standard operating discipline for working in this repository, independent of any specific tool:

- **Never load the entire repository into context.** Load only the modules a task actually touches, guided by the phase document's Folder Changes section and the dependency direction in §5.
- **Load only affected modules.** If a phase touches `apps/backend/.../scanner` and `packages/types`, that's what gets read — not every app, not every package.
- **Use indexed context.** Rely on this handbook, the relevant phase document, and targeted search/grep over a module's contents rather than a full-repo read, before writing code. The §8 module cards exist specifically to make this possible — a module's card should be enough to integrate with it without reading its internals.
- **Keep prompts/tasks incremental.** One phase, one feature branch, one reviewable chunk of work at a time — not a sprint's worth of changes in one pass.
- **Minimize token/context usage** as a matter of course, not just when it becomes a problem — it keeps reviews scoped and diffs honest about what actually changed.
- **Maintain architectural consistency** by treating this handbook and the relevant ADRs as required reading before touching a module you haven't touched before, rather than pattern-matching off whatever code happens to be nearby.

This is why §5's dependency direction and §6.1's layering are strict: a codebase where any file might import any other file is a codebase that can't be worked on incrementally, because "the affected modules" stops being a small, knowable set.

## 30. Change Management

Any decision that changes technology stack, architecture, or a convention in this handbook is recorded as an ADR in [`03_ARCHITECTURE_DECISIONS.md`](03_ARCHITECTURE_DECISIONS.md) before or alongside the change — not after the fact, and not left implicit in a PR description. This handbook is updated to match once an ADR is accepted; if they ever disagree, the ADR log has the more recent decision and the handbook is stale and needs a docs pass.

## 31. Definition of Engineering Success

AI Project Vault is successful, as an engineering effort, only if all of the following remain true as the system grows:

- Architecture remains modular — §8's module boundaries hold rather than eroding into a tangle.
- Features can be added without rewrites — each phase extends the pipeline in §6/§7 rather than reshaping it.
- Storage providers are replaceable — §18's connector chain executes without touching Scanner/Metadata/Knowledge/Recommendation/Execution.
- AI providers are replaceable — §12's Gateway guarantee holds under real multi-provider usage, not just in theory.
- Every feature is testable — §28's three layers actually exist for every new module, not retrofitted after a bug.
- Every destructive action is auditable — §8.12's audit trail has no gaps.
- Every recommendation is explainable — §17's five required fields are never optional in practice.
- Every engineer can understand the system by reading this handbook once — if a new senior engineer needs anything beyond this document and the relevant phase doc to start working, this handbook has a gap, and closing that gap is itself engineering work, not documentation overhead.

This section is the standard every phase review and every PR review is implicitly measured against, alongside that phase's own Definition of Done (§27).
