# Keep Quality Records with the owning vault

Generic Canary Scenario fixtures, schemas, and role rubrics live with the Vault
PR skill, while vault-specific Quality Records and Evaluation Baselines live in
the owning vault's Git history under `.vault-pr/quality/`. The directory is
excluded from qmd retrieval and knowledge-graph metrics so evaluation artifacts
cannot improve or contaminate the system they measure. This keeps reusable tests
portable without copying personal or restricted vault evidence into the skill
repository, and it preserves an auditable history instead of relying on
temporary reports.
