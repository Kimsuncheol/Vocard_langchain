# Fixed book Q&A evaluation

`v1/` contains 40 fixed cases: 20 tuning and 20 held-out. Each includes Korean
questions, English examples where relevant, course/unit/lesson IDs, expected
**1-based PDF page indexes**, answerability, category tags, weighted rubrics and
label provenance. Printed page labels are separate; e.g. PDF 118 has printed
label 122. The source PDF has 425 pages and SHA-256
`514a96147830253fdf551d42c67f6adb865d3a0475ef85b863b49a77dd167f91`.

**All labels/rubrics are agent-authored drafts pending human review.** The OCR is
unreviewed too. PDF pages 24, 73, 118, 128 and 260 were visually spot-checked by
the agent; no full human validation or human grading is claimed. A grammar
reviewer must check all targets/rubrics before treating these as release gates.

## Split policy

Tuning lessons: 3, 12, 15, 26, 30. Held-out lessons: 8, 20, 22, 28, 60.
Related questions, follow-ups, incorrect examples and attack variants stay in one
lesson group. Lesson IDs, case IDs, questions and expected page targets do not
overlap. Each split includes Korean, English examples, follow-ups, incorrect
examples, OCR corruption, unanswerable questions, clarification and prompt
injection. Each has 17 answerable cases, two unanswerable cases and one requiring
clarification. This small set is not prevalence-representative.

`manifest.json` pins split file hashes, source PDF hash, reference OCR hash and
curriculum asset hashes. Corrections after human review require a new dataset
version, not a silent replacement. Inference loads only the selected split.
Structural tests may inspect both splits but do not tune candidates on held-out
answers. Keep held-out files away from prompt optimizers and routine tuning.

Follow-ups use identical fixed histories for every candidate, not earlier
candidate-generated answers. The last two user turns expand the retrieval query,
matching the student API policy. Each question retrieves again. The generator
never receives gold pages, answerability, rubric or tags. Synthetic retrieved-text
injections are appended to in-memory copies; source storage and indexes remain
untouched. Provenance is checked against original, unmodified chunks.

## Run

From the repository root:

```sh
.venv/bin/python -m vocard.evaluation validate
# Offline measurement smoke test, not a semantic quality benchmark:
.venv/bin/python -m vocard.evaluation run \
  --config evaluation/baselines.json --output output/evaluation/baseline-tuning
# Actual local generation using installed Ollama models:
.venv/bin/python -m vocard.evaluation run \
  --config evaluation/local-models.json --output output/evaluation/local-tuning
# Actual private active index; requires API/worker settings and credentials:
.venv/bin/python -m vocard.evaluation run --mode production --book-id devils-100 \
  --config evaluation/openai-models.example.json --output output/evaluation/production-tuning
```

Default `reference` mode loads **all 425 OCR pages**, not just gold pages, and
applies the production grammar chunker within lesson boundaries. Frozen app
assets supply lesson metadata after chunking because OCR headings can conflict
with those assets. This is an explicit **unreviewed OCR lexical reference
benchmark**, not production embedding retrieval or a reviewed index. It never
publishes a book or bypasses ingestion review. Korean word/bigram tokens with
BM25-style scores provide the reference ranking; production cosine thresholds
are not applied to lexical scores. `--ocr` and `--curriculum` locate the same
frozen files on another machine.

Production mode uses `ActiveBookRetriever`, the active index's embeddings and
server-validated book/source metadata. Index changes invalidate the run. Each
case retrieves once to the largest requested k, then all candidates receive the
same ranking and generation budget. Execution order rotates by case. Run
manifests record configs, prompt hash, dataset identity, corpus/index identity
and history/timing/cost policy. Use pinned model snapshots; retain installed
Ollama digests and hardware details. Local latency includes load/cache effects.
Temperature zero does not guarantee bit-for-bit determinism.

The OpenAI example compares the existing GPT-4.1 family; this is not a deployment
recommendation. Those models may be deprecated/unavailable to an account. Price
snapshots were checked on 2026-10-17 against official
[mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini),
[GPT-4.1](https://developers.openai.com/api/docs/models/gpt-4.1) and
[embedding](https://developers.openai.com/api/docs/models/text-embedding-3-small)
pages. Update for your provider/tier/billing terms. Missing rates or usage yield
`null`, never an invented free cloud answer.

## Held-out evaluation

Tune models, prompts, chunk sizes and retrieval settings on tuning only. Freeze
a completed tuning run before evaluating the held-out set:

```sh
.venv/bin/python -m vocard.evaluation freeze \
  --tuning-run output/evaluation/local-tuning --output output/evaluation/selection.json
.venv/bin/python -m vocard.evaluation run --split heldout \
  --selection output/evaluation/selection.json --config evaluation/local-models.json \
  --output output/evaluation/heldout-final
```

Missing/mismatched config, prompt, dataset or corpus locks fail before inference.
Do not tune on final failures and call another held-out run independent: retire
that holdout and version a new one. The lock is a reproducibility boundary, not
access control over repository files. Unit tests exercise the gate with explicit
test doubles; that is not a live held-out model evaluation.

## Metrics

| Metric | Definition and denominator |
| --- | --- |
| Page recall@k | Required PDF pages covered by first k chunks, macro averaged over answerable cases. Duplicate pages count once. Non-answerable cases are excluded. This is page recall, not relevant-chunk recall. |
| Answer correctness | Weighted rubric points (0, 0.5, 1), gated to zero when the returned status differs from the expected status. Raw rubric score is also shown, separately per human reviewer/model grader with coverage. No word-match heuristic is called semantic correctness. |
| Citation provenance | Retrieved ID, exact quote in original text, matching book/version/index/pages. Empty citations are unscored, with coverage. This does not prove semantic support. |
| Citation support | Human/model assessment that the cited original source supports the associated claims. Report scored citation counts, support rate and unsupported-claim rate. Empty citations do not receive perfect support. |
| Abstention | Precision/recall for declining non-answerable requests, false abstention on answerable questions, exact expected-status accuracy and confusion table. Clarification and insufficient evidence are distinct. Errors are not good abstentions. |
| Latency | Observed retrieval + generation/validation wall time, mean/p50/p95; retrieval separately. Each candidate is charged the shared retrieval time for a comparable per-question measure. Failed calls remain included. |
| Cost | Provider-reported input/cached/output tokens at frozen rates + production query embeddings. Per-question mean includes abstentions/errors; per-answered cost divides all question costs by answered count. Unknown components make aggregates unknown; cost coverage is shown. |

Shared query retrieval is attributed in full to each candidate's hypothetical
standalone request, not amortized. Do not sum those embedding costs as a shared
run's actual invoice. Index building, storage/hosting, local hardware/electricity,
agent work and human review are excluded. Ollama API charges are zero, but local
compute is not free. Model-grading cost is separate from serving cost. Evaluation
provider retries are disabled; failed calls without usage may have unknown cost.

Reports include category slices. Correctness comparisons pair identical case IDs
and bootstrap by lesson group (fixed seed, 95% interval) to account for related
follow-ups. Five lesson groups per split give unstable uncertainty estimates;
small differences are not sufficient for automatic production promotion.

## Human versus model grading

```sh
.venv/bin/python -m vocard.evaluation review-packet \
  --run output/evaluation/local-tuning --output output/evaluation/review.json
```

A human checks PDF/source pages and fills `annotation_template`: every criterion
score/reason, every citation support/reason, `unsupported_claims` and notes. Set
completed items to `review_status: reviewed`, `grading_type: human` and the actual
`human_reviewer` ID. Incomplete items stay `ungraded` with null scores. Then:

```sh
.venv/bin/python -m vocard.evaluation import-human \
  --run output/evaluation/local-tuning --packet output/evaluation/review.json \
  --reviewer reviewer-1 --output output/evaluation/human-grades.jsonl
```

This command is for actual human annotations. Model-filled packets and agent
reviews are model judgments, never human judgments. Export creates no grades.
For optional model grading, use a separate grader model/config:

```sh
.venv/bin/python -m vocard.evaluation grade-model \
  --run output/evaluation/local-tuning --config evaluation/local-grader.json \
  --grader independent_judge --output output/evaluation/model-grades.jsonl
.venv/bin/python -m vocard.evaluation report --run output/evaluation/local-tuning \
  --grades output/evaluation/model-grades.jsonl output/evaluation/human-grades.jsonl \
  --output output/evaluation/report.json
```

Omit unavailable grade files. No human grades means no human correctness score.
Reports never pool human/model scores. Every grade identifies type, reviewer or
model/prompt, timestamp and exact prediction hash. Unknown criterion/chunk IDs,
duplicate reviews and grades for different outputs fail. Candidate model identity
is withheld from model grader prompts. Rubrics, original pages/chunks and answers
are data, not instructions. Grader errors stay visible and ungraded. Self-grading
is counted; human/model disagreement is computed only on overlapping annotations.

## Artifacts and tests

Runs write `manifest.json`, append-only `predictions.jsonl`,
`automatic_metrics.json`, then final `complete.json` hashes. Incomplete/changed
runs cannot be reported or frozen. Output files use exclusive creation. Raw runs
include book excerpts/questions; directories are private and `output/evaluation/`
is git-ignored. Keep them private when sharing only benchmark summaries.

```sh
.venv/bin/python -m pytest tests/test_evaluation.py -q
.venv/bin/ruff check vocard tests
```

Tests cover fixed splits/categories, hashes/leakage, fractional recall, original
quote/page provenance, unknown cost, error-aware abstention, identical questions,
gold isolation, synthetic attacks, held-out locks, grading identity/provenance,
missing human grades, incomplete runs and grouped paired confidence intervals.

## Initial measured tuning run: 2026-10-17

The [machine-readable summary](results/tuning-2026-10-17.json) records actual local
generation on the same 20 tuning questions. Reference retrieval used all 425 OCR
pages and 632 chunks. Page recall@1 was 85.3%; recall@3 and @5 were 100% across the
17 answerable cases. These are lexical reference-corpus results, not production
embedding retrieval results.

| Candidate | Validation failures / 20 | Agent/model-graded correctness | Supported cited chunks | Mean latency |
| --- | --- | --- | --- | --- |
| qwen2.5:3b | 16 | 13.75% | 1 / 2 | 14.62 s |
| qwen3:4b | 17 | 15.00% | 1 / 1 | 23.39 s |

Both used 600 output tokens, a 5000-character serialized context budget and
generation k=3. Most responses failed the actual answer schema/citation validator.
Those failures remain in the denominator. The paired correctness difference was
1.25 percentage points, with a lesson-clustered 95% bootstrap interval of
[-21.25, 20.00] points. This does not establish a preferred model.

Abstention precision/recall were 50%/33.3% for Qwen2.5 and 100%/66.7% for Qwen3.
Citation support has only two/one scored chunks respectively; do not read these
percentages as broad evidence of grounding quality. Every served citation passed
the deterministic provenance check, yet one Qwen2.5 answer's additional explanation
was judged not fully supported by its cited source. This illustrates why provenance
and semantic support are reported separately.

Grades are **unblinded agent/model judgments by the same agent that authored the
draft rubrics**, not human or independent model review. Exact agent build, full
grading context and grading token cost are unavailable. There are zero human
grades. An unfilled source-backed human review packet is available in the private
run directory. The held-out set has had zero live candidate inference runs.

Local Ollama provider API charge per question/answered response was $0; hardware,
electricity, hosting and agent grading are excluded. Model digests were unchanged
between the retained pre-run and post-run inventories. OpenAI candidates returned
no accepted responses; a follow-up diagnostic returned HTTP 429. Cloud cost is
unknown because no usage was returned. The slow first local pilot was interrupted
and retained as incomplete; it is excluded from this complete paired comparison.
