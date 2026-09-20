# Autonomous mini-dev execution status

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
| P3 B0 | First complete run `B0_20260921_01`: official EX **180/300 (60.00%)**, 290 executable submissions, 10 missing submissions, zero timeouts. Full engineering audit passed. Second predeclared run `B0_20260921_02` is active in `F:/data/VSCodeproject/AIDB-SQL-B0`, with the identical frozen commit `5a82da5` and fingerprint |
| P4/P5 | B0 remains current best; no improvement claimed. First candidate, explicit output-role assignment in data-link, is being implemented on `dev_20260921_060334` after trace review and reading DIN-SQL/RESDSQL methods, ablations and official implementations. See `methods/projection_roles_v1.md`; adoption requires two matched runs per version |

Full local evidence lives in `.local-services/experiments/` and
`.local-services/column-index/`. The active Goal continues; this file is a checkpoint,
not a completion claim. Every experimental run must bind a frozen commit and retain
all failed/timeout/missing-submission records.

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
