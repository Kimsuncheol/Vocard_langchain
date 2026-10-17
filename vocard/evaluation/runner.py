import json
import platform
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from vocard.answering import SYSTEM_PROMPT, AnsweringPipeline
from vocard.evaluation.dataset import digest, load_dataset, sha256
from vocard.evaluation.metrics import citation_provenance, recall_at_k, summarize
from vocard.evaluation.runtime import ModelAdapter, settings_for


def write_json(path, value):
    with Path(path).open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class FrozenRetrieval:
    """The exact same ranked evidence is reused across candidates, never across questions."""

    def __init__(self, corpus, docs, case, k):
        self.repository, self.book_id = corpus, corpus.book_id
        self.docs, self.case, self.k = docs, case, k

    def invoke(self, query):
        documents = [d.model_copy(deep=True) for d in self.docs[: self.k]]
        if self.case.attack:
            for doc in documents:
                doc.page_content += "\n\n" + self.case.attack.payload
        return documents


def run(
    dataset,
    split,
    config,
    config_path,
    corpus,
    settings,
    output,
    selection=None,
    adapter_factory=ModelAdapter,
):
    manifest, cases = load_dataset(dataset, split)
    protocol = {
        "config_sha256": sha256(config_path),
        "resolved_config_sha256": digest(config.model_dump()),
        "system_prompt_sha256": digest(SYSTEM_PROMPT),
        "dataset_manifest_sha256": sha256(Path(dataset) / "manifest.json"),
        "corpus": corpus.identity,
    }
    if split == "heldout":
        if selection is None or any(selection.get(k) != v for k, v in protocol.items()):
            raise ValueError("Heldout requires a selection lock from a matching tuning run")
        tuning_path = selection.get("tuning_run_path")
        if not tuning_path:
            raise ValueError("Heldout lock must reference the completed tuning run")
        tuning_manifest, _ = load_run(tuning_path)
        tuning_protocol = {
            **tuning_manifest["protocol"],
            "resolved_config_sha256": digest(tuning_manifest["config"]),
        }
        if (
            tuning_manifest["split"] != "tuning"
            or tuning_protocol != protocol
            or tuning_manifest["run_id"] != selection.get("tuning_run_id")
            or sha256(Path(tuning_path) / "predictions.jsonl")
            != selection.get("tuning_predictions_sha256")
        ):
            raise ValueError("Heldout tuning-run proof does not match")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    run_manifest = {
        "run_id": str(uuid.uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
        "split": split,
        "dataset_id": manifest["dataset_id"],
        "label_status": manifest["label_status"],
        "protocol": protocol,
        "config": config.model_dump(),
        "python": platform.python_version(),
        "case_ids": [c.case_id for c in cases],
        "history_policy": "identical fixed histories; last two user turns expand query",
        "timing_policy": "shared retrieval once per case; rotating candidate order",
        "cost_policy": "full query retrieval + generation per question, no amortization; "
        "index build, human/model judging, local compute and hosting excluded",
    }
    write_json(output / "manifest.json", run_manifest)
    rows = []
    max_k = max(*config.ks, config.generation_k)
    with (output / "predictions.jsonl").open("x", buffering=1) as stream:
        for case_index, case in enumerate(cases):
            active = corpus.active_index(corpus.book_id)["index_version"]
            if active != corpus.index_version:
                raise ValueError("Index changed during evaluation; discard this incomplete run")
            start = time.perf_counter()
            query = "\n".join(
                [h.content for h in case.history if h.role == "user"][-2:] + [case.question]
            )
            retrieval_error = None
            try:
                docs = corpus.retrieve(case, query, max_k)
            except Exception as exc:
                docs, retrieval_error = [], type(exc).__name__
            retrieval_ms = (time.perf_counter() - start) * 1000
            original = [
                {"id": d.id, "page_content": d.page_content, "metadata": d.metadata} for d in docs
            ]
            ledger = getattr(corpus, "ledger", [])
            retrieval_cost = (
                0.0
                if corpus.identity["mode"] == "offline_ocr_lexical"
                else (
                    sum(ledger) * config.embedding_input_usd_per_million / 1_000_000
                    if not retrieval_error
                    and ledger
                    and all(t is not None for t in ledger)
                    and config.embedding_input_usd_per_million is not None
                    else None
                )
            )
            # Selection never receives gold pages, rubrics, tags or answerability labels.
            offset = case_index % len(config.candidates)
            order = config.candidates[offset:] + config.candidates[:offset]
            for candidate in order:
                adapter = adapter_factory(candidate, config)
                start = time.perf_counter()
                response, error = None, retrieval_error
                error_chain = []
                if error is None:
                    try:
                        response = (
                            AnsweringPipeline(
                                FrozenRetrieval(corpus, docs, case, config.generation_k),
                                settings_for(settings, config, candidate),
                                lambda _, model_adapter=adapter: model_adapter,
                            )
                            .invoke(case.question, history=case.history, retrieval_query=query)
                            .model_dump()
                        )
                    except Exception as exc:
                        error = type(exc).__name__
                        current = exc
                        while current is not None and len(error_chain) < 5:
                            error_chain.append(
                                {
                                    "type": type(current).__name__,
                                    "status_code": getattr(current, "status_code", None),
                                }
                            )
                            current = current.__cause__
                generation_ms = (time.perf_counter() - start) * 1000
                # A call skipped for no evidence costs zero. Failed calls with no usage are unknown.
                generation_cost = (
                    adapter.cost()
                    if adapter.usage is not None
                    else (0.0 if response is not None else None)
                )
                cost = (
                    retrieval_cost + generation_cost
                    if (retrieval_cost is not None and generation_cost is not None)
                    else None
                )
                row = {
                    "run_id": run_manifest["run_id"],
                    "case_id": case.case_id,
                    "candidate_id": candidate.candidate_id,
                    "candidate_model": candidate.model,
                    "answerability": case.answerability,
                    "tags": case.tags,
                    "status": response["status"] if response else "error",
                    "error": error,
                    "error_chain": error_chain,
                    "raw_model_output": adapter.raw_output,
                    "response": response,
                    "retrieved_documents": original,
                    "generation_context_ids": adapter.context_ids,
                    "page_recall_at_k": {
                        str(k): recall_at_k(case.expected_source_pages, original, k)
                        for k in config.ks
                    },
                    "citation_provenance": citation_provenance(response, original),
                    "retrieval_latency_ms": retrieval_ms,
                    "generation_latency_ms": generation_ms,
                    "latency_ms": retrieval_ms + generation_ms,
                    "usage": adapter.usage,
                    "generation_cost_usd": generation_cost,
                    "retrieval_cost_usd": retrieval_cost,
                    "cost_usd": cost,
                    "synthetic_context_attack": bool(case.attack),
                }
                row["prediction_sha256"] = digest(row)
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                rows.append(row)
                print(f"{case.case_id} {candidate.candidate_id}: {row['status']}", flush=True)
            if corpus.active_index(corpus.book_id)["index_version"] != corpus.index_version:
                raise ValueError("Index changed during evaluation; discard this incomplete run")
    report = {
        "run_id": run_manifest["run_id"],
        "complete": True,
        "split": split,
        "label_status": manifest["label_status"],
        "retrieval_mode": corpus.identity["mode"],
        "semantic_grading": "not_graded",
        "candidates": {
            c.candidate_id: summarize(
                [r for r in rows if r["candidate_id"] == c.candidate_id], config.ks
            )
            for c in config.candidates
        },
    }
    write_json(output / "automatic_metrics.json", report)
    write_json(
        output / "complete.json",
        {
            "predictions_sha256": sha256(output / "predictions.jsonl"),
            "manifest_sha256": sha256(output / "manifest.json"),
        },
    )
    return report


def load_run(path):
    path = Path(path)
    complete = json.loads((path / "complete.json").read_text())
    for filename, key in (
        ("predictions.jsonl", "predictions_sha256"),
        ("manifest.json", "manifest_sha256"),
    ):
        if sha256(path / filename) != complete[key]:
            raise ValueError("Evaluation run changed or is incomplete")
    manifest = json.loads((path / "manifest.json").read_text())
    rows = [json.loads(line) for line in (path / "predictions.jsonl").read_text().splitlines()]
    expected = {
        (case, c["candidate_id"])
        for case in manifest["case_ids"]
        for c in manifest["config"]["candidates"]
    }
    if len(rows) != len(expected) or {(r["case_id"], r["candidate_id"]) for r in rows} != expected:
        raise ValueError("Candidates did not answer identical complete case sets")
    for row in rows:
        if row["run_id"] != manifest["run_id"] or row["prediction_sha256"] != digest(
            {k: v for k, v in row.items() if k != "prediction_sha256"}
        ):
            raise ValueError("Prediction identity changed")
    return manifest, rows
