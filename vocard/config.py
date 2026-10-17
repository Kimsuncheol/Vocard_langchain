import os
from dataclasses import dataclass
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
    pipeline_revision: str = "1"
    lease_seconds: float = 120
    max_attempts: int = 3
    max_upload_bytes: int = 100 * 1024 * 1024
    max_pages: int = 2000
    max_chunks: int = 30000

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

    def pipeline(self, extraction_sha: str | None, allow_uncertain: bool) -> dict:
        return {
            "revision": self.pipeline_revision,
            "embedding_provider": "openai",
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "extraction_sha256": extraction_sha,
            "allow_uncertain": allow_uncertain,
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
            pipeline_revision=os.getenv("VOCARD_PIPELINE_REVISION", "1"),
            lease_seconds=float(os.getenv("VOCARD_LEASE_SECONDS", "120")),
            max_attempts=int(os.getenv("VOCARD_MAX_ATTEMPTS", "3")),
            max_upload_bytes=int(os.getenv("VOCARD_MAX_UPLOAD_BYTES", "104857600")),
            max_pages=int(os.getenv("VOCARD_MAX_PAGES", "2000")),
            max_chunks=int(os.getenv("VOCARD_MAX_CHUNKS", "30000")),
        )
