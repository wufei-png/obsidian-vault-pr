# Use one authorization and many evidence items

A Semantic Plan records exactly one transaction-local Write Authorization
Snapshot and zero or more Evidence items.

Authorization answers why an agent may propose a write: an explicit Maintenance
Request. Evidence supports what should change and may include Source Captures or
other future explicitly supported evidence types. Bounded retrieval results,
accepted Evaluation Baselines, and diagnostic Quality Findings do not enter the
current Semantic Plan evidence schema. Evidence never grants authority.

This replaces the overloaded Plan Origin concept, which mixed permission and
provenance. Authorization is an audit assertion supplied by the orchestrator,
not a credential or separate lifecycle object. Independent review remains
required by risk and policy. Findings never initiate writes without a current
Maintenance Request.
