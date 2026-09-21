# Autonomous mini-dev execution status

Latest update, 2026-09-22: the official-provider second baseline stopped at
**04:34:31 Asia/Shanghai** on explicit **402 / Insufficient Balance**.
It has **66 terminal records (64 submissions, two semantic failures)** and
**234 never-started pending questions**. The supervisor and workers have exited;
no new model requests, balance probes, automatic recharge, or restart are scheduled.
The first full baseline remains 184/300 (61.33%). Partial delivery, the 23-check
stop audit, and exact frozen resumption instructions are recorded in
`RESOURCE_STOP_DEEPSEEK_20260922.md`. The research objective remains incomplete.

Provider transition history: the user supplied a DeepSeek official API key and changed
the endpoint. Authenticated model/balance checks and a two-request tool round trip
passed. The official V4.1-Flash API name is `deepseek-flash`; `.env` now uses that
name. Provider adaptation is frozen at `58de3cb344fcc2337ea01a168060065a8dd2cc7c`
for the independent `deepseek_official_20260922` series, with the same fixed 300/30
questions and budgets. The fixed smoke `DSF_20260922_B0_smoke_01` completed with
official EX **19/30 (63.33%)**, 28 executable submissions, two retained failures,
and zero timeouts. Its complete engineering audit passed with zero errors or
unverified evidence; prompt/completion/total/cache usage is complete for all 295
model calls. Reasoning-token counts were not reported and monetary costs are unknown.
The failures exhausted the existing SQL correction budget and did not submit SQL;
neither showed an API compatibility failure. This smoke is not a 300-question baseline.

The first full baseline `DSF_20260922_B0_01` completed all 300 questions with
official EX **184/300 (61.33%)**, 292 executable submissions, eight missing
submissions, and zero evaluation timeouts. Its complete engineering audit passed:
300 terminal records, 301 attempt sessions, zero engineering errors, and zero
unverified evidence. The immutable 300-row question/SQL/score export and its source
hash manifest are in `.local-services/experiments/deliverables/DSF_20260922_B0_01/`.
One transient failed request in q1252 returned no usage; complete token totals
remain unknown, with reported subtotals retained. Reasoning usage and actual
monetary cost remain unknown.

The second predeclared full baseline `DSF_20260922_B0_02` started at **2026-09-22
04:12:16 Asia/Shanghai**, after the first run's evaluation, audit, and export.
It uses the identical clean frozen `AIDB-SQL-DSF-B0` worktree, commit `58de3cb`,
configuration, provider/model, data, index, and budgets. The second run is now
resource-stopped as described above. Both full baselines must
finish and pass audit before candidate selection/registration in the new series.
Live progress and process identity are persisted under `.local-services/experiments/`.
See `DSF_20260922_B0_01_summary.json`, `DEEPSEEK_OFFICIAL_20260922.md`, and
`deepseek_official_cost_notes.md`. This first new-provider baseline establishes a
reference point; it does not demonstrate a method improvement.

First-run diagnosis also found a derived event-record issue in the frozen agent:
batched SQL responses were assigned to the most recent pending attempt instead of
their tool-call ID. In q26 and q95 this swapped row counts or accepted/rejected
labels in `sql_attempt_records`. Raw ID-bearing `tool_trace`, the native execution
ledger, accepted submissions, and official EX remain intact; the engineering audit
uses those authoritative sources. Main now matches unique call IDs and leaves
unidentified/ambiguous records pending. Eight offline event tests cover the
correction, including late responses without IDs. Historical artifacts and both
frozen baseline runs retain their original code and records; this diagnostic fix
is not a retrieval or SQL-method gain.

The results below describe the preserved earlier Token Plan series. Its stopped
281-question repeat is not resumed with the new provider and is not combined with
the new series. The historical best remains 180/300 within that older series.
See `DSF_20260922_B0_smoke_01_summary.json` for the new smoke summary.

Started 2026-09-21 (Asia/Shanghai) from `codex/dev_legion`, base commit `bd8502f`.
Authorized design and permissions were recovered from task
`01a0b3b9-48c9-7ba2-a3ef-5fcacfde8a0e` and `AUTONOMOUS_MINIDEV_PLAN.md`.

The initial working tree contained deployment scripts, the fixed 300-ID manifest,
column descriptions, `.gitignore`, and dual-column retrieval/SQLite changes. These
were retained; subsequent infrastructure work builds on them.

| Stage | Evidence / state |
|---|---|
| P0 dataset | 300 fixed generation records and 30 fixed smoke records exported; all 11 SQLite hashes recorded; gold isolated from worker input |
| P0 calibration | Passed: 300/300 gold self-checks, all 300 wrong predictions rejected, all missing predictions score zero. Pinned official SQLite 3.40.1 fixes q701 planner regression without changing SQL/data/scoring or 30 s budget |
| P1 full index | Complete and active: `column_dual_v1_624e825a0765bdb2e629`, 798 fields / 1497 vectors; reproducible and database-isolated |
| P2 agent | Fixed 30-question smoke complete: 30 terminal records, 29 genuine submissions/executable SQL, official EX 20/30 (66.67%), 0 timeouts; one bounded model failure retained |
| P2 persistence | Transactional question/attempt records, process isolation, code/data/index/runtime freeze, actual-interpreter PID handshake, request checkpoints, balance detection and resumption implemented; 51 offline checks across evaluation, worker, recovery, batch boundaries and real Windows process trees passed |
| Per-run audit | Reusable generation-only audit passes the complete smoke artifacts: 30 sessions, 29 submissions and one legitimate failure, zero integrity errors or missing evidence; 19 targeted tampering/recovery tests passed |
| P3 B0 | First complete run `B0_20260921_01`: official EX **180/300 (60.00%)**, 290 executable submissions, 10 missing submissions, zero timeouts. Original engineering audit passed; the stricter quota-stop review found one usage flag error (see below), without changing SQL/EX. Second predeclared run `B0_20260921_02` stopped on provider weekly quota exhaustion at **281/300 terminal records** (275 submissions, 6 semantic failures); 19 remain pending. It has no full-run score. Frozen commit `5a82da5` and fingerprint remain unchanged |
| P4/P5 | B0 remains current best; no improvement claimed. First candidate, explicit output-role assignment in data-link, is frozen and pushed as `f60c286` on `dev_20260921_060334` after trace review and reading DIN-SQL/RESDSQL methods, ablations and official implementations. It passed 49 offline checks and is registered pending smoke/two matched full runs. See `methods/projection_roles_v1.md` |

Full local evidence lives in `.local-services/experiments/` and
`.local-services/column-index/`. That historical series stopped at the user-authorized
resource boundary on **2026-09-21 08:13:55 Asia/Shanghai**; it sends no further requests.
The provider reported a weekly quota reset at **2026-09-28 02:33 Asia/Shanghai**.
The research plan is unfinished: matched repeats and candidate evaluation remain.
See `RESOURCE_STOP_20260921.md` for the saved deliverables and resumption sequence.
Every experimental run retains its frozen commit and all failure records.

Smoke run `smoke_B0_20260921_01` binds commit `96a0084`, the full index and the pinned
SQLite runtime. Its 66.67% EX is an engineering smoke result, not the 300-question B0.
No performance improvement is claimed. The Windows redirector issue was found by
inspecting actual process ancestry; normal smoke execution had no process failures.
The repair now registers the actual Python worker identity, verifies it before
dispatch, and terminates the actual process tree on timeout. Six real Windows tests
passed without model calls, including a launcher dying while its interpreter is
still alive and termination failure leaving the question running to block redispatch.

The first B0 used 2,882 actual model calls. Known usage is 51,437,910 prompt tokens,
417,021 completion tokens, and 45,877,760 cached tokens. A single transient request
failed without returned token usage; its bounded retry succeeded. Complete token
totals and monetary costs therefore remain unknown. Every original prediction and
score is retained, and the lightweight summary is `B0_20260921_01_summary.json`.

The initial coverage diagnostic was missing foreign-key target text. The parser now
records these targets as visible structural information without treating them as
explicitly linked columns. A pure text replay corrected eight failed questions,
fully removing six flags (16 to 10); all linked selections remain unchanged.
Twenty-seven diagnosis tests passed. The original report and a separate hash-bound
replay are retained. Remaining reference gaps still do not establish causal retrieval
failures or expected BM25 gains.

Completed-run recovery now reuses validated artifacts instead of recomputing scores.
Fifteen offline tests and a real read-only resume of the first B0 confirmed that
completed files and the active repeat's global control remain unchanged. The main
generation audit also checks candidate policy/skill hashes, with 28 offline tests;
the frozen baseline generation code has not changed.

Quota-stop review found that the frozen exporter could lose unknown usage from an
earlier retry. B01 q960 has an incorrect `usage_unknown=false` flag, while its token
complete totals already remain null. B02 q1209 also exposes known subtotals as complete
token totals. Main state export and the independent audit now preserve this distinction;
63 related state/audit/batch/worker checks passed, plus six repair-tool checks. A real
B02 preview changed only q1209 accounting fields in a separate copy; original artifacts
were untouched. The strict B01 review reports exactly one flag error and zero missing
evidence. Its 180/300 score and all registered hashes remain valid. Restore frozen
generation with `run_batch`, then repair metadata before evaluation as documented in
`RESOURCE_STOP_20260921.md`; do not mix new accounting code into frozen model generation.
