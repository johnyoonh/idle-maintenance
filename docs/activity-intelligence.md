# Activity pattern intelligence

Idle Maintenance records every qualifying resource incident, correlates it with coarse local context, and diagnoses a pattern only after recurrence or material worsening. One isolated known incident remains silent; an unknown or severe incident can still use the existing factual alert.

## Privacy boundary

The pattern worker reads three local sources:

- `resource-monitor-state.json` for bounded incident measurements and deterministic process triage;
- the ActivityWatch HTTP API for performance samples, AFK state, and foreground application names;
- Codex `session_meta` records for session timing and the workspace basename.

Window titles, URLs, ActivityWatch text fields, Codex prompts, responses, transcripts, and absolute workspace paths are discarded and never persisted, embedded, or sent to the diagnosis model. ActivityWatch databases are never opened or modified directly.

## Local vector database

`activity-intelligence.sqlite3` lives under `$HOME/Library/Application Support/idle-maintenance/`. It stores sanitized observations, embeddings, diagnosis history, delivery state, and worker health.

The preferred backend is the pinned `sqlite-vec` extension in `requirements.txt`. When it is unavailable, the same SQLite database performs a bounded exact cosine scan and reports `sqlite-exact` as its degraded vector backend. Install the accelerated backend with the repository environment:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Sanitized canonical summaries use `text-embedding-3-small` with 512 dimensions. If the embeddings endpoint is temporarily unavailable, observations are retained with a deterministic local fallback vector and the degradation is visible in `maint status`.

## Strategic diagnosis

The default gate opens when either condition is true:

1. three similar incidents occur within seven days; or
2. two similar incidents occur within 24 hours and the latest peak is at least 1.5 times the earlier median.

Similarity defaults to `0.82`. A cluster is diagnosed at most once per 24 hours, and all clusters share a four-diagnosis daily budget.

Diagnosis uses the OpenAI Responses API with `store=false`, no tools, and a strict JSON Schema. The response includes a summary, causes, evidence, remedies, verification step, confidence, urgency, and uncertainty. Only a diagnosis with confidence at least `0.75` and a concrete reversible remedy creates a notification. No remedy is executed automatically.

`OPENAI_API_KEY` must be available to the background process. `AW_PATTERN_MODEL` overrides the diagnosis model; `OPENAI_EVERYDAY_MODEL` is the secondary override; the default is `gpt-5-mini`.

## Status and manual inspection

The away-return flow starts a detached pattern cycle after interactive review. Inspect it without invoking the API:

```sh
python3 activity_intelligence.py status
maint status --json
```

Run one cycle manually:

```sh
python3 activity_intelligence.py process
```

`maint status` reports the vector backend, stored and pending observations, worker/API degradation, and latest remedy.

## Retention and safety

Observations are bounded to 10,000 rows and 180 days. Cluster diagnoses remain for one year. The prompt treats all observations as untrusted data and forbids automatic commands, settings changes, process termination, or definitive physical-disk attribution. Protected-process policy remains authoritative over any generated suggestion.
