# Use vaults, not domains, as governance boundaries

Vault PR treats the configured Obsidian vault as the smallest trust, read-access,
Git, retrieval, and governance boundary. Each CLI invocation loads exactly one
vault configuration, retrieves across that vault's full eligible note set, and
applies at most one Semantic Transaction inside it. Vault PR does not require a
vault to be split because it contains multiple folders or topics; an existing
mixed-topic vault may remain one governed vault.

The CLI and capture schema do not model Domain or Area. In particular, there is
no `--domain` argument, domain identity field, domain-to-source-directory map,
domain-to-qmd-collection map, or `cross_domain` review risk. A local Vault
Registry lets the Vault Maintainer select one configuration before capture from
source evidence, user intent, and locally configured policy. A deployment may
register one vault or several independently governed vaults without changing the
CLI data model.

## Consequences

Every registered vault has its own Git history, qmd collection, source
directory, inclusion rules, quality records, and review policy. A low-risk vault
may require independent review only for named semantic risks, while a more
restricted vault may require review for every Semantic Transaction. Reviewer
revision limits remain bounded and configurable. Tests use isolated fixture
vaults to verify both policy profiles; they do not imply a migration or physical
split of a user's existing vault.

A Semantic Transaction cannot read from or write to more than one Vault
Boundary. Moving reusable knowledge from work to personal is a Knowledge
Promotion: first produce and independently review a de-identified export under
the work policy, then ingest that export as a new source under the personal
policy. It is never implemented as an atomic cross-vault edit.
