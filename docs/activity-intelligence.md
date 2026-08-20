# Activity pattern intelligence

Idle Maintenance can aggregate repeated resource spikes with nearby activity context and ask Codex for one pattern-level diagnosis instead of treating every spike as an isolated notification.

## Evidence sources

The local event store combines three sources:

- `resource-monitor-state.json`: sustained I/O incidents detected by Idle Maintenance;
- `app_usage.json`: recently observed foreground application activity from the bundled app-usage watcher;
- Codex investigations opened from Idle Maintenance, recorded as investigation events before the existing interactive Codex workflow starts.

Other ActivityWatch-style collectors can write into the same store with the `record` command:

```sh
python3 activity_intelligence.py record \
  --source activitywatch \
  --kind foreground-activity \
  --summary "foreground app Obsidian"
```

Only compact event summaries and bounded metadata are persisted. Foreground application paths are reduced to application names before storage.

## Local vector store

`activity-intelligence.sqlite3` under `$HOME/Library/Application Support/idle-maintenance/` is a bounded SQLite event ledger. Each event receives a deterministic local feature vector; similarity uses cosine distance over those vectors. No external embedding service is required and raw observations are not uploaded merely to build the index.

The default retention policy keeps at most 2,000 events for 30 days. The store is intended for recent behavioral recurrence, not indefinite activity history.

## Strategic LLM gate

A Codex diagnosis is eligible only when all default conditions are met:

1. at least three semantically similar resource-spike events exist within seven days;
2. event similarity is at least `0.72`;
3. no semantically similar diagnosis was produced in the prior 24 hours;
4. the current processing cycle has not already run its one allowed diagnosis.

One or two isolated events are indexed and marked reviewed without invoking Codex. A later matching spike can still use those reviewed events as pattern evidence.

When the gate opens, the prompt contains the repeated spike summaries, deterministic process-triage guidance, and nearby foreground/Codex context. The prompt asks for a concise likely cause, supporting evidence, the safest reversible remedy, and one verification step.

The default headless command runs an ephemeral Codex session outside a repository with user config/rules ignored, web search disabled, and a read-only sandbox. The diagnosis prompt explicitly forbids executing commands, changing settings, terminating processes, or claiming physical-disk attribution. A failed or unavailable Codex invocation records no diagnosis and performs no corrective action.

## Delivery

The away-return maintenance flow launches the intelligence cycle detached after interactive review, so a Codex call cannot block the normal UI/focus handoff. The newest successful suggestion is written to:

`$HOME/Library/Application Support/idle-maintenance/activity-intelligence-latest.md`

A successful new diagnosis also produces one macOS notification summarizing the remedy. The diagnosis remains advisory; Idle Maintenance does not execute the suggested action automatically.

Inspect the store without invoking the LLM:

```sh
python3 activity_intelligence.py status
```

Run one ingestion/diagnosis cycle manually:

```sh
python3 activity_intelligence.py process
```

## Configuration

These keys can be overridden in the normal Idle Maintenance config:

| Key | Default |
| --- | ---: |
| `activity_intelligence_enabled` | `true` |
| `activity_intelligence_min_pattern_events` | `3` |
| `activity_intelligence_similarity_threshold` | `0.72` |
| `activity_intelligence_lookback_days` | `7` |
| `activity_intelligence_retention_days` | `30` |
| `activity_intelligence_max_events` | `2000` |
| `activity_intelligence_diagnosis_cooldown_hours` | `24` |
| `activity_intelligence_diagnosis_similarity_threshold` | `0.84` |
| `activity_intelligence_max_diagnoses_per_cycle` | `1` |
| `activity_intelligence_context_minutes` | `20` |
| `activity_intelligence_llm_timeout_seconds` | `90` |

`activity_intelligence_llm_command` may replace the Codex command entirely. Setting `activity_intelligence_enabled` to `false` disables detached processing while leaving the existing resource monitor and review behavior unchanged.
