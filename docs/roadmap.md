# Roadmap

Vault PR currently governs one explicitly configured Vault Boundary at a time.
The following are possible future directions, not supported capabilities or
committed milestones:

- agent-side selection among independently configured Vault Boundaries;
- reviewed, de-identified export from a more restricted Vault followed by a new
  ingestion into a less restricted Vault.

Neither direction implies an atomic cross-Vault transaction. If a real workflow
requires either capability, its trust boundary and vocabulary should be designed
as a separate context before implementation.
