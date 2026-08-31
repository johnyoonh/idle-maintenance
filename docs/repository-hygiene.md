# Repository hygiene

Run repository tooling, including `bd`, from the repository root. `bd where` should resolve the local `.beads` directory. Do not initialize Beads at `/` or another parent directory.

Beads metadata is local task state and is intentionally ignored. Application logs belong under `$HOME/Library/Logs/`, and runtime state belongs under `$HOME/Library/Application Support/idle-maintenance/`. Neither should be copied into the repository.

The tracked `.gitignore` also excludes personal configuration, credentials, caches, generated applications, incident records, and local-only fixtures. Tests must use minimal synthetic data rather than real logs, paths, histories, or credentials.
