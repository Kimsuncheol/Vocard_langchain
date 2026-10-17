import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Frozen offline benchmark fixtures, never accepted by the live API.
class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=10000)


DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "evaluation" / "v1"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Criterion(Strict):
    criterion_id: str
    description: str = Field(min_length=1)
    weight: float = Field(gt=0, allow_inf_nan=False)


class Provenance(Strict):
    author_type: Literal["agent", "human"]
    review_status: Literal["pending_human_review", "human_reviewed"]
    human_reviewer: str | None

    @model_validator(mode="after")
    def reviewed_identity(self):
        if self.review_status == "human_reviewed" and not self.human_reviewer:
            raise ValueError("Human-reviewed labels require an identified human reviewer")
        return self


class Attack(Strict):
    target: Literal["retrieved_text"]
    payload: str = Field(min_length=1, max_length=4000)


class Case(Strict):
    case_id: str
    group_id: str
    course_id: str
    unit_id: str
    lesson_number: int = Field(ge=1, strict=True)
    lesson_title: str
    question: str = Field(min_length=1, max_length=4000)
    history: list[HistoryMessage] = Field(max_length=20)
    answerability: Literal["answerable", "unanswerable", "needs_clarification"]
    expected_source_pages: list[int]
    rubric: list[Criterion] = Field(min_length=1)
    tags: list[str]
    attack: Attack | None
    label_provenance: Provenance

    @model_validator(mode="after")
    def evidence_and_rubric(self):
        if (self.answerability == "answerable") != bool(self.expected_source_pages):
            raise ValueError("Only answerable cases have required source pages")
        if any(type(p) is not int or p < 1 or p > 425 for p in self.expected_source_pages):
            raise ValueError("Expected pages must be 1-based PDF indexes")
        if len(set(self.expected_source_pages)) != len(self.expected_source_pages):
            raise ValueError("Duplicate source page")
        if len({r.criterion_id for r in self.rubric}) != len(self.rubric):
            raise ValueError("Duplicate rubric criterion")
        return self


def load_dataset(root=DEFAULT_DATASET, split="tuning"):
    if split not in ("tuning", "heldout"):
        raise ValueError("Choose tuning or heldout explicitly")
    root = Path(root)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest["source"]["page_index_base"] != 1:
        raise ValueError("Dataset page indexes must be 1-based")
    filename = f"{split}.jsonl"
    if sha256(root / filename) != manifest["files"][filename]:
        raise ValueError("Frozen dataset changed: create a new version instead")
    cases = [Case.model_validate_json(line) for line in (root / filename).read_text().splitlines()]
    if len(cases) != manifest["counts"][split] or len({c.case_id for c in cases}) != len(cases):
        raise ValueError("Dataset count or identity mismatch")
    return manifest, cases


def validate_splits(root=DEFAULT_DATASET):
    manifest, tuning = load_dataset(root, "tuning")
    _, heldout = load_dataset(root, "heldout")
    for attribute in ("case_id", "group_id", "lesson_number", "question"):
        if {getattr(c, attribute) for c in tuning} & {getattr(c, attribute) for c in heldout}:
            raise ValueError(f"Split leakage in {attribute}")
    if {p for c in tuning for p in c.expected_source_pages} & {
        p for c in heldout for p in c.expected_source_pages
    }:
        raise ValueError("Source page leakage between splits")
    required = {
        "korean",
        "english_example",
        "follow_up",
        "incorrect_example",
        "ocr_error",
        "unanswerable",
        "needs_clarification",
        "prompt_injection",
    }
    for cases in (tuning, heldout):
        if required - {tag for c in cases for tag in c.tags}:
            raise ValueError("A split is missing required evaluation categories")
    return {
        "dataset_id": manifest["dataset_id"],
        "tuning": len(tuning),
        "heldout": len(heldout),
        "label_status": manifest["label_status"],
    }
