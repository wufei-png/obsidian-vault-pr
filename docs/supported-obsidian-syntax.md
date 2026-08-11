# Supported Obsidian syntax

The Governance Kernel validates the prospective tracked Vault state using a
deliberately bounded Obsidian syntax subset. This contract is narrower than the
Obsidian application's parser.

Supported reference forms:

- UTF-8 Markdown notes with optional YAML mapping frontmatter;
- frontmatter `aliases` as a string or list for note resolution;
- wikilinks and embeds such as `[[Note]]`, `[[path/Note|label]]`,
  `[[Note#Heading]]`, `[[Note#^block-id]]`, and `![[asset.png]]`;
- local Markdown image embeds such as `![alt](asset.png)` and angle-bracket
  destinations containing spaces;
- headings levels 1 through 6 and alphanumeric, underscore, or hyphen block IDs;
- embedded `.base` files whose YAML mapping, views, formulas, and supported view
  types pass the deterministic subset validator.

Reference scanning ignores YAML frontmatter, fenced code, inline code, and
single-line Obsidian comments. Note targets and aliases are normalized to
Unicode NFC and compared case-insensitively. Attachment and Markdown-image
targets must match an exact tracked Git path or unambiguous basename; their case
and Unicode representation are not normalized. Relative paths may collapse `.`
and an in-bound `..`, but absolute, escaping, ambiguous, symlinked, or internal
`.git`, `.obsidian`, and `.vault-pr` references are rejected when newly
introduced.

The validator currently does not claim full support for ordinary non-image
Markdown links, every CommonMark destination edge case, Obsidian URI links,
HTML links/embeds, nested comment syntax, custom heading slug rules, or every
`.base` feature. Existing reference debt is reported separately; a transaction
is blocked when it introduces new deterministic reference defects.
