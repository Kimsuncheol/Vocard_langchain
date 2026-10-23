# Daily Expressions — Mini API Specification

Date: 2026-10-23 · Status: Implemented; source-backed content correction on 2026-10-23

## Feature

The Home card displays today's English expression and Korean meaning. Tapping
the card opens the day's expression detail screen. A daily lesson list opens
the same detail screen for earlier dates.

Each published daily lesson has one featured expression, a stable lesson ID,
and a publication date. It may include additional expressions. The featured
expression on the Home card must match the featured expression in the detail.

## Conventions

- Base URL: `https://vocard-chat.onrender.com` over HTTPS (existing Vocard service).
- Responses use JSON and dates use `YYYY-MM-DD`.
- Send `Authorization: Bearer <Firebase ID token>` on all three endpoints.
  The backend derives the user identity from the verified token.
- `timezone` is an IANA timezone, defaults to `Asia/Seoul`, and determines the
  current local date. Publication dates are lesson labels, not timestamps.
- Only published lessons whose dates are on or before the current date in the
  requested timezone are accessible. Lesson IDs are opaque strings.
- Progress is stored separately in the existing Firestore setup; these endpoints
  return lesson content only.

## Methods and endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/expressions/today?timezone=Asia/Seoul` | Home card: today's featured expression and lesson ID |
| GET | `/expression-lessons?timezone=Asia/Seoul&limit=20` | Daily lesson list, newest first |
| GET | `/expression-lessons/{lessonId}?timezone=Asia/Seoul` | Full content for the selected day's detail screen |

### 1. Today's expression

`GET /expressions/today?timezone=Asia/Seoul`

Success: `200 OK`

```json
{
  "date": "2026-10-23",
  "timezone": "Asia/Seoul",
  "lesson_id": "daily-2026-10-23",
  "expression_id": "expr-call-it-a-day",
  "english": "Let's call it a day.",
  "korean": "오늘은 여기까지 하자."
}
```

The app opens the detail screen using `lesson_id` and initially selects
`expression_id`. If today's lesson is unavailable, return `404`; do not silently
substitute an older lesson. A cached older card must show its actual date.

### 2. Daily lesson list

`GET /expression-lessons?timezone=Asia/Seoul&before=2026-10-23&limit=20`

- `before`: optional exclusive publication-date boundary for pagination.
- `limit`: optional integer, default `20`, range `1–50`.
- Without `before`, start with the latest available lesson up to today.
- Return `200` with an empty `items` array if no lessons match.

Success: `200 OK`

```json
{
  "items": [
    {
      "lesson_id": "daily-2026-10-22",
      "date": "2026-10-22",
      "title": "Making plans",
      "expression_count": 1,
      "featured_expression": {
        "expression_id": "expr-play-it-by-ear",
        "english": "Let's play it by ear.",
        "korean": "상황을 봐 가면서 정하자."
      }
    }
  ],
  "next_before": null
}
```

When more results exist, `next_before` is the last returned item's date. Pass it
as `before` on the next request. Otherwise it is `null`. Publish at most one
lesson per date so this pagination remains unambiguous.

### 3. Expression detail

`GET /expression-lessons/daily-2026-10-23?timezone=Asia/Seoul`

Success: `200 OK`

```json
{
  "lesson_id": "daily-2026-10-23",
  "date": "2026-10-23",
  "title": "Finishing work",
  "featured_expression_id": "expr-call-it-a-day",
  "expressions": [
    {
      "expression_id": "expr-call-it-a-day",
      "english": "Let's call it a day.",
      "korean": "오늘은 여기까지 하자.",
      "usage_note_ko": "하던 일이나 활동을 오늘은 그만하자고 제안할 때 써요.",
      "dialogue": [
        {
          "speaker": "A",
          "english": "We've finished the main tasks.",
          "korean": "주요 업무는 다 끝냈어."
        },
        {
          "speaker": "B",
          "english": "Great. Let's call it a day.",
          "korean": "좋아. 오늘은 여기까지 하자."
        }
      ],
      "audio_url": null
    }
  ],
  "previous_lesson_id": "daily-2026-10-22",
  "next_lesson_id": null
}
```

`expressions` is nonempty and contains `featured_expression_id`. Previous and
next IDs refer to the closest earlier/later accessible published lessons and
are `null` at the boundaries. `audio_url` is an optional HTTPS audio resource,
represented as `null` when absent; use the app's English TTS as the fallback.
The source-backed response also includes nullable `source_url`, `source_name`,
`source_credit`, and `transcription_notice_ko`. On an expression,
`usage_note_ko` may be null when the source did not extract an explanation.
`vocabulary_notes` is optional. When English/Korean speaker turns cannot be
matched safely, `dialogue` is empty and nullable `raw_dialogue` /
`raw_translation` preserve the source text without invented alignment.
The required expression ID, English, and Korean meaning must all be present;
incomplete source cards are excluded from the archive and return 503 on direct
access. Core content remains identical between Home and detail.

## Errors

| Status | Meaning |
| --- | --- |
| 400 | Invalid timezone, date, or limit |
| 401 | Missing, invalid, or expired Firebase token |
| 403 | Anonymous account is not eligible |
| 404 | Today's lesson is missing, or the requested lesson is missing/unpublished/future-dated |
| 503 | Content service temporarily unavailable |

```json
{
  "error": {
    "code": "lesson_not_found",
    "message": "No published expression lesson is available for this date."
  }
}
```

Use stable machine-readable error codes. The app may refresh an expired token
once before retrying. Network and service errors should preserve the currently
displayed cached lesson and provide an explicit retry action.

## Vocard integration

- Home: fetch today's summary; render English, Korean, and a detail entry point.
- Lesson list: fetch summaries and paginate using `next_before`.
- Detail: fetch by lesson ID; show usage, bilingual dialogue, pronunciation,
  previous/next navigation, and a Mark as studied action.
- Cache downloaded content locally for offline reading. Refresh today's summary
  when the local date or timezone changes, including when the app resumes.
- Save study position and completion under the signed-in user's Firestore
  progress. Agree on that document schema during implementation; no additional
  progress REST endpoints are required for this feature.

All lesson IDs, dates, and content in the examples are illustrative.

## Automatic expression checks and manual recovery

Requirement: the backend checks for new expressions every day at **01:00
Asia/Seoul (KST)**, independently of whether Vocard is open. This collection
schedule is fixed to Korea time; it does not follow a student's device timezone.
Use an external scheduler with durable job records. Configure the scheduler and
the server's due-time setting to the same time. If using a UTC-only scheduler,
01:00 KST corresponds to 16:00 UTC on the previous calendar day.

The check discovers new source content, processes it, and publishes complete
lessons. Repeated checks must deduplicate by stable source identity. Preserve
existing published lessons if collection fails. A successful check with no new
content is a valid outcome; it must not manufacture today's lesson or relabel an
older one.

### Proposed operational endpoints

These are separate from the three student content endpoints. They are proposals
for an administrator gateway, not currently implemented routes.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/admin/expression-checks/latest` | Show the latest attempt, last successful check, and next scheduled check |
| POST | `/admin/expression-checks` | Start a manual check, including recovery after a failed or partial daily attempt |
| GET | `/admin/expression-checks/{checkId}` | Poll one check until it completes |

Require verified administrator access for all operational routes; a normal
student Firebase token is insufficient. Keep scraper trigger secrets on the
backend, never in the Flutter application. The scheduler uses a server-side
credential to invoke the same underlying check operation.

Manual request:

```http
POST /admin/expression-checks
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: application/json

{}
```

Accepted response: `202 Accepted`.

```json
{
  "check_id": "check-20261023-002",
  "status": "queued",
  "trigger": "manual"
}
```

If a check is already queued or running, return its ID with `202` instead of
starting duplicate work. Otherwise a manual check creates a new attempt even if
today's earlier attempt is terminal. Retry incomplete work without duplicating
previously published expressions. Manual checks may run at any time, including
before 01:00, and do not cancel the next scheduled check.

Latest-status response: `200 OK`.

```json
{
  "schedule_timezone": "Asia/Seoul",
  "next_scheduled_at": "2026-10-24T01:00:00+09:00",
  "last_successful_at": "2026-10-22T01:03:00+09:00",
  "latest_check": {
    "check_id": "check-20261023-001",
    "trigger": "scheduled",
    "status": "failed",
    "started_at": "2026-10-23T01:00:00+09:00",
    "finished_at": "2026-10-23T01:02:00+09:00",
    "new_expression_count": 0,
    "error_code": "source_unavailable"
  }
}
```

`latest_check` and `last_successful_at` are `null` before any attempt/success.
The per-check endpoint returns the `latest_check` object shape above. Status is
`queued`, `running`, `completed`, `partial`, or `failed`; start/finish times are
`null` until reached, and `error_code` is `null` when there is no error.
`new_expression_count` counts expressions durably published by that attempt.
Only `completed` updates `last_successful_at`, including a check with zero new
expressions. Unknown check IDs return `404`; operational routes also use `401`,
`403`, and `503` for authentication, authorization, and service failures.

In the administrator interface, show **Check for new expressions**, the latest
status, and the last successful check time. After a failed, partial, or missed
scheduled check, keep the manual action available. Disable duplicate submissions
while queued/running and poll by check ID; a `202` response alone is not success.

### Existing scraper integration gap

The local `english_scrapper` project already exposes
`GET /api/v1/jobs/status` and `POST /api/v1/jobs/daily/trigger` (the latter uses a
server trigger token). However, its checked-in default schedule is 06:00 KST,
and its trigger does not retry a terminal `failed` or `partial` run on the same
date. Implementation must align the schedule to 01:00 and add explicit recovery
semantics before claiming this requirement is supported. This document update
does not change the scraper, enable scheduling, or configure a hosted service.

## Implementation (2026-10-23)

The Flutter client and the existing Vocard backend implement this contract.
The default base URL is `https://vocard-chat.onrender.com`; override it with
`--dart-define=VOCARD_EXPRESSIONS_BASE_URL=https://your-service.example`.
The authenticated gateway reads persisted source content from
`https://english-scrapper.onrender.com/api/v1/articles` and
`/api/v1/articles/{articleId}`. `VOCARD_EXPRESSION_SOURCE_URL` overrides that
server-only source base URL. Student Firebase tokens are verified by Vocard and
are never forwarded to the scraper. Content reads do not trigger scraping or AI
generation.

- Home: `DailyExpressionsSection` above Recent study, with a featured summary,
  pronunciation, date, completion status, and an archive entry point.
- Detail: expression selection (when there are several), English/Korean meaning,
  reveal/hide meaning, usage, A/B dialogue with translation reveal, pronunciation,
  completion, and previous/next lesson navigation.
- Archive: month headings, dated lesson cards, completion state, and explicit
  Load more pagination. Opening from Home selects the featured expression;
  opening from the archive restores the saved expression position.
- Playback: HTTPS `audio_url` when provided, otherwise English TTS; failed audio
  playback also falls back to TTS. Home uses TTS because its summary has no audio
  field. Playback respects the app's volume; TTS respects its speech rate.
- Content caches in SharedPreferences are scoped by API base URL, Firebase UID,
  and timezone. Version 2 uses an independent source-backed namespace, so old
  starter-catalog responses cannot appear after this correction.
  Downloaded content appears immediately during refresh and remains readable
  after network/service failures. A 404 on a detail removes its withdrawn cache.
  Auth failures never expose a cached lesson for another user.
- The current device's IANA timezone determines today. Home checks for date or
  timezone changes every minute while mounted and refreshes on app resume.
  The app's offline banner leaves cached lessons accessible, including after a
  cold start with an existing Firebase session.

### Firestore progress schema

Each signed-in user owns `users/{uid}/expressionProgress/{lessonId}`:

```json
{
  "expressionId": "expr-call-it-a-day",
  "completed": true,
  "updatedAt": 1792728000000
}
```

`updatedAt` is a client epoch timestamp in milliseconds for study-position
merging. Completion is monotonic (local OR remote). Local progress and pending
writes are UID-scoped and survive restarts. The app retries synchronization on
Home/detail/archive resume and study actions. Existing user-subcollection
Firestore rules cover this path; no new progress REST routes are needed.
Marking a lesson studied also records the day in Vocard's existing streak.

### Source-backed content and root-cause correction

The initial Vocard implementation served seven original starter lessons from a
JSON catalog. It did not integrate the existing scraper. This made requests
succeed while showing different expressions than the source API. Those catalog
records are now test fixtures only: production never reads them or uses them as
an outage fallback.

The gateway indexes saved English-series articles, follows every upstream offset
page, filters non-English records, and sorts by the issue date in the canonical
article URL. It does not use insertion order, scrape/save timestamps, or the UTC
`published_at` date (which can be the previous day). For example, the October 23,
2026 source issue is `turn over a new leaf`, while its article timestamp is on
October 22 UTC. Stable source IDs are namespaced as `source-{articleId}`; starter
completion records are retained but are not assigned to different source lessons.

Detail mapping copies the source lesson fields. Speaker-prefixed wrapped lines
are joined only within a turn; English/Korean turns are paired only when the
speaker sequences match. Unaligned text, missing explanations, and OCR uncertainty
are preserved explicitly. The screen displays source attribution and a link to
the original article. No extra expressions, explanations, or translations are
generated. Upstream failures return 503, never starter content. Server metadata
and bounded detail caches expire after 60 seconds; the client retains its own
correct-source cache for offline reading.

Publication still depends on persisted source records. A successful content read
does not mean the proposed administrator scheduling/recovery endpoints above have
been implemented. No scraper trigger secret is embedded in the mobile app.
