# Separate route canaries from user ingestion

A user-supplied Source Reference expresses intent to retain its validated
knowledge in the selected real Vault Boundary; it is not redirected to a test
vault merely because it also provides useful runtime evidence. Capture and
extraction happen in temporary staging first, with bounded provider fallback and
retry, and exhausted acquisition fails without writing a broken source note.

Maintained route canaries instead run against disposable Fixture Vaults so HTML,
PDF, WeChat, authenticated-page, policy, and recovery regressions can be repeated
without polluting user knowledge. Authenticated live canaries are local and
environment-gated, use an existing browser session to produce a sanitized
Snapshot Bundle, and never place credentials or private URLs in repository or CI;
CI verifies that route with synthetic fixtures.
