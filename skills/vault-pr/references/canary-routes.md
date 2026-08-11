# Capture-route canaries

`vault-pr canary <url>` creates a disposable Fixture Vault, captures and commits
the source, builds a temporary qmd collection, verifies retrieval, removes the
collection, and deletes the fixture. Passing this deterministic route does not by
itself prove semantic integration; the Vault Maintainer must still complete a
bounded plan and any policy-required Semantic Review before the whole Canary
Scenario is accepted.

Maintain these route classes:

| Route | Capture input | Live requirement |
| --- | --- | --- |
| HTML | real public URL, direct raw response | stable public article |
| Markdown | real public `.md` URL, direct raw response | stable public Markdown |
| PDF | real public PDF URL, direct bytes | stable public document |
| WeChat | real public URL plus provider Markdown snapshot | local provider available |
| Authenticated | environment URL plus sanitized browser snapshot | local opt-in only |

The maintained public smoke references are RFC 9110 HTML and PDF from
`rfc-editor.org`, plus this repository's public raw `README.md` for Markdown:

```text
https://www.rfc-editor.org/rfc/rfc9110.html
https://www.rfc-editor.org/rfc/rfc9110.pdf
https://raw.githubusercontent.com/wufei-png/obsidian-vault-pr/main/README.md
```

The maintained WeChat reference is configured by the local operator because
public article availability can change; its repository fixture contains only
synthetic content. Authenticated URLs are supplied only through a local
environment-gated run. Keep these live references out of the default unit-test
suite so hosted CI remains deterministic and does not depend on third-party
network availability.

Never use a user-submitted retention URL as the disposable fixture target. A
maintained canary URL is test data; a user URL is a User Ingestion and goes to the
selected real Vault Boundary after temporary capture validation. Repository and
CI fixtures contain no credentials, private URLs, copied user notes, or private
source text.
