# Candidate: explicit projection roles

Status: implemented with offline checks, not yet evaluated. Parent best version: B0,
`5a82da5ad0e2ad301f33bb8f5afdbdba46030fa4`. Method branch:
`dev_20260921_060334`. The independent checkout is
`F:/data/VSCodeproject/AIDB-SQL-projection-roles`.

## Evidence and hypothesis

The first complete B0 scored 180/300 official EX. Its 110 executable mismatches
include 35 predictions with more output columns than the reference. That is a
diagnostic count, not 35 recoverable answers. A bounded development-only review
confirmed seven cases where `data-link` already labeled support-only attributes
as query targets; removing those extra output columns from the saved prediction
matched the reference tuple set in an offline counterfactual. These are neither
new predictions nor promised candidate gains. Another projection case arose
during correction, and a missing-output case belongs to the opposite failure
direction; neither motivates this intervention.

The existing SQL drafting skill treats upstream query targets as its SELECT
whitelist. The candidate changes only the upstream role assignment: fields
requested in the answer are distinct from fields needed to select, order, filter,
group or join. A requested field may have both roles. Every explicitly requested
output must remain, including compound requests and requested numeric results.
An Evidence formula becomes one output target only when its result is requested;
otherwise it remains available in the supporting role. Required formula inputs
and other support fields still participate in retrieval.

## Original research and implementation

DIN-SQL decomposes linking, classification/decomposition, generation and correction.
Its Section 5.6 reports that ambiguous schema links can introduce redundant joins
or output columns. Table 5 reports Spider development EX of 69.9 for the complete
CodeX Davinci system, 65.9 without schema linking and 67.3 without correction.
These are component ablations in a different system, not a test of this prompt
rule. The official easy-query demonstrations use broad schema links while
projecting only the requested fields. Sources:
[published paper](https://proceedings.neurips.cc/paper_files/paper/2023/file/72223cc66f63ca1aa59edaec1b3670e6-Paper-Conference.pdf),
[official implementation, pinned commit](https://github.com/MohammadrezaPourreza/Few-shot-NL2SQL-with-prompting/blob/0474801616130413b024fd008b016a190684649e/DIN-SQL.py).

RESDSQL separates trained schema ranking from skeleton-aware SQL decoding. Its
Table 5 gives Base EX 77.9, 70.1 without schema ranking and 77.1 without skeleton
parsing on Spider development data. The official skeleton extractor collapses
column placeholders, so it does not supply a strict output-column-count guard.
The relevant inference is only that schema relevance and output structure are
different objects. Sources:
[published paper](https://ojs.aaai.org/index.php/AAAI/article/download/26535/26307),
[official skeleton extraction](https://github.com/RUCKBReasoning/RESDSQL/blob/7472f7a51fdd054d8139b1bc2627d955aff855e4/preprocessing.py),
[official training target construction](https://github.com/RUCKBReasoning/RESDSQL/blob/7472f7a51fdd054d8139b1bc2627d955aff855e4/text2sql_data_generator.py).

This is a paper-inspired prompt hypothesis. It does not reproduce either system,
copy benchmark demonstrations, introduce a trained ranker or add generation calls.
The local detailed review and paper reading notes remain in the ignored first-B0
diagnostics directory and are never passed to the generating agent.

## Frozen test and adoption rules

- Configuration: `data_link_policy=explicit_projection_v1`; `baseline` loads the
  original skill unchanged. Unknown settings fail before model dispatch. The
  configured skill and its hash are bound to each run and recorded per worker.
- Only the data-link instructions change. The model, temperature, evidence,
  retrieval/index, tool set, SQL drafting/correction, official EX evaluator and
  per-question budgets stay fixed. No SQL output trimming or answer postprocessing.
- Use the fixed 30-question smoke, then exactly two full runs of the same fixed
  300 questions. Match them chronologically to the two predeclared B0 runs. Do
  not select the best repeat, remove failures, or combine answers across runs.
- Compare mean official EX, gains/losses, per-database and difficulty results,
  paired uncertainty, genuine submission rate and engineering integrity. Positive
  net benefit is required for adoption; a gain below six questions is provisional.
- Check the mechanism in actual traces: support-only attributes should leave
  query targets without losing linked support columns or explicit outputs.
  Report regressions among previously correct questions as well as improvements.
- No structural extra model call is added, but prompt size, lookup phrases and
  reasoning may change. Measure actual calls, tokens/cache and latency. Monetary
  cost remains unknown until provider pricing is verified; no total cost cap was
  imposed by the user.

Main risks are omitted requested metrics, lost multi-part outputs, weakened
support-column retrieval and ambiguous question/Evidence/reference conventions.
Preserve the official metric and retain contradictory cases as diagnostics. A
smoke gain or the seven motivating examples alone cannot justify adoption.

## Implementation and execution

The candidate keeps the five baseline skill files byte-for-byte unchanged and
selects only the alternate data-link directory through an explicit instance
parameter. The worker records the selected policy and raw skill-file SHA256.
The snapshot includes both the policy loader and alternate instructions. It
also includes the supervisor-only recovery repair from `bc9418c`; this prevents
completed runs from being rescored and does not alter generation. Forty-nine
offline policy, batch, worker and supervisor checks passed after integration.

Run from the candidate checkout, using the existing main-checkout Python and
shared local services. Preserve the frozen candidate commit once generation starts.
The commands below run serially, only after the active B0 repeat has finished;
inspect smoke artifacts and engineering integrity before the full runs.

```powershell
& 'F:/data/VSCodeproject/AIDB-SQL/.venv/Scripts/python.exe' -m experiments.supervisor --config experiments/projection_roles.example.json --subset smoke --run-id smoke_C1_projection_roles_20260921_01
& 'F:/data/VSCodeproject/AIDB-SQL/.venv/Scripts/python.exe' -m experiments.supervisor --config experiments/projection_roles.example.json --run-id C1_projection_roles_20260921_01
& 'F:/data/VSCodeproject/AIDB-SQL/.venv/Scripts/python.exe' -m experiments.supervisor --config experiments/projection_roles.example.json --run-id C1_projection_roles_20260921_02
```

Audit and compare from the main checkout, where the generation-only audit,
research registry and chronological matched-repeat checks are maintained.
