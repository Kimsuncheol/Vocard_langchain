import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    bucket: str
    jwt_secret: str
    jwt_issuer: str = "vocard-admin"
    jwt_audience: str = "vocard-ingestion"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    chunk_size: int = 1000
    chunk_overlap: int = 150
    pipeline_revision: str = "3"
    lease_seconds: float = 120
    max_attempts: int = 3
    max_upload_bytes: int = 100 * 1024 * 1024
    max_pages: int = 2000
    max_chunks: int = 30000
    ocr_languages: str = "kor+eng"
    ocr_dpi: int = 300
    ocr_timeout: int = 120
    ocr_min_confidence: float = 85
    ocr_max_pixels: int = 16000000
    tessdata_dir: str | None = None
    top_k: int = 4
    max_fallback: int = 2
    min_score: float = 0.0
    answer_model: str = "gpt-4.1-mini"
    answer_timeout: int = 60
    answer_max_tokens: int = 3000
    answer_context_chars: int = 60000
    firebase_project_id: str | None = None
    course_books: dict[str, str] = field(default_factory=dict)
    ai_daily_quota: int = 50

    def __post_init__(self):
        if not self.bucket or len(self.jwt_secret.encode()) < 32:
            raise ValueError("Set VOCARD_S3_BUCKET and a JWT secret of at least 32 bytes")
        if not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("Chunk overlap must be smaller than chunk size")
        if (
            min(
                self.embedding_dimensions,
                self.max_attempts,
                self.max_pages,
                self.max_upload_bytes,
                self.max_chunks,
            )
            < 1
            or self.lease_seconds < 3
        ):
            raise ValueError("Resource limits must be positive; lease must be >= 3 seconds")
        if (
            not 72 <= self.ocr_dpi <= 600
            or self.ocr_timeout < 1
            or not 0 <= self.ocr_min_confidence <= 100
            or self.ocr_max_pixels < 1
        ):
            raise ValueError("Invalid OCR resource limits")
        if self.ocr_languages != "kor+eng":
            raise ValueError("This extractor requires both Korean and English OCR models (kor+eng)")
        if (
            not 1 <= self.top_k <= 100
            or not 0 <= self.max_fallback <= 100
            or not -1 <= self.min_score <= 1
        ):
            raise ValueError("Invalid retrieval top_k, fallback limit or score threshold")
        if (
            not self.answer_model.strip()
            or not 1 <= self.answer_timeout <= 300
            or not 256 <= self.answer_max_tokens <= 16000
            or not 1000 <= self.answer_context_chars <= 200000
        ):
            raise ValueError("Invalid answering model or resource limits")
        if (
            not isinstance(self.course_books, dict)
            or any(
                not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
                for k, v in self.course_books.items()
            )
            or not 1 <= self.ai_daily_quota <= 10000
        ):
            raise ValueError("Invalid AI course mapping, quota limits")

    def pipeline(
        self,
        extraction_sha: str | None,
        allow_uncertain: bool,
        reviewed_extraction: bool = False,
        course_id: str | None = None,
        unit_id: str | None = None,
        chunk_size: int | None = None,
    ) -> dict:
        return {
            "revision": self.pipeline_revision,
            "embedding_provider": "openai",
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "chunk_size": self.chunk_size if chunk_size is None else chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "extraction_sha256": extraction_sha,
            "allow_uncertain": allow_uncertain,
            "reviewed_extraction": reviewed_extraction,
            "extraction_schema_version": "2.0",
            "chunking_schema": "grammar_key_point_v1",
            "course_id": course_id,
            "unit_id": unit_id,
            "ocr": {
                "languages": self.ocr_languages,
                "dpi": self.ocr_dpi,
                "timeout": self.ocr_timeout,
                "min_confidence": self.ocr_min_confidence,
                "max_pixels": self.ocr_max_pixels,
                "tessdata_dir": self.tessdata_dir,
            },
            "max_pages": self.max_pages,
            "max_chunks": self.max_chunks,
        }

    @classmethod
    def from_env(cls):
        return cls(
            db_path=Path(os.getenv("VOCARD_DB_PATH", "state/ingestion.sqlite3")),
            bucket=os.environ["VOCARD_S3_BUCKET"],
            jwt_secret=os.environ["VOCARD_JWT_SECRET"],
            jwt_issuer=os.getenv("VOCARD_JWT_ISSUER", "vocard-admin"),
            jwt_audience=os.getenv("VOCARD_JWT_AUDIENCE", "vocard-ingestion"),
            embedding_model=os.getenv("VOCARD_EMBEDDING_MODEL", "text-embedding-3-small"),
            embedding_dimensions=int(os.getenv("VOCARD_EMBEDDING_DIMENSIONS", "1536")),
            chunk_size=int(os.getenv("VOCARD_CHUNK_SIZE", "1000")),
            chunk_overlap=int(os.getenv("VOCARD_CHUNK_OVERLAP", "150")),
            pipeline_revision=os.getenv("VOCARD_PIPELINE_REVISION", "3"),
            lease_seconds=float(os.getenv("VOCARD_LEASE_SECONDS", "120")),
            max_attempts=int(os.getenv("VOCARD_MAX_ATTEMPTS", "3")),
            max_upload_bytes=int(os.getenv("VOCARD_MAX_UPLOAD_BYTES", "104857600")),
            max_pages=int(os.getenv("VOCARD_MAX_PAGES", "2000")),
            max_chunks=int(os.getenv("VOCARD_MAX_CHUNKS", "30000")),
            ocr_dpi=int(os.getenv("VOCARD_OCR_DPI", "300")),
            ocr_timeout=int(os.getenv("VOCARD_OCR_TIMEOUT", "120")),
            ocr_min_confidence=float(os.getenv("VOCARD_OCR_MIN_CONFIDENCE", "85")),
            ocr_max_pixels=int(os.getenv("VOCARD_OCR_MAX_PIXELS", "16000000")),
            tessdata_dir=os.getenv("VOCARD_TESSDATA_DIR") or None,
            top_k=int(os.getenv("VOCARD_TOP_K", "4")),
            max_fallback=int(os.getenv("VOCARD_MAX_FALLBACK", "2")),
            min_score=float(os.getenv("VOCARD_MIN_SCORE", "0")),
            answer_model=os.getenv("VOCARD_ANSWER_MODEL", "gpt-4.1-mini"),
            answer_timeout=int(os.getenv("VOCARD_ANSWER_TIMEOUT", "60")),
            answer_max_tokens=int(os.getenv("VOCARD_ANSWER_MAX_TOKENS", "3000")),
            answer_context_chars=int(os.getenv("VOCARD_ANSWER_CONTEXT_CHARS", "60000")),
            firebase_project_id=os.getenv("VOCARD_FIREBASE_PROJECT_ID") or None,
            course_books=json.loads(os.getenv("VOCARD_COURSE_BOOKS", "{}")),
            ai_daily_quota=int(os.getenv("VOCARD_AI_DAILY_QUOTA", "50")),
        )
