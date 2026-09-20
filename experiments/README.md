# mini-dev reproducible experiments

The fixed manifest selects 300 development questions and a deterministic 30-question
smoke subset. The remaining 200 source questions are excluded from method selection.
Business data stays in read-only SQLite. PostgreSQL contains versioned metadata and
independent column-name/description vectors. Generation uses `deepseek-v4.1-flash`
from the local `.env`; credentials and full traces are never committed.

From the project root in PowerShell:

```powershell
& .\.venv\Scripts\python.exe -m experiments.prepare_dataset
& .\.venv\Scripts\python.exe -m experiments.sqlite_runtime --install --check
& .\.venv\Scripts\python.exe deploy/build-column-index.py --help
& .\.venv\Scripts\python.exe -m experiments.evaluate --selfcheck --output-dir .local-services/experiments/p0_selfcheck
& .\.venv\Scripts\python.exe -m experiments.supervisor --run-id smoke_B0_001 --subset smoke
& .\.venv\Scripts\python.exe -m experiments.supervisor --run-id B0_001
```

Run the smoke subset only after the complete index is activated. Review engineering
failures, freeze a Git commit, then start B0. Existing run IDs reject changed code,
configuration, dataset order, or index manifests. Reuse the exact command to resume
an interrupted run; live orphan workers prevent duplicate dispatch. Each question
runs in a separate process with a 40-model-call budget and 15-minute hard deadline.
On Windows, the actual interpreter registers its PID and creation identity before
dispatch. The venv redirector's PID is not treated as the worker. Timeout cleanup
terminates the actual process tree, and failed cleanup blocks another dispatch.
Only transient infrastructure errors receive up to three retries; incorrect scores
never trigger repeated sampling. Usage across attempts is accumulated. Missing usage
is marked unknown. A provider balance error stops dispatch immediately and leaves
unfinished questions pending. HTTP 429 throttling alone is not a balance error.

`state.sqlite`, per-attempt logs, model usage, raw submissions, predictions and scores
live under `.local-services/experiments/`. The supervisor advances generation to
official evaluation and initial diagnosis. The active Codex Goal owns evidence
review, primary-paper research, implementation and the next experimental decision;
the Python supervisor does not pretend to perform those research decisions itself.
Create `.local-services/experiments/STOP` to stop dispatch between questions.

Official EX uses the locked evaluator's result-set equality, ignoring duplicate rows
and ordering. Every selected question remains in the denominator. Empty/missing final
submissions export an intentionally invalid SQL statement. Only `submit_final_sql`
provides scored predictions; generated text and heuristic fallback SQL are excluded.
The `generation/` inputs contain only question ID, database ID, question and evidence.
Gold SQL lives in sibling `evaluation/` files and must never be passed to the worker.

Business SQL generation and evaluation both use the pinned official SQLite 3.40.1
Windows DLL. On the original Python SQLite 3.51.0 runtime, q701's unchanged gold SQL
exceeded 180 seconds; the pinned runtime completed both executions in 0.39 seconds.
This calibration fix changes neither gold SQL nor the official scoring rule. Runtime
hash and download provenance are included in every run manifest. The supervisor
prevents idle system sleep while alive without changing persistent power settings.

Before retaining a candidate, compare the same 300 questions, report gains/regressions,
cost and paired uncertainty, and complete the two predeclared matched runs per version
(including the first full run). Their chronological pairing cannot be reordered. Monetary
cost ratios remain unverified until matching provider Credit rates or billing records
are available; do not substitute an unrelated USD price. The API's absolute total
spending/call limits remain unset as authorized. The 40-call/900-second question limits
remain fixed, and comparisons report calls, cached/uncached tokens and latency. Cumulative
counters remain authoritative over a retry's last-attempt usage; incomplete usage stays
unknown. Small retained gains below six questions are marked provisional. One
main method changes per candidate. Unsuccessful experiments remain recorded.

Pre-baseline observation: the full index includes 99 columns without descriptions.
Exact-name `PIC` has cosine similarity 1 and rank 1 in the name lane, but may fall
outside default fused top-15 because it receives only one RRF contribution. This is
an observation for later error analysis, not an implemented baseline improvement.

## Completed-run diagnosis and research bookkeeping

After official scoring completes, run diagnosis from the main development checkout:

```powershell
& .\.venv\Scripts\python.exe -m experiments.audit_run --run-dir .local-services/experiments/runs/B0_20260921_01
& .\.venv\Scripts\python.exe -m experiments.diagnose --run-dir .local-services/experiments/runs/B0_20260921_01
& .\.venv\Scripts\python.exe -m experiments.research_registry status
```

The engineering audit reads generation inputs, manifests, worker outputs, tool traces,
and usage checkpoints only. It requires the full fixed set and binds its report to
their hashes. Failed or timed-out questions remain valid terminal records; missing
evidence, database/session drift, lost raw submissions, or contradictory accounting
cannot pass. Agent skill-order deviations are recorded separately. It does not claim
to independently observe hidden retrieval candidates or physical connection state.
For the fixed 30-question smoke use `--subset smoke`. Existing reports are preserved;
use `--output <run-dir>/engineering_audit_new.json` for a new audit. Review the audit
and relevant implementation tests before passing `--checks-passed` to `compare.py`.

Diagnosis compiles SQL under read-only EXPLAIN to identify referenced tables/columns;
it does not execute gold queries, change scores or choose a method. Gold-reference
gaps are explicitly tentative, because equivalent SQL may use different columns.
Outputs stay inside the completed run's `diagnostics/` directory and contain no gold
SQL or literal values. A candidate must meet the registry's matching-model, data,
budget and two-repetition requirements before the best commit can change. Unknown
monetary costs remain unknown. Rejected experiments and review/cycle triggers persist.

The active B0 uses a separate frozen checkout at `F:/data/VSCodeproject/AIDB-SQL-B0`
and commit `5a82da5`. Its local service directory points to this project's ignored
service directory. Resume that run from the frozen checkout, not a later edited tree:

```powershell
Push-Location 'F:\data\VSCodeproject\AIDB-SQL-B0'
& 'F:\data\VSCodeproject\AIDB-SQL\.venv\Scripts\python.exe' -m experiments.supervisor --run-id B0_20260921_01
Pop-Location
```
