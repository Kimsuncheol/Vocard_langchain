import hashlib
import io
import json
import time
from dataclasses import replace

import boto3
import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from moto import mock_aws
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from vocard.api import create_app
from vocard.config import Settings
from vocard.db import Repository
from vocard.ingestion import IngestionService
from vocard.storage import PrivateS3
from vocard.worker import Worker


class TestEmbeddings(Embeddings):
    __test__ = False

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        return [float(n + 1) for n in hashlib.sha256(text.encode()).digest()[:8]]


def make_pdf(text="Where am I? English grammar examples.", title="test"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def make_sidecar(
    pdf, text="가정법 과거완료\n○ I enjoyed swimming.\n× I enjoyed to swim.", uncertain=True
):
    return json.dumps(
        {
            "source": {"sha256": hashlib.sha256(pdf).hexdigest()},
            "pages": [{"pdf_page_index": 0, "text": text, "uncertain": uncertain}],
        },
        ensure_ascii=False,
    ).encode()


def token(settings, role="administrator", **overrides):
    claims = {
        "sub": "test-admin",
        "role": role,
        "iat": int(time.time()) - 1,
        "exp": int(time.time()) + 3600,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        **overrides,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


@pytest.fixture
def system(tmp_path, monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings(
        db_path=tmp_path / "db.sqlite3",
        bucket="private-test-books",
        jwt_secret="x" * 40,
        embedding_dimensions=8,
        chunk_size=100,
        chunk_overlap=10,
    )
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=settings.bucket)
        s3.put_public_access_block(
            Bucket=settings.bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        storage, repo = PrivateS3(settings.bucket, s3), Repository(settings.db_path)
        service = IngestionService(settings, repo, storage)
        worker = Worker(settings, repo, storage, lambda _: TestEmbeddings())
        app = create_app(settings, repo, storage, lambda _: TestEmbeddings())
        with TestClient(app) as client:
            yield {
                "settings": settings,
                "s3": s3,
                "storage": storage,
                "repo": repo,
                "service": service,
                "worker": worker,
                "client": client,
                "headers": {"Authorization": f"Bearer {token(settings)}"},
                "replace_settings": lambda **kw: replace(settings, **kw),
            }
