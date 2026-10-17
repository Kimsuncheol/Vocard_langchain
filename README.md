# Vocard administrator book ingestion

FastAPI accepts administrator-authorized uploads and records durable jobs. A separate
worker uses LangChain documents, text splitting, and OpenAI embeddings to build an
immutable index in private S3. A LangChain `BaseRetriever` resolves the active index
at query time. There is no administrator dashboard or in-request ingestion worker.

## Install and configure

Use Python 3.11 or newer (tested with Python 3.14):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install --no-deps -e .
cp .env.example .env
```

`requirements.txt` pins the tested environment, including test tools;
`requirements.in` declares runtime dependencies for the installable package.
Edit `.env`, then export its values in both service environments, for example
`set -a; source .env; set +a` in a trusted local shell. The application does not
implicitly load environment files. Docker Compose reads `.env` itself.

Required configuration:

- `VOCARD_S3_BUCKET`: an existing private AWS S3 bucket. The optional CloudFormation
  template in `infrastructure/private-bucket.yaml` creates encrypted storage with
  versioning, TLS-only access, and all four Block Public Access settings enabled.
  Nothing in this repository automatically creates or changes cloud resources.
- AWS credentials through the standard boto3 credential chain, preferably a service
  IAM role. Each service requires `s3:GetBucketPublicAccessBlock` and
  `s3:GetBucketPolicyStatus` on the bucket. The API requires `s3:PutObject` and
  `s3:GetObject` on `sources/*` and `extractions/*`, plus `s3:GetObject` on `indexes/*`
  for search. The worker requires `s3:GetObject` on all three prefixes and
  `s3:PutObject` on `indexes/*`. No public ACL, public URL, deletion, or list
  permission is needed. The bucket check runs at startup and before each operation;
  missing permissions or unsafe configuration fail closed.
- `VOCARD_JWT_SECRET`: at least 32 random bytes shared with your trusted JWT issuer.
  API tokens must use HS256 and contain `sub`, `iat`, `exp`, matching `iss` and
  `aud`, and exactly `role: "administrator"`. Defaults are issuer `vocard-admin`
  and audience `vocard-ingestion`. End users must never receive the signing secret.
  The application deliberately has no token-minting or role-assignment endpoint.
- `OPENAI_API_KEY` in the worker and API search environment. Document text is sent
  to OpenAI for embeddings. Model and dimensions are snapshotted per job/index;
  search uses that snapshot, including when the service defaults change.

Set `VOCARD_ADMIN_TOKEN` to a short-lived administrator JWT issued by your trusted
identity service. CLI requests use this token; the CLI cannot bypass authorization
by accessing the queue or database directly. Authentication runs before multipart
body parsing. Remote CLI connections require HTTPS. Put an HTTPS reverse proxy
with request limits in front of a remotely exposed API.

## Run the API and worker independently

In separate terminals with the same configuration and persistent database path:

```sh
.venv/bin/uvicorn vocard.api:create_app --factory --host 127.0.0.1 --port 8000
```

```sh
.venv/bin/python -m vocard.worker
```

Alternatively, `docker compose up --build -d` runs both processes with restart
policies and a shared persistent SQLite volume. Use service-role credentials or
explicitly provide AWS credential configuration to the containers. `--once` on
the worker claims at most one currently available job; omit it for a service.
SIGTERM finishes the current attempt before exiting. A killed worker's lease
expires and another worker can recover the job.

SQLite uses WAL, full synchronization, transactions, and a persistent file. Run
the API and workers on one host with the same local disk/volume; do not put the
database on NFS or copy it independently to multiple hosts. Back it up using
SQLite's online backup mechanism; copying only the main file while WAL is active
is insufficient. Multi-host deployments need a transactional queue/database
adapter such as PostgreSQL. Worker processes and database files belong to the
trusted service boundary; there is no unauthenticated remote worker interface.

## CLI

```sh
.venv/bin/vocard ingest grammar-book /absolute/path/book.pdf
.venv/bin/vocard job JOB_ID
.venv/bin/vocard book grammar-book
.venv/bin/vocard retry FAILED_JOB_ID
.venv/bin/vocard search grammar-book 'Where am I?' --k 4
```

The ingest command returns immediately after private uploads and the database
transaction, with `job_id`, `book_id`, PDF `sha256`, numeric `book_version`,
`extraction_status`, and queue status. `book` returns the active index version,
source version history, and immutable index history. Failed-job retry is explicit
and audited by administrator subject; re-uploading the same failed input returns
the same failed job rather than spending more embedding requests automatically.

For the supplied scanned textbook, use the existing structured extraction:

```sh
.venv/bin/vocard ingest grammar-book \
  '/Users/gimseoncheol/Downloads/840194660-영문법악마의100.pdf' \
  --extraction-json output/pdf/textbook-extraction/textbook.json \
  --allow-uncertain
```

`--allow-uncertain` explicitly permits indexing the earlier OCR draft. Without it,
uncertain extraction or pages with no embedded text fail validation. The default
PDF loader does not run OCR; scanned PDFs need a structured extraction. Uncertainty
and source page indices remain in retrieval metadata. The original PDF and full
structured extraction remain immutable private objects. Source wording and
incorrect examples are not rewritten or completed. Text inside source documents
is never executed as instructions. No LLM is used to extract or correct content.

Structured extraction JSON has this minimum shape; page indices must cover the
actual PDF exactly once in order, starting at zero:

```json
{
  "source": {"sha256": "SHA-256 of the exact uploaded PDF bytes"},
  "pages": [
    {"pdf_page_index": 0, "text": "Original Korean and English text", "uncertain": false}
  ]
}
```

The earlier complete JSON format, including `lines`, `uncertainty_flags`, and
document-level `extraction.uncertain`, is supported. Empty pages can be skipped
only with explicit uncertainty acceptance; an entirely empty extraction never
publishes. Upload, page, and chunk limits are configurable in `.env.example`.

## Durability, idempotency, and publication

1. Hash upload bytes and conditionally store the PDF at
   `sources/sha256/<sha256>.pdf` with server-side AES256 encryption. OCR sidecars
   are also content-addressed. No upload is overwritten. An interrupted upload
   or enqueue may leave an unreferenced object, which cannot become active.
2. Under a database write transaction, deduplicate a book version by
   `(book_id, source SHA-256)` and a job by `(book version, pipeline fingerprint)`.
   The fingerprint includes extraction SHA-256, model, dimensions, chunk settings,
   uncertainty acceptance, limits, and explicit pipeline revision. Increment
   `VOCARD_PIPELINE_REVISION` when changing extraction or indexing behavior.
   Concurrent duplicate submissions return the same job and source version.
3. A separate worker claims a persisted job using an expiring, heartbeated lease.
   Every mutation checks the lease token and expiry. Transient failures retry with
   bounded exponential backoff; malformed inputs and validation failures stop
   immediately. Claims, retry count, stage, extraction status, timestamps, requesting
   administrator, and last safe error are persisted. Expired final attempts become
   failed; explicit administrator retry grants three additional attempts.
4. Build at an attempt-specific immutable `indexes/<book>/<job>/<lease>.json` key.
   Retry attempts can repeat extraction/embedding work (at-least-once execution),
   but cannot overwrite a competing attempt or publish twice. No Python pickle
   or unsafe vector-store deserialization is used.
5. Verify that chunk source spans cover every extracted character without gaps.
   Download the stored index and validate its checksum, exact documents and metadata,
   source identity, chunk count, unique IDs, finite nonzero vectors, dimensions,
   model configuration, and query/document embedding consistency. The query smoke
   test requires cosine similarity of at least 0.95 for identical input; this tests
   pipeline consistency, not semantic retrieval quality or OCR accuracy.
6. In one fenced transaction, record the validation report and new immutable index
   version, switch `books.active_index_version`, and complete the job. Failure before
   this transaction leaves the active pointer untouched. The old index, validation
   report, and previous-version link remain available. An older queued job finishing
   after a newer publication is recorded as `superseded` and cannot roll back the
   active pointer. In-flight queries finish against the snapshot they already read.

Embedding a large textbook may take time and incur provider costs. Live ingestion
does not happen during installation, tests, or upload submission. JSON vector
artifacts and cosine retrieval are intended for a modest book library; large
collections should use a vector database behind the same publication boundary.

## Verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check vocard tests
.venv/bin/ruff format --check vocard tests
```

Tests use mocked S3 and deterministic test-only embeddings, with no real AWS or
OpenAI credentials. They cover authorization, concurrent idempotency, private
encrypted storage, durable queue reopening, crash recovery and fencing, lease
heartbeats, retries, validation failures, OCR provenance/uncertainty, retrieval,
and preservation of the previous active index.

Current local verification: 29 passing tests, including the CLI against a live
local HTTP server. The supplied 425-page scanned PDF and structured OCR also passed
a complete smoke ingestion into mocked private S3 with test-only embeddings:
425 indexed pages, 492 chunks, successful validation and retrieval. Source-span
coverage was checked against the complete original OCR text. AWS/OpenAI live calls
and the container build were not run; Docker was unavailable in the local environment.

Reference APIs: [LangChain embeddings](https://reference.langchain.com/python/langchain-core/embeddings/embeddings/Embeddings/embed_documents),
[LangChain retrievers](https://reference.langchain.com/python/langchain-core/vectorstores/base),
and [S3 conditional object writes](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).
