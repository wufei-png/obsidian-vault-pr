# Allow only verified qmd versions

Vault PR supports an explicit allowlist of exact qmd build strings, initially
`qmd 2.5.3 (c92adb3)`, rather than inferring compatibility from help-text
markers. Runtime checks still validate the configured collection, command
results, index update, retrieval, and exact-HEAD freshness, but the help-text
capability matrix is removed.

Before another qmd version enters the allowlist, repository or release validation
must run a semantic fixture canary covering the behavior Vault PR relies on,
including hidden `.vault-pr` exclusion, structured output, Unicode and spaced
paths, and duplicate filenames. This makes compatibility deliberately narrow and
test-backed without making every `doctor` invocation create temporary qmd state or
depending on undocumented help-text conventions.

The repository canary is `python scripts/qmd-semantic-canary.py`; it uses a
temporary index and synthetic tracked fixtures, never the configured user Vault.

The supported stamped release is now also `qmd 2.8.3 (facd35e)`. Its published
package records the qmd commit at build time and rejects unrelated ancestor Git
repositories during source-checkout fallback. This fixes the 2.5.3 behavior
that could report a Homebrew repository commit as qmd's build identity. We do
not accept additional 2.5.3 strings produced by Homebrew updates.

Version 2.8.3 preserves exact relative paths, including spaces and underscores;
2.5.3 uses normalized virtual paths. Retrieval selects the path mapping from
the validated runtime snapshot. The release canary resolves actual qmd results
through the application resolver, and adds space, hyphen, and underscore
collision fixtures for the new release. Both tested runtime strings remain
explicitly allowlisted; unverified and dirty builds are rejected.
