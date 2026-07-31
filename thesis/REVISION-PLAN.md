# Thesis Revision Plan (WTF-P review cycle, 2026-07-31)

Produced from the three-layer review (citation / coherence / rubric) run over
the complete draft. Applied fixes are in git; this file tracks what remains
and who decides.

## verify-work — chapter acceptance against stated goals

| Ch | Stated goal | Verdict |
|---|---|---|
| 1 Introduction | Motivate the workload + gap, state insight, contributions, structure | **PASS** (preamble added; gap claims hedged; §1.2 compressed to defer survey to §2.6) |
| 2 Background | Concepts + SOTA establishing the gap, each section feeding the thesis | **PASS** (Camp-1 characterization made defensible; Datadog/OTel near-misses handled explicitly; "most sections close…" promise now accurate) |
| 3 Problem | Questions Q1–Q5, requirements R1–R7, non-goals | **PASS** (closing bridge to Ch. 4 added; thin at ~790 words — acceptable as a bridge chapter, fold into 2/4 only if page pressure demands) |
| 4 Design | Capture paths, mapping, delivery semantics, hot/cold split | **PASS** (schema table now referenced with self-contained caption) |
| 5 Analytics | The four analyses + operator surface with real screenshots | **PASS** (terminology standardized: scenario graph / cluster graph) |
| 6 Evaluation | Measure all six questions with distributions and caveats | **PASS** (CS1–CS3 rename resolves Q-namespace collision; closing recap + bridge added; regime gap 32% vs 0.3% explained) |
| 7 Conclusions | Requirement check, lessons, future work | **PASS** (preamble added; claims hedged; R-template varied) |

## polish-prose — completed

All 263 prose em-dashes (`---`) rewritten across the abstract and all seven
chapters: parenthetical asides to parentheses, appositions to commas/colons,
pivots to semicolons or new sentences, chosen per case. Preserved: two-char
`--` en-dashes in ranges, comment rules, math, `\texttt{}` content, and the
three standalone `---` not-applicable cells in `tab:doctor-quality`. Also
applied earlier in the cycle: long-sentence splits, "honest/honestly" tic
reduced 7 to 2, the six-fold "R\emph{n} is met by" template varied, formulaic
openers cut. Post-edit verification: braces, environments and math balanced
in all eight files; no broken `\ref`; no missing or orphan bibliography keys.

## Citation gaps — CLOSED (all verified against primary sources)

Seven entries added to `references.bib` (bibliography now 32 entries, all
cited, no orphans):

| Key | Source | Cited at |
|---|---|---|
| `cemri2025mast` | Cemri et al., *Why Do Multi-Agent LLM Systems Fail?*, arXiv 2503.13657, 2025 | ch1 failure modes; §2.5.3 opening |
| `kleppmann2017ddia` | Kleppmann, *Designing Data-Intensive Applications*, O'Reilly 2017 | §2.1.2 delivery semantics |
| `yang1988critpath` | Yang & Miller, ICDCS 1988, pp. 366–373 | §2.2.2 critical-path definition |
| `fidge1988timestamps` | Fidge, 11th ACSC 1988, pp. 56–66 | §2.1.1 alongside Lamport/Mattern |
| `bronson2021metastable` | Bronson et al., HotOS 2021, pp. 221–227 | §2.5.3 retry-storm sentence |
| `pham2026multiagenthpc` | Pham et al., arXiv 2604.07681, 2026 | ch1 cluster-deployment trend |
| `souza2025provenance` | Souza et al., SC '25 WORKS workshop | ch1 cluster-deployment trend |

**MSST venue string verified correct as written** ("Proceedings of the 36th
International Conference on Massive Storage Systems and Technology (MSST
20)"), confirmed against the MSST conference proceedings listing.

## Open items — decisions for Anna

1. **Page budget.** ~55–58 pages estimated vs a 30–40 target. Anna has
   decided not to modify this for now; revisit only if the faculty enforces
   a hard cap. If a cut becomes necessary, the donors are §2.1–2.3 (the
   distributed-systems primer) and §6.6's narrative walk, not the measured
   results.

## Standing pipeline items (not review findings)

- Compile on Overleaf (nothing has been through pdflatex; two pgfplots
  figures + five PNGs + float placement to eyeball).
- Institutional template: cover page, declaration, possibly second-language
  abstract.
- Acknowledgements (`main.tex` TODO).
- Commit everything (working tree holds eval + UI fixes + thesis, all
  uncommitted).
- Advisor pass after compile.
