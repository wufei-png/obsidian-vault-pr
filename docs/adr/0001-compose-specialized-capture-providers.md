# Compose specialized Capture Providers outside the governance CLI

`vault-pr` keeps deterministic fetching, validation, vault writes, recovery, and
indexing in the Governance Kernel, while the agent selects optional specialized
Capture Providers for sources such as public WeChat articles and authenticated
pages. Providers produce an untrusted local Snapshot Bundle; the CLI does not
depend on provider skills and accepts only a validated bundle through its snapshot
interface. This preserves a one-link user experience without coupling the CLI to
every browser or site-specific acquisition tool.

## Consequences

Provider availability and environment policy determine the capture route, so
there is no separate strict-versus-practical vault mode. An unavailable or
disallowed provider falls back to another approved provider or a user-supplied
snapshot. `defuddle` remains a CLI executable used to extract already captured
ordinary HTML; the `defuddle` skill is not a runtime dependency.
