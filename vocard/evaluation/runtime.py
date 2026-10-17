"""Evaluation-only adapters: full OCR lexical baseline or the production retriever."""

import json
import math
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

import httpx
from langchain_core.documents import Document
from langchain_openai import ChatOpenAI
from pydantic import Field

from vocard.answering import AnswerDraft
from vocard.chunking import split_grammar_points
from vocard.evaluation.dataset import Strict, digest, sha256
from vocard.retrieval import ActiveBookRetriever
from vocard.vectors import embeddings_for


class Candidate(Strict):
    candidate_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    provider: str = Field(pattern=r"^(openai|ollama|extractive_baseline|abstain_baseline)$")
    model: str
    input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cached_input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class RunConfig(Strict):
    candidates: list[Candidate] = Field(min_length=1)
    ks: list[int] = Field(default=[1, 3, 5])
    generation_k: int = Field(default=5, ge=1, le=100)
    max_fallback: int = Field(default=2, ge=0, le=100)
    min_score: float = Field(default=0, ge=-1, le=1, allow_inf_nan=False)
    chunk_size: int = Field(default=1000, ge=200, le=10000)
    context_chars: int = Field(default=60000, ge=1000, le=200000)
    max_tokens: int = Field(default=1500, ge=256, le=16000)
    local_context_tokens: int = Field(default=8192, ge=4096, le=131072)
    timeout: int = Field(default=60, ge=1, le=300)
    embedding_input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    prices_as_of: str | None = None
    price_source: str | None = None

    def validate_comparison(self):
        if len({c.candidate_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate IDs must be unique")
        if not self.ks or any(type(k) is not int or not 1 <= k <= 100 for k in self.ks):
            raise ValueError("Invalid recall k values")


def settings_for(settings, config, candidate):
    return replace(
        settings,
        answer_model=candidate.model,
        answer_timeout=config.timeout,
        answer_max_tokens=config.max_tokens,
        answer_context_chars=config.context_chars,
    )


class ModelAdapter:
    def __init__(self, candidate, config, schema=AnswerDraft):
        self.candidate, self.config, self.schema = candidate, config, schema
        self.usage = None
        self.context_ids = []
        self.raw_output = None

    def invoke(self, messages):
        envelope = json.loads(messages[-1].content)
        self.context_ids = [e["chunk_id"] for e in envelope.get("evidence", [])]
        provider = self.candidate.provider
        if provider in ("extractive_baseline", "abstain_baseline"):
            self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
            evidence = envelope.get("evidence", [])
            ref = evidence[0] if evidence and provider == "extractive_baseline" else None
            return AnswerDraft(
                answer="검색된 교재 발췌입니다." if ref else "교재 근거가 부족합니다.",
                status="answered" if ref else "insufficient_evidence",
                citations=[{"chunk_id": ref["chunk_id"], "quote": ref["text"][:400]}]
                if ref
                else [],
                source_examples=[],
                practice_examples=[],
            )
        if provider == "ollama":
            response = httpx.post(
                "http://127.0.0.1:11434/api/chat",
                timeout=self.config.timeout,
                trust_env=False,
                json={
                    "model": self.candidate.model,
                    "stream": False,
                    "think": False,
                    "format": self.schema.model_json_schema(),
                    "messages": [
                        {"role": "system" if m.type == "system" else "user", "content": m.content}
                        for m in messages
                    ],
                    "options": {
                        "temperature": 0,
                        "seed": 20261017,
                        "num_predict": self.config.max_tokens,
                        "num_ctx": self.config.local_context_tokens,
                    },
                },
            )
            response.raise_for_status()
            payload = response.json()
            self.raw_output = payload["message"]["content"]
            self.usage = {
                "input_tokens": payload["prompt_eval_count"],
                "output_tokens": payload["eval_count"],
                "cached_input_tokens": 0,
            }
            return self.schema.model_validate_json(payload["message"]["content"])
        model = ChatOpenAI(
            model=self.candidate.model,
            temperature=0,
            max_retries=0,
            timeout=self.config.timeout,
            max_tokens=self.config.max_tokens,
        )
        raw = model.with_structured_output(
            self.schema, method="json_schema", strict=True, include_raw=True
        ).invoke(messages)
        self.raw_output = raw["raw"].content
        usage = raw["raw"].usage_metadata
        if usage:
            self.usage = {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cached_input_tokens": usage.get("input_token_details", {}).get("cache_read", 0),
            }
        if raw["parsed"] is None:
            raise ValueError("Structured model parsing failed")
        return raw["parsed"]

    def cost(self):
        if self.candidate.provider != "openai":
            return 0.0 if self.usage is not None else None
        return usage_cost(self.usage, self.candidate)


def usage_cost(usage, candidate):
    if usage is None:
        return None
    cached = usage.get("cached_input_tokens", 0)
    if candidate.input_usd_per_million is None or candidate.output_usd_per_million is None:
        return None
    if cached and candidate.cached_input_usd_per_million is None:
        return None
    return (
        (usage["input_tokens"] - cached) * candidate.input_usd_per_million
        + cached * (candidate.cached_input_usd_per_million or 0)
        + usage["output_tokens"] * candidate.output_usd_per_million
    ) / 1_000_000


class MeteredResource:
    """Capture provider-reported embedding usage, including internally batched calls."""

    def __init__(self, resource, ledger):
        self.resource, self.ledger = resource, ledger

    def create(self, **kwargs):
        response = self.resource.create(**kwargs)
        payload = response if isinstance(response, dict) else response.model_dump()
        tokens = (payload.get("usage") or {}).get("total_tokens")
        self.ledger.append(tokens if type(tokens) is int and tokens >= 0 else None)
        return response


def metered_embeddings(ledger):
    def factory(pipeline):
        embeddings = embeddings_for(pipeline)
        embeddings.client = MeteredResource(embeddings.client, ledger)
        return embeddings

    return factory


def terms(text):
    result = re.findall(r"[a-z]+|[가-힣]+", text.lower())
    return [
        token
        for word in result
        for token in (
            [word]
            if re.fullmatch(r"[a-z]+", word) or len(word) < 3
            else [word, *[word[i : i + 2] for i in range(len(word) - 1)]]
        )
    ]


class ReferenceCorpus:
    """All 425 OCR pages, grouped by production chunker; no evaluation gold is used in ranking."""

    def __init__(self, ocr_path, curriculum, manifest, config, book_id):
        raw = json.loads(Path(ocr_path).read_text())
        if sha256(ocr_path) != manifest["reference_ocr_sha256"]:
            raise ValueError("Frozen reference OCR content changed")
        if raw["source"]["sha256"] != manifest["source"]["sha256"]:
            raise ValueError("OCR PDF hash differs from fixed dataset")
        self.pages = {p["pdf_page_number"]: p for p in raw["pages"]}
        if set(self.pages) != set(range(1, manifest["source"]["pdf_page_count"] + 1)):
            raise ValueError("Offline corpus must include every PDF page")
        contexts = {}
        for asset in manifest["asset_sources"]:
            path = Path(curriculum) / asset["name"]
            if sha256(path) != asset["sha256"]:
                raise ValueError("Curriculum metadata changed; version the evaluation corpus")
            unit = json.loads(path.read_text())
            for lesson in unit["sections"]:
                for page in range(
                    lesson["source_pages"]["start"], lesson["source_pages"]["end"] + 1
                ):
                    contexts[page] = {
                        "course_id": unit["course_id"],
                        "unit_id": unit["id"],
                        "lesson_number": lesson["lesson_number"],
                        "lesson_title": lesson["title"],
                    }
        self.book_id = book_id
        self.identity = {
            "mode": "offline_ocr_lexical",
            "source_sha256": raw["source"]["sha256"],
            "ocr_sha256": sha256(ocr_path),
            "chunk_size": config.chunk_size,
            "curriculum_sha256": digest(manifest["asset_sources"]),
            "metadata_policy": manifest["reference_metadata_policy"],
        }
        self.index_version = "offline-" + digest(self.identity)
        pages = [
            Document(
                page_content=p["text"],
                metadata={
                    "book_id": book_id,
                    "book_version": 1,
                    "sha256": raw["source"]["sha256"],
                    "pdf_page_index": n,
                    "pdf_page_number": n,
                    "printed_page_labels": p["printed_page_labels"],
                    "grammar": {
                        k: p.get(k, [])
                        for k in (
                            "headings",
                            "explanations",
                            "examples",
                            "translations",
                            "example_pairs",
                        )
                    },
                    # Offline reference metadata is applied after OCR chunking below.
                },
            )
            for n, p in sorted(self.pages.items())
        ]
        job = {
            "book_id": book_id,
            "book_version": 1,
            "sha256": raw["source"]["sha256"],
            "pipeline_fingerprint": digest(self.identity),
            "job_id": "offline-evaluation",
            "pipeline_json": json.dumps(
                {"chunk_size": config.chunk_size, "chunk_overlap": 150, "max_chunks": 30000}
            ),
        }
        groups = {}
        for page in pages:
            number = page.metadata["pdf_page_index"]
            context = contexts.get(number, {})
            groups.setdefault(context.get("lesson_number", f"unassigned-{number}"), []).append(page)
        self.documents = []
        for group in groups.values():
            chunks = split_grammar_points(group, job)
            for doc in chunks:
                doc.metadata.update(contexts.get(doc.metadata["source_pages"][0], {}))
            self.documents.extend(chunks)
        for doc in self.documents:
            doc.metadata["index_version"] = self.index_version
        self.counts = [Counter(terms(d.page_content)) for d in self.documents]
        self.idf = {
            t: math.log(1 + (len(self.counts) - df + 0.5) / (df + 0.5))
            for t, df in Counter(t for row in self.counts for t in row).items()
        }
        self.average_length = sum(sum(c.values()) for c in self.counts) / len(self.counts)
        self.config = config

    def active_index(self, book_id):
        if book_id != self.book_id:
            raise ValueError("Offline book scope changed")
        return {"index_version": self.index_version}

    def retrieve(self, case, query, k):
        candidates = []
        query_terms = set(terms(query))
        for doc, counts in zip(self.documents, self.counts, strict=True):
            if (doc.metadata.get("course_id"), doc.metadata.get("unit_id")) != (
                case.course_id,
                case.unit_id,
            ):
                continue
            length = sum(counts.values())
            score = sum(
                self.idf.get(t, 0)
                * counts[t]
                * 2.5
                / (counts[t] + 1.5 * (0.25 + 0.75 * length / self.average_length))
                for t in query_terms
                if counts[t]
            )
            # Lexical scores are BM25-like, not cosine; do not reuse a cosine threshold.
            if score > 0:
                candidates.append((doc, score))
        candidates.sort(key=lambda row: (-row[1], row[0].id))
        primary = [
            d for d, _ in candidates if d.metadata.get("lesson_number") == case.lesson_number
        ]
        fallback = [
            d for d, _ in candidates if d.metadata.get("lesson_number") != case.lesson_number
        ]
        return primary[:k] + fallback[: min(max(0, k - len(primary)), self.config.max_fallback)]


class ProductionCorpus:
    def __init__(self, settings, repository, storage, book_id, manifest, config):
        self.settings, self.repository, self.storage = settings, repository, storage
        self.book_id, self.config = book_id, config
        active = repository.active_index(book_id)
        if active["source_sha256"] != manifest["source"]["sha256"]:
            raise ValueError("Active book differs from the fixed source PDF")
        self.index_version = active["index_version"]
        self.identity = {
            "mode": "production_active_index",
            "source_sha256": active["source_sha256"],
            "index_version": self.index_version,
            "pipeline_fingerprint": active["pipeline_fingerprint"],
        }
        self.ledger = []

    def active_index(self, book_id):
        return self.repository.active_index(book_id)

    def retrieve(self, case, query, k):
        self.ledger = []
        return ActiveBookRetriever(
            repository=self.repository,
            storage=self.storage,
            embedding_factory=metered_embeddings(self.ledger),
            book_id=self.book_id,
            authorized_book_ids=frozenset({self.book_id}),
            top_k=k,
            current_lesson=case.lesson_number,
            course_id=case.course_id,
            unit_id=case.unit_id,
            max_fallback=self.config.max_fallback,
            min_score=self.config.min_score,
        ).invoke(query)
