# Private AWS reader deployment

## Scope and target

This environment deploys the **current manual reader**, not the entire M2-M5 release plan. It includes adult-only Cognito managed login, temporary private reading jobs, background recognition/speech, replay, and discard. Reading-ahead queues, automatic capture, cross-capture deduplication, and a full mobile/voice evaluation are not implemented.

- Confirmed AWS account: `892334471075`.
- Region: `ap-southeast-1` (Singapore).
- Four independent application stacks: `ReaderDataStack`, `ReaderAuthStack`, `ReaderApiStack`, `ReaderWebStack`.
- The live URL and public resource identifiers are in ignored `.local/deployment/outputs.json`; the deployment command also prints the URL.
- Adult account and operational alert destination use the email supplied during setup. No public signup is allowed.

**Live app:** <https://dn9bjxhghgekj.cloudfront.net>

## First login

1. Open the deployed HTTPS reader URL. A valid tab session opens the reader immediately; otherwise the app tries Cognito sign-in once. If Cognito still has a valid login session it returns you to the homepage without asking for credentials. After explicit sign-out, select **Sign In** to start again.
2. Open `.local/deployment/first-login.json` **locally in the editor**, not in terminal logs. It is ignored by git and created with mode `0600`. Do not share or commit it.
3. Use the configured email and temporary password. Cognito requires choosing a new password on first login. The deployment does not know or store the new password.
4. Confirm the AWS Notifications/SNS subscription email to activate operational alerts. An unconfirmed subscription receives no alarms.
5. After successfully changing the password, remove the temporary file using the checked cleanup command below. It refuses removal while Cognito still reports `FORCE_CHANGE_PASSWORD`.

```bash
npm run deploy -- --account 892334471075 --region ap-southeast-1 \
  --email YOUR_ADULT_EMAIL --cleanup-login-file
```

Camera permission and audible playback still require browser interaction. Start opens the preview. Submit captures the framed page and shows full-screen progress. Another capture replaces current playback, but completed recordings remain under Recent captures until the owner deletes them. Tap a recording to play it without another model call. Closing/backgrounding the page releases or pauses camera/audio; playback never resumes automatically on return.

## Reproduce or update

Prerequisites: Node.js 22.12+, Python 3.12, uv, AWS CLI/SSO access to the explicitly selected account. Keep provider keys in ignored server-only `.env`/dotenv-style `.envrc`, never in `VITE_` variables.

```bash
npm ci
uv sync --locked
npm run check

# Review only; does not apply application changes.
npm run deploy -- --account 892334471075 --region ap-southeast-1 \
  --email YOUR_ADULT_EMAIL

# Apply the reviewed application changes and publish.
npm run deploy -- --account 892334471075 --region ap-southeast-1 \
  --email YOUR_ADULT_EMAIL --execute
```

Add `--profile YOUR_PROFILE` when not using the active/default AWS profile. The script compares STS identity against `--account` and refuses to reuse local deployment state for another account/region. Use a separate checkout/state directory for another environment.

On the initial deployment, CloudFront's generated domain is not yet known. The script creates the four stacks, then applies a second update using the exact HTTPS origin as a literal Cognito callback/logout URL and media CORS origin. This avoids a CloudFormation circular dependency. It publishes the frontend only after that configuration and secret/account bootstrap succeed.

Only the two **public** Cognito settings are injected into the frontend build. Provider values are sent directly by SDK from the local server configuration to Secrets Manager; they are never command arguments, CloudFormation values, frontend configuration, or deployment output. Only the processing role can read the provider secret. The adult invitation is suppressed; the temporary password is written once to the ignored local file.

Lambda dependencies are cross-installed as Linux ARM64 manylinux 2.28 wheels, compatible with the Python 3.12 Amazon Linux 2023 runtime. Never upload the macOS virtual environment. `npm run package:backend` and CDK asset synthesis must precede deployment; `npm run check` includes both.

## Architecture and limits

- CloudFront serves a private S3 frontend using OAC. Fingerprinted assets are cached; index/API responses are not.
- Cognito uses authorization code + PKCE, a public client without a client secret, disabled self-signup, and explicit `reader/read` / `reader/write` JWT route scopes. Access tokens stay in tab session storage and are not sent to S3 or model providers.
- API Lambda reserves an owner-bound job and uploads a size-limited raw JPEG to private S3, then commits `QUEUED` in DynamoDB. This manual deployment uses the existing raw-JPEG API request rather than the plan's future signed S3 POST. The 4 MiB cap stays below Lambda's request limit after API Gateway's Base64 expansion. The API never waits for model responses.
- DynamoDB streams dispatch committed jobs to SQS. Workers use expiring/fenced leases, checkpoint recognized text and completed audio chunks, and ignore duplicate/stale messages. Failed speech retries reuse recognized text.
- Media is accessed only through short-lived, ownership-checked S3 URLs; audio is WAV. CSP allows only the exact Cognito/media endpoints, and media CORS allows only the deployed reader origin.
- `POST /api/books/{book_id}/completions` (`reader/write`) records a full slideshow reading for the authenticated reader. Send `{"completion_id":"<UUID>"}` and reuse that ID on retries. The response contains `read_count`, also included in book listings when nonzero.
- `PATCH /api/books/{book_id}` (`reader/write`) accepts `{"name":"New title"}` with the same validation as book creation. It returns 204 without changing pages or cover.
- `GET /api/books` (`reader/read`) returns `{"items":[{"id":"earlier","name":"Earlier captures"}, ...]}`. `POST /api/books` (`reader/write`) accepts `{"name":"My book"}` (1–200 characters after trimming) and returns the new `{id,name}` with status 201. Named books belong to the JWT subject across runs; their metadata does not expire.
- `POST /api/readings/{job_id}?book_id=…` assigns the capture on submission; missing or foreign named books return 404. Omitting `book_id` or selecting `earlier` submits an unassigned capture. `GET /api/readings?book_id=…` lists playable captures in that book; `earlier` includes legacy unassigned captures without migration. Omitting the filter lists all books together. Pass `next_cursor` as `cursor` with the same filter until it is null. Results are newest first, with stable ties across pages; `created_at` remains Unix seconds. Named-book queries use the sparse `BookCapturesIndex`; wait for it to become ACTIVE before enabling named-book clients.
- `POST /api/books/{book_id}/cover` (`reader/write`) accepts `{"run_id":"<capture run UUID>","page_id":"<capture UUID>"}` with the library `X-Reader-Run` header and returns the updated book (200). The page must belong to that book and owner and have a current thumbnail; otherwise the request returns 404 without replacing the cover. Book listings resolve the optional `cover: {run_id,page_id,thumbnail_url}` on each read; an unavailable cover is omitted. Only the selection persists, not the URL or a copy of the image. In cloud mode the JWT identifies the owner, so the capture run may differ from the library run.
- In local mode use `/api/local/books` and `/api/local/readings` with `X-Reader-Run` on every request. Names persist per run in `.local/books.sqlite3` (override with `READER_LOCAL_BOOKS_PATH`); local capture media still expires after an hour or on restart.
- Unfiltered `GET /api/readings` queries `OwnerCapturesIndex` (JWT subject + numeric `createdAt`) newest first. Existing jobs have both attributes, so DynamoDB can populate the index without rewriting jobs. Wait for the index to become ACTIVE before enabling the route. The index projects state, not narration or audio. The API rechecks each candidate against the current base row for discard/expiry before returning replay metadata; pages of up to ten entries include private thumbnail links when available.
- Maintenance runs every five minutes to repair stranded jobs, expire uploads/captures, and repeat media cleanup after cancellation. Cancellation tombstones remain at least 24 hours. Incomplete work remains temporary. New READY captures retain the full-size submitted page, thumbnail, text and audio until explicitly deleted. Review fetches the original on demand through the owner-checked `GET /api/readings/{job_id}/original` route. Previously discarded originals cannot be recovered. Older, already-expired recordings cannot be restored.
- Per-owner UTC-day limits: 200 capture reservations/retries, 200 vision attempts, 200 speech attempts including chunks/retries. State survives Lambda restarts. These are request limits, not dollar spending caps.
- SQS processing is capped at two concurrent worker invocations. This AWS account currently has a total Lambda concurrency quota of **10**; reserved worker concurrency was rejected by AWS because it would reduce the minimum unreserved pool. The corrected configuration uses SQS maximum concurrency without reserving account-wide capacity or changing the account quota.

## Verification

Baseline check: Python tests/types/lint, frontend and PKCE/API-client tests, CDK assertions, production build, ARM64 packaging, and four-stack synth. Synthetic cloud checks use their own temporary accounts/capture and do not upload the user's book photos:

```bash
uv run --locked python -m scripts.smoke_cloud --live
```

This check makes billable provider calls. It verifies managed OAuth/PKCE, missing/foreign-owner access, upload -> stream/SQS worker -> exact synthetic OCR -> private WAV -> replay -> discard, exact-origin media CORS, and cleanup. Passwords, tokens, signed URLs, and provider keys are not printed. Minimal cancellation tombstones remain until TTL, even after synthetic media and users are removed.

Verified on 2026-11-20: all four stacks reached successful states; public health returned 200 and unauthenticated config returned 401. The final isolated smoke passed both managed PKCE logins, cross-owner denials, exact synthetic recognition, private 205,484-byte WAV/replay, exact-origin CORS, and discard. Capture-to-ready was 13.308 seconds for that single sample, not a performance distribution. Temporary accounts and synthetic media were removed; cancellation drops recognized text/checkpoints immediately while retaining non-content fencing tombstones. A real invocation of the scheduled maintenance function also passed. The adult's first password change and SNS subscription confirmation are intentionally left to the owner.

See [operations](operations.md) for recovery, alerts, rotation, cost controls and rollback. Real-device camera/audio acceptance, voice listening, long/complex-book quality, and production release gates remain pending.
