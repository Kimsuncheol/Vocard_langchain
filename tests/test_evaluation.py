import json
import shutil
from pathlib import Path

import pytest
from langchain_core.documents import Document

from vocard.evaluation.dataset import DEFAULT_DATASET, digest, load_dataset, sha256, validate_splits
from vocard.evaluation.grading import (
    JUDGE_PROMPT,
    Grade,
    export_review,
    import_human,
    report,
    validate_grade,
)
from vocard.evaluation.metrics import citation_provenance, paired_bootstrap, recall_at_k, summarize
from vocard.evaluation.runner import load_run, run
from vocard.evaluation.runtime import (
    Candidate,
    MeteredResource,
    ModelAdapter,
    RunConfig,
    usage_cost,
)


def test_fixed_set_has_complete_disjoint_lessons_and_categories():
    assert validate_splits() == {
        "dataset_id": "vocard-devils100-qa-v1",
        "tuning": 20,
        "heldout": 20,
        "label_status": "agent_authored_pending_human_review",
    }
    for split in ("tuning", "heldout"):
        _, cases = load_dataset(split=split)
        assert sum(c.answerability == "answerable" for c in cases) == 17
        assert all(c.label_provenance.human_reviewer is None for c in cases)
        assert all(c.label_provenance.author_type == "agent" for c in cases)


def test_dataset_tampering_and_group_leakage_rejected(tmp_path):
    target = tmp_path / "dataset"
    shutil.copytree(DEFAULT_DATASET, target)
    source = target / "heldout.jsonl"
    source.write_text(source.read_text().replace('"lesson-008"', '"lesson-003"'))
    with pytest.raises(ValueError, match="Frozen dataset"):
        load_dataset(target, "heldout")
    manifest = json.loads((target / "manifest.json").read_text())
    manifest["files"]["heldout.jsonl"] = sha256(source)
    (target / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Split leakage"):
        validate_splits(target)


def test_page_recall_is_fractional_not_hit_rate_and_deduplicates_pages():
    docs = [{"metadata": {"source_pages": [1, 1]}}, {"metadata": {"source_pages": [2, 3]}}]
    assert recall_at_k([1, 2], docs, 1) == 0.5
    assert recall_at_k([1, 2], docs, 2) == 1
    assert recall_at_k([], docs, 2) is None


def test_citation_provenance_checks_original_text_identity_and_page_metadata():
    meta = {"book_id": "b", "version": 1, "index_version": "i", "source_pages": [24]}
    docs = [{"id": "c", "page_content": "원본 설명", "metadata": meta}]
    ref = {"chunk_id": "c", "quote": "원본", **meta}
    assert citation_provenance({"citations": [ref]}, docs) == 1
    for change in ({"chunk_id": "fake"}, {"quote": "injected"}, {"source_pages": [999]}):
        assert citation_provenance({"citations": [{**ref, **change}]}, docs) == 0
    assert citation_provenance({"citations": []}, docs) is None


def test_unknown_cost_and_errors_do_not_look_like_free_answers_or_good_abstention():
    candidate = Candidate(
        candidate_id="m",
        provider="openai",
        model="model",
        input_usd_per_million=2,
        cached_input_usd_per_million=0.5,
        output_usd_per_million=8,
    )
    assert usage_cost(
        {"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 200}, candidate
    ) == pytest.approx(0.0025)
    assert usage_cost(None, candidate) is None
    no_rate = candidate.model_copy(update={"output_usd_per_million": None})
    assert usage_cost({"input_tokens": 100, "output_tokens": 10}, no_rate) is None
    rows = [
        {
            "answerability": label,
            "status": status,
            "cost_usd": cost,
            "page_recall_at_k": {"1": recall},
            "citation_provenance": None,
            "latency_ms": 10,
            "retrieval_latency_ms": 2,
        }
        for label, status, cost, recall in (
            ("answerable", "insufficient_evidence", 0, 0.5),
            ("unanswerable", "error", None, None),
            ("needs_clarification", "clarification_needed", 0, None),
        )
    ]
    metrics = summarize(rows, [1])
    assert metrics["abstention_precision"] == 0.5 and metrics["abstention_recall"] == 0.5
    assert metrics["status_accuracy"] == pytest.approx(1 / 3)
    assert metrics["cost_usd_per_question"] is None and metrics["cost_coverage"] == pytest.approx(
        2 / 3
    )


class TestCorpus:
    __test__ = False
    book_id = "test-book"
    index_version = "test-index"
    identity = {"mode": "offline_ocr_lexical", "test_only": True}

    def active_index(self, book_id):
        return {"index_version": self.index_version}

    def retrieve(self, case, query, k):
        return [
            Document(
                id=case.case_id,
                page_content="고정된 테스트 교재 설명",
                metadata={
                    "book_id": self.book_id,
                    "version": 1,
                    "index_version": self.index_version,
                    "source_pages": case.expected_source_pages or [1],
                    "course_id": case.course_id,
                    "unit_id": case.unit_id,
                    "lesson_number": case.lesson_number,
                },
            )
        ]


@pytest.fixture
def evaluation_run(system, tmp_path):
    config_path = Path("evaluation/baselines.json")
    config = RunConfig.model_validate_json(config_path.read_text())
    output = tmp_path / "run"
    run(DEFAULT_DATASET, "tuning", config, config_path, TestCorpus(), system["settings"], output)
    return output, config, config_path


def test_identical_questions_fixed_history_and_sealed_heldout(evaluation_run, system, tmp_path):
    path, config, config_path = evaluation_run
    manifest, rows = load_run(path)
    assert len(rows) == 40
    assert all(
        r["citation_provenance"] == (0 if r["synthetic_context_attack"] else 1)
        for r in rows
        if r["candidate_id"] == "extractive"
    )
    assert {r["case_id"] for r in rows if r["candidate_id"] == "extractive"} == {
        r["case_id"] for r in rows if r["candidate_id"] == "always_abstain"
    }
    with pytest.raises(ValueError, match="selection lock"):
        run(
            DEFAULT_DATASET,
            "heldout",
            config,
            config_path,
            TestCorpus(),
            system["settings"],
            tmp_path / "blocked",
        )
    assert not (tmp_path / "blocked").exists()
    lock = {
        **manifest["protocol"],
        "tuning_run_id": manifest["run_id"],
        "tuning_run_path": str(path),
        "tuning_predictions_sha256": sha256(path / "predictions.jsonl"),
    }
    run(
        DEFAULT_DATASET,
        "heldout",
        config,
        config_path,
        TestCorpus(),
        system["settings"],
        tmp_path / "test-heldout",
        selection=lock,
    )
    changed = {**lock, "config_sha256": "changed"}
    with pytest.raises(ValueError):
        run(
            DEFAULT_DATASET,
            "heldout",
            config,
            config_path,
            TestCorpus(),
            system["settings"],
            tmp_path / "changed",
            selection=changed,
        )


def test_model_never_receives_gold_labels_and_attacks_are_in_memory(system, tmp_path):
    observed = []

    class InspectingAdapter(ModelAdapter):
        def invoke(self, messages):
            envelope = json.loads(messages[-1].content)
            assert "rubric" not in envelope and "expected_source_pages" not in envelope
            assert "answerability" not in envelope
            observed.append(envelope)
            return super().invoke(messages)

    config_path = Path("evaluation/baselines.json")
    config = RunConfig.model_validate_json(config_path.read_text())
    run(
        DEFAULT_DATASET,
        "tuning",
        config,
        config_path,
        TestCorpus(),
        system["settings"],
        tmp_path / "inspect",
        adapter_factory=InspectingAdapter,
    )
    assert any("SYSTEM OVERRIDE" in e["evidence"][0]["text"] for e in observed)
    _, rows = load_run(tmp_path / "inspect")
    assert all(
        "SYSTEM OVERRIDE" not in d["page_content"] for r in rows for d in r["retrieved_documents"]
    )


def make_grade(row, case, kind="human"):
    return Grade(
        **{k: row[k] for k in ("run_id", "case_id", "candidate_id", "prediction_sha256")},
        grading_type=kind,
        grader_id="test-human" if kind == "human" else "model:judge",
        graded_at="2026-10-17T00:00:00Z",
        grader_model=None if kind == "human" else "judge",
        grader_prompt_sha256=None if kind == "human" else digest(JUDGE_PROMPT),
        usage=None,
        cost_usd=None,
        criteria=[
            {"criterion_id": c.criterion_id, "score": 1, "reason": "Test annotation"}
            for c in case.rubric
        ],
        citation_support=[
            {"chunk_id": c, "supported": True, "reason": "Test annotation"}
            for c in {ref["chunk_id"] for ref in row["response"]["citations"]}
        ],
        unsupported_claims=False,
        notes="TEST ONLY, not an actual human or model judgment",
    )


def test_human_model_grades_are_distinct_bound_to_outputs_and_not_fabricated(
    evaluation_run, tmp_path
):
    path, _, _ = evaluation_run
    _, rows = load_run(path)
    _, cases = load_dataset()
    case = cases[0]
    row = rows[0]
    human, model = make_grade(row, case), make_grade(row, case, "model")
    validate_grade(human, case, row)
    with pytest.raises(ValueError, match="human grades"):
        validate_grade(model.model_copy(update={"grading_type": "human"}), case, row)
    with pytest.raises(ValueError, match="different prediction"):
        validate_grade(human.model_copy(update={"prediction_sha256": "other"}), case, row)
    with pytest.raises(ValueError, match="criterion"):
        validate_grade(human.model_copy(update={"criteria": []}), case, row)
    grades = tmp_path / "test-grades.jsonl"
    grades.write_text(human.model_dump_json() + "\n" + model.model_dump_json() + "\n")
    result = report(path, DEFAULT_DATASET, [grades], tmp_path / "graded.json")
    assert {g["grading_type"] for g in result["semantic_grades"]} == {"human", "model"}
    assert result["human_model_agreement"][0]["n"] == 1
    ungraded = report(path, DEFAULT_DATASET, [], tmp_path / "ungraded.json")
    assert ungraded["semantic_grades"] == []
    packet = tmp_path / "packet.json"
    pages = {n: {"text": "test reference"} for c in cases for n in c.expected_source_pages}
    export_review(path, DEFAULT_DATASET, pages, packet)
    item = json.loads(packet.read_text())["items"][0]
    assert item["review_status"] == "ungraded" and item["human_reviewer"] is None
    assert item["annotation_template"]["criteria"][0]["score"] is None


def test_incomplete_or_modified_runs_cannot_be_reported(evaluation_run):
    path, _, _ = evaluation_run
    predictions = path / "predictions.jsonl"
    predictions.write_text(predictions.read_text().splitlines()[0] + "\n")
    with pytest.raises(ValueError, match="changed or is incomplete"):
        load_run(path)


def test_human_import_never_invents_review_or_accepts_model_annotations(evaluation_run, tmp_path):
    path, _, _ = evaluation_run
    _, rows = load_run(path)
    _, cases = load_dataset()
    pages = {n: {"text": "test source"} for c in cases for n in c.expected_source_pages}
    packet_path = tmp_path / "review.json"
    export_review(path, DEFAULT_DATASET, pages, packet_path)
    with pytest.raises(ValueError, match="No completed human reviews"):
        import_human(path, DEFAULT_DATASET, packet_path, "reviewer", tmp_path / "none.jsonl")
    packet = json.loads(packet_path.read_text())
    item = packet["items"][0]
    annotation = make_grade(rows[0], cases[0])
    item["annotation_template"] = {
        key: annotation.model_dump()[key]
        for key in ("criteria", "citation_support", "unsupported_claims", "notes")
    }
    item.update(review_status="reviewed", grading_type="model", human_reviewer="reviewer")
    packet_path.write_text(json.dumps(packet))
    with pytest.raises(ValueError, match="explicitly identify"):
        import_human(path, DEFAULT_DATASET, packet_path, "reviewer", tmp_path / "invalid.jsonl")
    item["grading_type"] = "human"
    packet_path.write_text(json.dumps(packet))
    target = tmp_path / "human.jsonl"
    import_human(path, DEFAULT_DATASET, packet_path, "reviewer", target)
    grade = Grade.model_validate_json(target.read_text().strip())
    assert grade.grading_type == "human" and grade.grader_model is None
    assert grade.grader_id == "reviewer"


def test_paired_bootstrap_matches_ids_and_clusters_related_cases():
    first = {"a": 0, "b": 0.5, "c": 1, "ungraded": None}
    second = {"b": 1, "a": 0.5, "c": 1, "ungraded": 1, "extra": 1}
    groups = {"a": "lesson1", "b": "lesson1", "c": "lesson2", "ungraded": "lesson3"}
    result = paired_bootstrap(first, second, groups=groups)
    assert result["n_pairs"] == 3 and result["n_groups"] == 2
    assert result["mean_delta"] == pytest.approx(1 / 3)
    assert result == paired_bootstrap(first, second, groups=groups)


def test_embedding_usage_is_metered_and_missing_usage_is_unknown():
    class Resource:
        def create(self, **kwargs):
            return {"usage": kwargs.get("usage"), "data": []}

    ledger = []
    resource = MeteredResource(Resource(), ledger)
    resource.create(usage={"total_tokens": 10})
    resource.create(usage=None)
    resource.create(usage={"total_tokens": -1})
    assert ledger == [10, None, None]
