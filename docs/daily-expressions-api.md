# Daily Expressions — Mini API Specification

Date: 2026-10-23 · Status: Implemented; deployment verification tracked separately

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
Other fields shown above are required.

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

## Implementation (2026-10-23)

The Flutter client and the existing Vocard backend implement this contract.
The default base URL is `https://vocard-chat.onrender.com`; override it with
`--dart-define=VOCARD_EXPRESSIONS_BASE_URL=https://your-service.example`.
The backend must be deployed with the new expression router before using the
client against that URL.

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
- Content caches in SharedPreferences are scoped by Firebase UID and timezone.
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

### Original starter content and publication

`Vocard_langchain/vocard/daily_expressions.json` contains seven original lessons
for October 17–23, 2026 (eight expressions). These are editorial learning content,
not textbook quotations. They use TTS; no recorded audio is supplied.

To publish more content, add a validated lesson with a new stable ID and fixed
publication date to the backend catalog, set `status` to `published`, run the
backend expression tests, and deploy. `draft` and future lessons remain hidden.
The service does not invent new lessons or rotate old content into today's date.
An empty/missing day deliberately returns 404. An optional
`VOCARD_EXPRESSIONS_PATH` selects a different JSON catalog on the backend.
