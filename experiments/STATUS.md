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
| P2 agent | Required skills restored; first real smoke question q1515 succeeded with explicit final submission, 9 model calls, 29.453 s |
| P2 persistence | Transactional question/attempt records, process isolation, code/data/index/runtime freeze, startup handshake, request checkpoints, balance detection and resumption implemented; 44 offline checks passed across evaluation, worker, recovery and batch boundaries |
| P3 B0 | Not started; no baseline score claimed |
| P4/P5 | Await baseline evidence |

Full local evidence lives in `.local-services/experiments/` and
`.local-services/column-index/`. The active Goal continues; this file is a checkpoint,
not a completion claim. Every experimental run must bind a frozen commit and retain
all failed/timeout/missing-submission records.
