# Vault PR

Vault PR governs how outside sources become durable, reviewable knowledge in an
existing vault without taking ownership of every way those sources are acquired.

## Language

**Source Reference**:
An address or local handle that identifies material the user wants integrated.
_Avoid_: Input, URL

**Capture Provider**:
An agent capability that resolves a Source Reference into a local Snapshot Bundle.
_Avoid_: Downloader, vault adapter

**Snapshot Bundle**:
An untrusted local primary document plus any sibling assets produced by a Capture Provider.
_Avoid_: Imported note, capture

**Source Capture**:
The immutable, identity-bound archive accepted from a direct fetch or Snapshot Bundle.
_Avoid_: Download, cache

**Governance Kernel**:
The deterministic boundary that validates, plans, applies, commits, recovers, and indexes vault changes.
_Avoid_: Agent, downloader

**Semantic Transaction**:
A bounded, reviewable set of interlinked knowledge changes derived from one Source Capture.
_Avoid_: Batch edit, import
