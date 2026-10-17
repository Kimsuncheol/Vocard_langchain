import hashlib
import io
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from vocard.db import SCHEMA, Repository
from vocard.extraction import (
    GrammarExtraction,
    ReviewRequired,
    extract_grammar,
    extract_pages,
    normalize_import,
    structure_text,
)
from vocard.ingestion import InputRejected
from vocard.ocr import OCRUnavailable, TesseractOCR
from vocard.worker import Worker

from .conftest import TestEmbeddings, make_pdf, make_sidecar


def job_for(system, pdf, extraction=None, reviewed=False):
    return system["service"].submit(
        "ocr-book",
        io.BytesIO(pdf),
        "reviewing-admin",
        io.BytesIO(extraction) if extraction is not None else None,
        reviewed_extraction=reviewed,
    )


def mock_ocr(pdf, index, config):
    assert config["languages"] == "kor+eng"
    return {
        "text": "가정법 과거완료\n○ I enjoyed swimming.\n난 수영을 즐겼다.\n"
        "× I enjoyed to swim.\n수영을 즐겼다.\n122",
        "lines": [{"text": "○ I enjoyed swimming.", "ocr_confidence": 98}],
        "ocr_confidence": 97.0,
    }


def test_page_level_fallback_and_one_based_indexes_separate_printed_labels(system):
    writer = PdfWriter()
    writer.append(io.BytesIO(make_pdf()))
    writer.append(io.BytesIO(make_pdf(text=None)))
    pdf = io.BytesIO()
    writer.write(pdf)
    job = job_for(system, pdf.getvalue())
    calls = []

    def ocr(data, index, config):
        calls.append(index)
        return mock_ocr(data, index, config)

    extracted = extract_grammar(pdf.getvalue(), job, None, ocr)
    assert calls == [1]  # Internal renderer offset, not a source page index.
    assert extracted["source"]["page_index_base"] == 1
    assert [p["pdf_page_index"] for p in extracted["pages"]] == [1, 2]
    page = extracted["pages"][1]
    assert page["printed_page_labels"][0]["text"] == "122"
    assert page["pdf_page_index"] == page["pdf_page_number"] == 2
    assert page["extraction_method"] == "tesseract_kor_eng"
    assert [e["is_correct"] for e in page["examples"]] == [True, False]
    assert page["examples"][0]["translations"] == ["난 수영을 즐겼다."]
    assert page["examples"][1]["english"] == "I enjoyed to swim."
    assert extracted["review"]["required"] is True
    GrammarExtraction.model_validate(extracted)


def test_ocr_draft_is_saved_and_never_embedded_then_reviewed_import_publishes(system):
    pdf = make_pdf(text=None)
    draft_job = job_for(system, pdf)
    embeddings_called = []

    def forbidden_embeddings(pipeline):
        embeddings_called.append(True)
        raise AssertionError("A review draft must never reach the embedding provider")

    Worker(
        system["settings"], system["repo"], system["storage"], forbidden_embeddings, mock_ocr
    ).run_once()
    status = system["repo"].job(draft_job["job_id"])
    assert status["stage"] == "review_required"
    assert status["review_status"] == "required"
    assert status["extraction_status"] == "succeeded"
    assert embeddings_called == []
    endpoint = f"/admin/jobs/{draft_job['job_id']}/extraction"
    assert system["client"].get(endpoint).status_code == 401
    response = system["client"].get(endpoint, headers=system["headers"])
    assert response.status_code == 200
    assert response.json()["source"]["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert (
        system["client"]
        .post(f"/admin/jobs/{draft_job['job_id']}/retry", headers=system["headers"])
        .status_code
        == 409
    )
    reviewed_job = job_for(system, pdf, response.content, reviewed=True)
    assert system["worker"].run_once()
    final = system["repo"].job(reviewed_job["job_id"])
    assert final["status"] == "succeeded" and final["review_status"] == "approved"
    assert final["reviewed_by"] == "reviewing-admin" and final["reviewed_at"]
    active = system["repo"].active_index("ocr-book")
    index = json.loads(system["storage"].get(active["object_key"], active["sha256"]))
    grammar = index["documents"][0]["metadata"]["grammar"]
    assert grammar["examples"][1]["is_correct"] is False
    assert grammar["examples"][0]["translations"] == ["난 수영을 즐겼다."]


def test_reviewed_import_retains_all_grammar_fields_and_original_zero_index(system):
    pdf = make_pdf(text=None)
    imported = json.loads(make_sidecar(pdf))
    page = imported["pages"][0]
    page.update(
        {
            "printed_page_labels": [{"text": "iv", "uncertain": False}],
            "headings": [{"text": "가정법 과거완료"}],
            "explanations": [{"text": "그때 실제로는 그렇지 않았다."}],
            "examples": [
                {
                    "text": "× I enjoyed to swim.",
                    "is_correct": False,
                    "translations": ["난 수영을 즐겼다."],
                    "translation": "난 수영을 즐겼다.",
                    "correct_incorrect_markers": [{"raw": "×"}],
                }
            ],
            "translations": ["난 수영을 즐겼다."],
            "example_pairs": [{"example_ids": ["a", "b"]}],
        }
    )
    sidecar = json.dumps(imported, ensure_ascii=False).encode()
    job = job_for(system, pdf, sidecar, reviewed=True)
    result = extract_grammar(pdf, job, sidecar)
    canonical = result["pages"][0]
    assert canonical["pdf_page_index"] == 1
    assert canonical["source_pdf_page_index"] == 0
    assert canonical["source_page_index_base"] == 0
    for field in (
        "headings",
        "explanations",
        "examples",
        "translations",
        "example_pairs",
        "printed_page_labels",
        "text",
    ):
        assert canonical[field] == page[field]
    docs = extract_pages(pdf, job, sidecar)
    assert docs[0].metadata["grammar"]["examples"][0]["is_correct"] is False
    assert docs[0].metadata["printed_page_labels"] == page["printed_page_labels"]


@pytest.mark.parametrize("failure", [OCRUnavailable("missing kor"), TimeoutError("timeout")])
def test_ocr_error_is_reviewable_and_contains_no_provider_error_text(system, failure):
    pdf = make_pdf(text=None)
    job = job_for(system, pdf)

    def broken(*args):
        raise failure

    Worker(
        system["settings"], system["repo"], system["storage"], lambda _: TestEmbeddings(), broken
    ).run_once()
    status = system["repo"].job(job["job_id"])
    assert status["review_status"] == "required"
    artifact = json.loads(
        system["storage"].get(status["extracted_key"], status["extracted_sha256"])
    )
    assert "ocr_failed" in artifact["pages"][0]["uncertainty_flags"]
    assert "empty_page" in artifact["pages"][0]["uncertainty_flags"]
    assert not system["repo"].book("ocr-book")["active_index_version"]


def test_suspicious_embedded_text_also_triggers_ocr_and_review(system):
    pdf = make_pdf("### ???? !!!!!")
    job = job_for(system, pdf)
    result = extract_grammar(pdf, job, None, mock_ocr)
    assert result["pages"][0]["extraction_method"] == "tesseract_kor_eng"
    assert result["pages"][0]["embedded_text_before_ocr"].startswith("###")
    assert result["review"]["required"]


def test_bare_json_approval_and_allow_uncertain_do_not_bypass_review(system):
    pdf = make_pdf(text=None)
    imported = json.loads(make_sidecar(pdf))
    imported["review"] = {"required": False, "reviewed_by": "forged-admin"}
    sidecar = json.dumps(imported).encode()
    job = system["service"].submit(
        "ocr-book", io.BytesIO(pdf), "admin", io.BytesIO(sidecar), allow_uncertain=True
    )
    result = extract_grammar(pdf, job, sidecar)
    assert result["review"]["required"] and result["review"]["reviewed_by"] is None
    with pytest.raises(ReviewRequired):
        extract_pages(pdf, job, sidecar)


def test_review_required_new_version_preserves_the_previous_active_index(system):
    job_for(system, make_pdf())
    system["worker"].run_once()
    previous = system["repo"].active_index("ocr-book")
    job = job_for(system, make_pdf(text=None, title="scanned replacement"))
    Worker(
        system["settings"], system["repo"], system["storage"], lambda _: TestEmbeddings(), mock_ocr
    ).run_once()
    assert system["repo"].job(job["job_id"])["review_status"] == "required"
    assert system["repo"].active_index("ocr-book") == previous
    assert system["storage"].get(previous["object_key"], previous["sha256"])


@pytest.mark.parametrize("bad", ["false", 0, 1])
def test_is_correct_requires_boolean_not_coerced_strings_or_numbers(system, bad):
    pdf = make_pdf()
    imported = json.loads(make_sidecar(pdf))
    imported["pages"][0]["examples"] = [{"text": "I enjoyed to swim.", "is_correct": bad}]
    with pytest.raises(InputRejected):
        normalize_import(json.dumps(imported).encode(), hashlib.sha256(pdf).hexdigest(), 1)


def test_empty_reviewed_pages_need_explicit_blank_disposition(system):
    writer = PdfWriter()
    writer.append(io.BytesIO(make_pdf()))
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    pdf = output.getvalue()
    imported = {
        "source": {"sha256": hashlib.sha256(pdf).hexdigest(), "page_index_base": 1},
        "pages": [
            {"pdf_page_index": 1, "text": "가정법 과거완료와 그 문장의 의미를 설명한다."},
            {"pdf_page_index": 2, "text": ""},
        ],
    }
    sidecar = json.dumps(imported).encode()
    job = job_for(system, pdf, sidecar, reviewed=True)
    assert extract_grammar(pdf, job, sidecar)["review"]["required"]
    imported["pages"][1]["review_disposition"] = "blank"
    accepted = json.dumps(imported).encode()
    job = job_for(system, pdf, accepted, reviewed=True)
    docs = extract_pages(pdf, job, accepted)
    assert len(docs) == 1 and docs[0].metadata["pdf_page_index"] == 1


def test_legacy_v1_database_migrates_without_changing_active_pointer(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("PRAGMA user_version=1")
    con.execute("INSERT INTO books VALUES (?,?,?)", ("legacy", "retained-index", time.time()))
    con.commit()
    con.close()
    repo = Repository(path)
    assert repo.book("legacy")["active_index_version"] == "retained-index"
    with repo.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert {"reviewed_by", "extracted_sha256", "review_status"} <= columns


def test_conservative_structure_never_repairs_incorrect_examples():
    source = "■ 동명사\n이 설명은 원문이다.\n× I enjoyed to swim.\n난 수영을 즐겼다."
    parsed = structure_text(source)
    assert parsed["explanations"] == [{"text": "이 설명은 원문이다."}]
    assert parsed["examples"][0]["text"] == "× I enjoyed to swim."
    assert parsed["examples"][0]["is_correct"] is False
    assert parsed["examples"][0]["translations"] == ["난 수영을 즐겼다."]


def test_real_pdfium_render_and_tesseract_tsv_contract(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--list-langs" in command:
            return SimpleNamespace(stdout=b"List of available languages (2):\nkor\neng\n")
        assert kwargs["input"].startswith(b"\x89PNG")
        assert command[command.index("-l") + 1] == "kor+eng"
        header = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext\n"
        )
        rows = (
            "5\t1\t1\t1\t1\t1\t20\t20\t40\t10\t94\t가정법\n"
            "5\t1\t1\t1\t1\t2\t65\t20\t30\t10\t91\tEnglish\n"
        )
        return SimpleNamespace(stdout=(header + rows).encode())

    monkeypatch.setattr("vocard.ocr.subprocess.run", run)
    result = TesseractOCR()(
        make_pdf(), 0, {"languages": "kor+eng", "dpi": 100, "max_pixels": 1000000}
    )
    assert len(calls) == 2
    assert result["text"] == "가정법 English"
    assert result["ocr_confidence"] == 92.5
    assert result["lines"][0]["words"][0]["text"] == "가정법"


def test_review_import_requires_source_json_and_admin_authorization(system):
    pdf = make_pdf()
    response = system["client"].post(
        "/admin/books/ocr-book/ingestions",
        headers=system["headers"],
        files={"file": ("book.pdf", pdf)},
        data={"reviewed_extraction": "true"},
    )
    assert response.status_code == 422
    sidecar = make_sidecar(make_pdf(title="other source"))
    job = job_for(system, pdf, sidecar, reviewed=True)
    system["worker"].run_once()
    assert system["repo"].job(job["job_id"])["status"] == "failed"
    assert not system["repo"].book("ocr-book")["active_index_version"]


def test_one_based_import_mixed_or_duplicate_indexes_rejected(system):
    pdf = make_pdf()
    sidecar = json.loads(make_sidecar(pdf))
    sidecar["source"]["page_index_base"] = 1
    sidecar["pages"][0]["pdf_page_index"] = 1
    result = normalize_import(json.dumps(sidecar).encode(), hashlib.sha256(pdf).hexdigest(), 1)
    assert result["pages"][0]["pdf_page_index"] == 1
    sidecar["pages"][0]["pdf_page_index"] = 0
    with pytest.raises(InputRejected):
        normalize_import(json.dumps(sidecar).encode(), hashlib.sha256(pdf).hexdigest(), 1)
