"""Deterministic metrics. Provenance checks are deliberately not semantic grades."""

import math
import random
from collections import Counter
from statistics import mean


def average(values):
    values = [v for v in values if v is not None]
    return mean(values) if values else None


def recall_at_k(required_pages, documents, k):
    if not required_pages:
        return None
    found = {p for d in documents[:k] for p in d["metadata"]["source_pages"]}
    return len(set(required_pages) & found) / len(set(required_pages))


def citation_provenance(response, documents):
    if not response or not response["citations"]:
        return None
    allowed = {d["id"]: d for d in documents}

    def supported(ref):
        doc = allowed.get(ref["chunk_id"])
        return bool(
            doc
            and ref["quote"].strip()
            and ref["quote"] in doc["page_content"]
            and all(
                ref[k] == doc["metadata"][k]
                for k in ("book_id", "version", "index_version", "source_pages")
            )
        )

    return mean(supported(ref) for ref in response["citations"])


def expected_status(answerability):
    return {
        "answerable": "answered",
        "unanswerable": "insufficient_evidence",
        "needs_clarification": "clarification_needed",
    }[answerability]


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def summarize(rows, ks):
    expected_negative = sum(r["answerability"] != "answerable" for r in rows)
    answered_gold = sum(r["answerability"] == "answerable" for r in rows)
    abstained = [
        r for r in rows if r["status"] in ("insufficient_evidence", "clarification_needed")
    ]
    true_abstention = sum(r["answerability"] != "answerable" for r in abstained)
    costs = [r["cost_usd"] for r in rows]
    return {
        "n": len(rows),
        "errors": sum(r["status"] == "error" for r in rows),
        "page_recall_at_k": {
            str(k): average([r["page_recall_at_k"][str(k)] for r in rows]) for k in ks
        },
        "retrieval_scored_cases": answered_gold,
        "citation_provenance": average([r["citation_provenance"] for r in rows]),
        "citation_provenance_coverage": sum(r["citation_provenance"] is not None for r in rows),
        "status_accuracy": average(
            [r["status"] == expected_status(r["answerability"]) for r in rows]
        ),
        "abstention_precision": true_abstention / len(abstained) if abstained else None,
        "abstention_recall": true_abstention / expected_negative if expected_negative else None,
        "false_abstention_rate": sum(r["answerability"] == "answerable" for r in abstained)
        / answered_gold
        if answered_gold
        else None,
        "status_confusion": dict(
            Counter(expected_status(r["answerability"]) + " -> " + r["status"] for r in rows)
        ),
        "latency_ms": {
            "mean": average([r["latency_ms"] for r in rows]),
            "p50": percentile([r["latency_ms"] for r in rows], 0.5),
            "p95": percentile([r["latency_ms"] for r in rows], 0.95),
        },
        "retrieval_latency_ms": average([r["retrieval_latency_ms"] for r in rows]),
        "cost_usd_per_question": average(costs) if all(c is not None for c in costs) else None,
        "cost_coverage": sum(c is not None for c in costs) / len(costs) if costs else 0,
        "total_known_cost_usd": sum(c for c in costs if c is not None),
        "cost_usd_per_answered": sum(costs) / sum(r["status"] == "answered" for r in rows)
        if all(c is not None for c in costs) and any(r["status"] == "answered" for r in rows)
        else None,
    }


def paired_bootstrap(a, b, seed=20261017, repetitions=2000, groups=None):
    """b - a, paired by identical case ID. Missing grades never become zero."""
    keys = sorted(a.keys() & b.keys())
    keys = [k for k in keys if a[k] is not None and b[k] is not None]
    differences = [b[k] - a[k] for k in keys]
    if not differences:
        return {"n_pairs": 0, "mean_delta": None, "ci95": None}
    clusters = {}
    for key in keys:
        clusters.setdefault(groups[key] if groups else key, []).append(b[key] - a[key])
    values = list(clusters.values())
    rng = random.Random(seed)
    samples = sorted(
        mean([v for cluster in rng.choices(values, k=len(values)) for v in cluster])
        for _ in range(repetitions)
    )
    return {
        "n_pairs": len(differences),
        "n_groups": len(values),
        "mean_delta": mean(differences),
        "ci95": [percentile(samples, 0.025), percentile(samples, 0.975)],
    }
