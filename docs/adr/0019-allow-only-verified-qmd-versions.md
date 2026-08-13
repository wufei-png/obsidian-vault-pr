# Allow only verified qmd versions

Vault PR supports an explicit allowlist of exact qmd versions, initially the
currently validated qmd 2.5.3, rather than inferring compatibility from help-text
markers. Runtime checks still validate the configured collection, command
results, index update, retrieval, and exact-HEAD freshness, but the help-text
capability matrix is removed.

Before another qmd version enters the allowlist, repository or release validation
must run a semantic fixture canary covering the behavior Vault PR relies on,
including hidden `.vault-pr` exclusion, structured output, Unicode and spaced
paths, and duplicate filenames. This makes compatibility deliberately narrow and
test-backed without making every `doctor` invocation create temporary qmd state or
depending on undocumented help-text conventions.
