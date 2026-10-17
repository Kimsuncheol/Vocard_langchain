# Stateless Vocard AI Q&A

The online application processes each question independently. Only the current
question, optional course/unit/lesson context and freshly retrieved authorized
book evidence reach generation. Conversation IDs and previous messages are not
accepted. A reference such as “왜 그런가요?” requires a focused clarification.
When more than one authorized book is available and no course selects one, the
server asks which course before retrieving rather than guessing a book.

## Application data policy

Questions and generated answers exist only in request memory and the Flutter
screen's bounded display list. They are never written by the online Q&A path to
SQLite, files, application caches, logs, analytics or tracing. Leaving the screen
clears display data and drafts; account changes also clear them. Authentication
still uses Firebase ID tokens and revocation checks. Tokens stay in headers.

Persisted data for Q&A is limited to `ai_daily_usage(uid, day, count)` using UTC
dates. Admitted failed attempts also consume quota. Book sources, reviewed
extraction, indexes, active version pointers and ingestion operational records
remain durable; they are textbook data, not user questions or generated answers.
Citations are resolved and returned per request, never saved as conversation turns.

LangSmith tracing and inherited LangChain run collectors/callbacks are disabled
around online retrieval and generation. ChatOpenAI sets `cache=False` and
`store=False`. LangChain debug/verbose output and SDK HTTP/body logging are
suppressed. Uvicorn access logging is disabled in the application and Docker
command. Validation errors return fixed messages without rejected input; unexpected
Q&A errors return a fixed error without traceback logging. Responses include
`Cache-Control: no-store` and `Pragma: no-cache`.

Deploy without request/response body capture, session replay, debug HTTP logging,
APM auto-instrumentation that captures prompts, or question-bearing analytics.
Reverse proxies and gateways must disable body buffering to disk and access/error
records containing bodies or query strings; do not cache these routes. Do not add
questions to URLs, span attributes, breadcrumbs, exception locals, identifiers or
content hashes. Operational monitoring may use route templates, status, duration,
model ID, aggregate token/cost totals and counters, without content. Infrastructure
crash dumps and swap must not retain process memory. These deployment controls
must be applied outside this application's process too.

The standalone Ollama/Gemini playground also sends only the current question;
its Basic-auth/loopback setup is separate from the Firebase production API. Never
substitute that prototype for the authorized-book production endpoint.

The fixed offline evaluation fixtures are development data, not collected student
requests. Legacy v1 histories remain frozen for audit but are ignored by the
stateless runner; old history-assisted scores are not comparable to new runs.
Never import live student questions/responses into evaluation files or graders.

## Existing installations

Startup fails closed if `ai_conversations` contains any legacy rows. There is no
new read/write path for that table. This check does not erase existing data or
claim it has disappeared from backups, WAL files or snapshots.

Stop all API/worker processes and explicitly authorize removal of legacy
conversation data before performing the migration. Preserve books, ingestion jobs,
index pointers and `ai_daily_usage`. An administrator can then run:

```sh
.venv/bin/python -m vocard.privacy_migration /absolute/path/to/vocard.sqlite3 \
  --confirm-delete-conversations
```

This irreversible opt-in operation drops only the old conversation table, enables
SQLite secure deletion, checkpoints/truncates its WAL and vacuums the database.
It does not create a content backup. Arrange deletion/expiry of pre-existing
backups, snapshots, filesystem copies and old telemetry separately under the
operator's data deletion policy. Filesystem/SSD snapshots may retain historical
blocks; SQL deletion is not a guarantee of physical erasure. The migration is not
run automatically and was not run against an existing user database by this change.

## Model provider retention (separate from application statelessness)

The default production generation and query-embedding provider is OpenAI. The
question is sent for query embedding; generation receives the question and
selected textbook excerpts. `store=False` disables optional completion storage,
but does **not** promise zero retention by OpenAI. Its API data controls describe
default abuse monitoring retention of up to 30 days, possible legal exceptions,
and provider-side prompt caching. Approved organizations/projects may qualify
for Zero Data Retention or Modified Abuse Monitoring, subject to endpoint/model
and feature limitations. Verify the actual project's controls and chosen model
before making a zero-retention claim. Application cache disabling does not disable
provider-side caching. See [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
(checked 2026-10-17).

The separate playground may use Ollama or Google Gemini. Their runtime, hosting
and provider retention settings must be reviewed independently. Nothing in this
application's stateless contract establishes zero retention by those providers.
