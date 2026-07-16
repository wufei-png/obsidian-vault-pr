# Vault PR

Vault PR governs how outside sources become durable, reviewable knowledge in an
existing vault without taking ownership of every way those sources are acquired.

## Language

**Source Reference**:
An address or local handle that identifies material the user wants integrated.
_Avoid_: Input, URL

**User Ingestion**:
A Source Reference supplied with the intent that its validated knowledge be retained in the selected real Vault Boundary.
_Avoid_: Canary, trial import

**Ingestion Request**:
A user's request to capture one Source Reference and integrate its validated knowledge into a selected Vault Boundary. When semantic writes are proposed, its bounded intent is recorded in the Semantic Transaction's Write Authorization Snapshot; the preceding deterministic capture remains provenance and Transaction Evidence.
_Avoid_: Capture request, import command

**Capture Provider**:
An agent capability that resolves a Source Reference into a local Snapshot Bundle.
_Avoid_: Downloader, vault adapter

**Snapshot Bundle**:
An untrusted local primary document plus any sibling assets produced by a Capture Provider.
_Avoid_: Imported note, capture

**Source Capture**:
The immutable, identity-bound archive accepted from a direct fetch or Snapshot Bundle. It provides Transaction Evidence and never grants write authority by itself.
_Avoid_: Download, cache

**Governance Kernel**:
The deterministic boundary that validates, plans, applies, commits, recovers, and indexes vault changes.
_Avoid_: Agent, downloader

**Vault Boundary**:
One independently opened, configured, indexed, versioned, and governed Obsidian vault whose contents share the same trust and read-access policy.
_Avoid_: Domain, area, folder scope

**Fixture Vault**:
A disposable Git-managed vault containing synthetic notes and configuration for repeatable route, policy, and recovery tests.
_Avoid_: User vault, temporary user vault

**Vault Registry**:
The local agent-facing list of Vault Boundaries and their routing evidence, configuration paths, and allowed source classes; it selects one vault before a workflow starts.
_Avoid_: Multi-vault config, domain map

**Knowledge Promotion**:
A reviewed, de-identified export from a more restricted Vault Boundary followed by a new ingestion into a less restricted Vault Boundary.
_Avoid_: Move, cross-vault transaction

**Semantic Transaction**:
A bounded, reviewable set of interlinked knowledge changes containing one Write Authorization Snapshot and zero or more items of Transaction Evidence.
_Avoid_: Batch edit, import

**Write Authorization Snapshot**:
An immutable, transaction-local record of the user request or current Vault policy under which an agent proposes semantic writes. The orchestrator supplies it and the Governance Kernel records and constrains it; it is audit metadata, not an authenticated credential or reusable permission. Deterministic kernel operations such as capture acceptance, migration, evaluation-baseline recording, and index refresh do not acquire one merely because they write state.
_Avoid_: Authorization token, permission credential, plan origin

**Transaction Evidence**:
A bounded, hash-verifiable item used to justify or review a proposed Semantic Transaction. A transaction may cite multiple evidence items, but evidence does not grant write authority.
_Avoid_: Authorization snapshot, context, prompt

**Maintenance Request**:
A user's explicit request for an agent to change an existing Vault Boundary. When writes are proposed, it is represented by a minimal intent summary in the transaction's Write Authorization Snapshot rather than by the full conversation.
_Avoid_: User prompt, chat transcript, repair ticket

**Canary Scenario**:
A repeatable representative source journey, normally run against a Fixture Vault, used to verify a capture route and its governed knowledge outcome.
_Avoid_: Demo, one-off test

**Fixture Canary**:
A Canary Scenario driven by a deterministic local Snapshot Bundle to verify a capture contract without relying on an external service.
_Avoid_: Live test, mocked success

**Live Canary**:
A Canary Scenario that exercises a maintained real Source Reference and Capture Provider in a Fixture Vault; it is required before a source route is considered operational.
_Avoid_: Fixture, production import

**Durable Knowledge Link**:
An explicitly authored relationship between vault notes that is intended to remain part of the knowledge graph.
_Avoid_: Similarity edge, search result

**Retrieval Neighbor**:
A transient candidate returned by lexical or semantic retrieval that may inform a Semantic Transaction but is not itself a knowledge-graph relationship.
_Avoid_: Related note, graph edge

**Quality Finding**:
An evidence-backed observation about vault integrity, structure, or retrieval quality. It may remain diagnostic and does not by itself authorize autonomous repair.
_Avoid_: Error, repair task

**Note Role**:
The knowledge purpose of a note, which determines the graph and provenance invariants used to assess it.
_Avoid_: Folder, file type

**Quality Record**:
A versioned, retrieval-excluded artifact under `.vault-pr/quality/` containing Quality Findings or evaluation outcomes. It may provide Transaction Evidence for autonomous repair but never grants write authority by itself.
_Avoid_: Temporary report, knowledge note

**Autonomous Repair Policy**:
A current Vault Boundary policy that permits an agent to propose repairs from eligible Git-tracked Quality Records without a new user request. The transaction records the applicable policy in its Write Authorization Snapshot; the referenced Quality Record remains Transaction Evidence.
_Avoid_: Quality finding authorization, self-approval, automatic fix flag

**Claim Conflict**:
Two provenance-bound claims that cannot both hold under the same stated scope and time; recording the conflict does not decide which claim is correct.
_Avoid_: Duplicate, disagreement

**RAG Evaluation Case**:
A versioned query with expected retrieval, answer, and provenance outcomes used to measure regressions in the knowledge system.
_Avoid_: Prompt example, benchmark question

**Evaluation Baseline**:
The accepted outcomes of a versioned RAG Evaluation Case set against which later runs are compared.
_Avoid_: Target score, latest run

**Vault Maintainer**:
The primary agent role accountable for turning Source References and Quality Findings into governed Semantic Transactions.
_Avoid_: User, operator, writer

**Quality Assessor**:
A read-only agent role that produces Quality Findings, Claim Conflicts, and RAG evaluation judgments without planning changes or granting approval.
_Avoid_: Auditor script, reviewer

**Semantic Reviewer**:
An independent agent role that approves or rejects an exact Semantic Transaction without planning or editing it.
_Avoid_: Editor, second maintainer

**Review Packet**:
The immutable evidence, plan, proposed content, risk rubric, and identities needed for an independent semantic decision.
_Avoid_: Prompt context, summary

**Semantic Review Policy**:
The Vault Boundary rule that either requires independent review for every Semantic Transaction or explicitly trusts the Vault Maintainer for transactions outside the risk-based rubric.
_Avoid_: Review flag, approval toggle
