# Activity pattern intelligence

Idle Maintenance records every qualifying resource incident, correlates it with coarse local context, and diagnoses a pattern only after recurrence or material worsening. One isolated known incident remains silent; an unknown or severe incident can still use the existing factual alert.

## Privacy boundary

The pattern worker reads three local sources:

- `resource-monitor-state.json` for bounded incident measurements and deterministic process triage;
- the ActivityWatch HTTP API for performance samples, AFK state, and foreground application names;
- Codex `session_meta` records for session timing and the workspace basename.

Window titles, URLs, ActivityWatch text fields, Codex prompts, raw responses, transcripts, and absolute workspace paths are discarded and never persisted, embedded, or sent to the diagnosis model. An investigation launched by Idle Maintenance carries a random reference and asks Codex for one bounded `IDLE_MAINTENANCE_SUMMARY_JSON` footer. After Codex exits, a post-exit helper locates that exact reference in recent rollout files and stores only the validated, sanitized classification, finding, remedy, and confidence. Unstructured output is ignored. ActivityWatch databases are never opened or modified directly.

## Local vector database

`activity-intelligence.sqlite3` lives under `$HOME/Library/Application Support/idle-maintenance/`. It stores sanitized observations, embeddings, diagnosis history, delivery state, and worker health.

The preferred backend is the pinned `sqlite-vec` extension in `requirements.txt`. When it is unavailable, the same SQLite database performs a bounded exact cosine scan and reports `sqlite-exact` as its degraded vector backend. Install the accelerated backend with the repository environment:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

Sanitized canonical summaries use `text-embedding-3-small` with 512 dimensions. If the embeddings endpoint is temporarily unavailable, observations are retained with a deterministic local fallback vector and the degradation is visible in `maint status`.

## Strategic diagnosis

The default gate opens when any condition is true:

1. three similar incidents occur within seven days; or
2. two similar incidents occur within 24 hours and the latest peak is at least 1.5 times the earlier median; or
3. one incident has no similar event in the lookback window and its process I/O peak is at least four times the configured incident threshold (80 MiB/s total or 40 MiB/s writes with defaults).

Similarity defaults to `0.82`. A cluster is diagnosed at most once per 24 hours, and all clusters share a four-diagnosis daily budget.

Growing incidents refresh their stored peaks and are reconsidered when severity increases; an already diagnosed incident is not diagnosed again for a higher peak.

A single-incident diagnosis is explicitly framed as uncertain evidence, not a confirmed cause. `activity_intelligence_novel_incident_peak_multiplier` controls the severity multiplier; the total and write thresholds use `process_high_io_total_mib_per_second` and `process_high_io_write_mib_per_second`.

Completed investigations feed the next pattern decision. Two identity-linked `normal` outcomes with confidence of at least `0.75`, and no conflicting `actionable` outcome, suppress a matching false-positive pattern without calling the diagnosis model. Actionable or uncertain outcomes are added to the evidence supplied to the next diagnosis so the suggestion starts from observations already completed by the user. A single investigation can never suppress a pattern.

Diagnosis uses the OpenAI Responses API with `store=false`, no tools, and a strict JSON Schema. The response includes a summary, causes, evidence, remedies, verification step, confidence, urgency, and uncertainty. Only a diagnosis with confidence at least `0.75` and a concrete reversible remedy creates a notification. Model text is never interpreted as a command.

Automatic remediation is available only through `DEFAULT_REMEDIATION_ACTIONS` in `activity_intelligence.py`. Each registered action must use a deterministic incident/diagnosis matcher and provide bounded execution, authoritative verification, and rollback callbacks; action callbacks receive a monotonic deadline and must honor it. Remediation requires confidence of at least `activity_intelligence_remediation_confidence` (default `0.95`). The event/action pair is recorded as running before execution, and an uncertain running action is verified before any recovery attempt after restart. Failed verification triggers rollback; an unsuccessful rollback is recorded as `recovery_required`. `maint status` exposes the latest outcome.

The production registry starts empty because current resource incidents have no vetted unattended correction and the resource monitor prohibits automatic process termination. New actions must preserve that boundary; adding an action is a code change that requires its own tests and safety review.

A transient diagnosis failure leaves its spike pending and pauses further model attempts for 30 minutes. The worker continues ingesting local observations during that interval and retries later instead of consuming the failed event or repeatedly calling the model.

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

`maint status` reports the vector backend, stored investigation summaries, stored and pending observations, worker/API degradation, and latest remedy.

## Retention and safety

Observations are bounded to 10,000 rows and 180 days. Cluster diagnoses and remediation outcomes remain for one year. The prompt treats all observations as untrusted data and forbids model-selected commands, settings changes, process termination, or definitive physical-disk attribution. Protected-process policy remains authoritative over any generated suggestion.
