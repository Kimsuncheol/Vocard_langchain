import hashlib
import re
from tempfile import SpooledTemporaryFile
from typing import BinaryIO

from vocard.config import Settings
from vocard.db import Repository
from vocard.storage import PrivateS3


class InputRejected(ValueError):
    """Invalid source or index data; retries cannot repair this input."""


def validate_book_id(book_id: str):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", book_id):
        raise InputRejected("book_id must be 1-100 ASCII letters, digits, underscores or hyphens")


def copy_and_hash(source: BinaryIO, destination: BinaryIO, limit: int) -> str:
    digest, total = hashlib.sha256(), 0
    while block := source.read(1024 * 1024):
        total += len(block)
        if total > limit:
            raise InputRejected("Upload exceeds configured byte limit")
        digest.update(block)
        destination.write(block)
    if total == 0:
        raise InputRejected("Empty upload")
    destination.seek(0)
    return digest.hexdigest()


class IngestionService:
    def __init__(self, settings: Settings, repository: Repository, storage: PrivateS3):
        self.settings, self.repository, self.storage = settings, repository, storage

    def submit(
        self,
        book_id: str,
        source: BinaryIO,
        actor: str,
        extraction: BinaryIO | None = None,
        allow_uncertain: bool = False,
        reviewed_extraction: bool = False,
    ):
        validate_book_id(book_id)
        if reviewed_extraction and extraction is None:
            raise InputRejected("A reviewed extraction requires an extracted JSON upload")
        with SpooledTemporaryFile(max_size=8 * 1024 * 1024) as pdf:
            sha = copy_and_hash(source, pdf, self.settings.max_upload_bytes)
            if pdf.read(5) != b"%PDF-":
                raise InputRejected("Source must be a PDF")
            pdf.seek(0)
            source_key = f"sources/sha256/{sha}.pdf"
            self.storage.put(source_key, pdf, sha, "application/pdf")
        extraction_sha = extraction_key = None
        if extraction is not None:
            with SpooledTemporaryFile(max_size=8 * 1024 * 1024) as sidecar:
                extraction_sha = copy_and_hash(extraction, sidecar, self.settings.max_upload_bytes)
                extraction_key = f"extractions/sha256/{extraction_sha}.json"
                self.storage.put(extraction_key, sidecar, extraction_sha, "application/json")
        return self.repository.enqueue(
            book_id,
            sha,
            source_key,
            extraction_key,
            self.settings.pipeline(extraction_sha, allow_uncertain, reviewed_extraction),
            actor,
            self.settings.max_attempts,
        )
