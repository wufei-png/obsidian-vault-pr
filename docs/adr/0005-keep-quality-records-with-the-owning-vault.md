# Keep Quality Records with the owning vault

Generic Canary Scenario fixtures, schemas, and role rubrics live with the Vault
PR skill, while vault-specific Quality Records and Evaluation Baselines live in
the owning vault's Git history. This keeps reusable tests portable without
copying personal or restricted vault evidence into the skill repository, and it
preserves an auditable history instead of relying on temporary reports.
