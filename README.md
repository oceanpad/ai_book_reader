# Little Pages / AI Book Reader

**Manual capture, text recognition, and read-aloud**, with local development and a private AWS deployment. The cloud version adds Cognito managed login, owner-bound DynamoDB/S3 jobs, SQS workers, and private capture history. This remains a private evaluation app, not the full production release: cross-capture deduplication, automatic capture, and comprehensive real-device/voice acceptance remain future milestones in the [implementation plan](AI_Book_Reader_Implementation_Plan.md).

## Local development

Requires Node.js 22.12+ with npm, Python 3.12, and [uv](https://docs.astral.sh/uv/). No AWS account is required. Camera preview works without credentials; recognition/read-aloud requires a server-only OpenRouter key with model access and credits.

```bash
npm ci
uv sync --locked
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api` to the API at `127.0.0.1:8000`. Both servers bind to loopback only. Stop with Ctrl+C; the launcher stops both child processes, including when either server fails. Keep these ports free. The launcher intentionally forces local mode, which has no authentication; do not expose it to a network.

From **My books**, select **Create new book**, enter a name, and choose **Create book** (or open an existing book). Select **Start Reading**, allow camera access, frame the page, then tap **Submit**. The live preview does not detect pages, sample frames, or upload images automatically. Only the tap freezes and submits one frame; status polling follows that submitted job. The framed JPEG is re-encoded without source metadata, sized to at most 2,560 pixels on the longest edge and 4 MiB, and sent to the local API. Gemini 3.5 Flash-Lite via OpenRouter recognizes the page; OpenRouter generates narration in the original language. The UI shows processing stages, extracted text, and retake/provider errors. Picture-only pages receive a short description; unreadable pages are rejected rather than invented.

Recognition uses `reader-vision-v3`: the server supplies four orientations of the **same capture** in one vision request, so sideways/upside-down Chinese is read from an upright view rather than guessed. Each view is capped at 1,600 pixels and under 500 KB before base64; server JPEG sanitization avoids chroma subsampling that can smear small characters. This increases image-token cost compared with a single view; it is still one provider attempt. The UI displays the OCR prompt version after a successful reading. For small print, move closer, fill the preview, keep all lines visible, and use even light. The 25-case synthetic Chinese check passed; it is not a guarantee for every real book or blurred image.

**My books** opens with the newest books first. Use **Sort by** to reverse creation order or rank books by most or fewest completed reads. Older books without a recorded creation time use their earliest saved page as an estimate; books with no date appear last in date order. Filter by title, open a book to capture pages, or select **Slide Preview** to listen from the beginning. **Reading guide** in the header explains capture, playback, and provider privacy without leaving the current book. The studio follows the system light or dark appearance.

The shelf shows **Completed reads** for each book. A read counts once when every page has been narrated in order and the last page finishes. Pausing is fine; skipping unread pages or closing early does not count. A full replay adds one more. Signed-in counts are private to your account and start at zero.

Select **Rename** beside the title on a book’s detail page, enter the new name, and choose **Save name**. The book keeps its pages and cover.

Under **Book pages**, expand **Page options** for the desired photo and select **Set as cover**. Its thumbnail appears next to the book name on **My books**. Choosing another page replaces the cover; deleting the cover page leaves the book without a cover.

At the bottom of a named book, expand **Manage book**, select **Delete book** and then **Confirm delete book** to permanently remove it and all its pages, including unfinished pages and their media. **Cancel** keeps the book. **Earlier captures** cannot be deleted as a book; its recordings can be removed individually.

After submitting a photo, select **Edit text** next to **Selected capture** to pause playback and correct the recognized words in place. For older pages, **Edit text** is also available in the slideshow. **Save and regenerate audio** generates a new recording from the edited text without scanning the image again. Tap ▶️ when you’re ready to listen.

The camera is off until Start. On phones, opening the camera brings the preview and Submit controls into view together, with space reserved for the browser toolbar. The compact preview has no crop guide; Submit includes the entire visible frame. Submit closes the preview and uploads one photo. Once the server accepts it, you can start the camera again or switch books without waiting for narration. Up to **10 pending pages per reader** (including the one processing) can be outstanding across books. If the queue is full, wait for a slot and retry. AWS dispatches accepted work to a single SQS FIFO message group. Book pages stay in submission order, even when a page needs a retry. The book’s page list shows queue status; leaving it does not cancel accepted work.

Each book lists its submitted pages, oldest first. If the slideshow reaches a pending page, it waits there rather than skipping ahead. Recordings made before books were introduced appear under **Earlier captures** only while that collection contains recordings. Select a page to open its image and full narration and start listening there. **Slide Preview** on the homepage or **Slide Play** inside a book starts at page one. On laptops and tablets, the full-screen slideshow centers the page within the left image area, with compact narration on the right and playback and editing controls at the bottom of that panel. On phones, the arrows sit midway up the image area. **Expand image** and **Show text & controls** change only the view: narration and automatic page turns continue unless paused. The slideshow waits two seconds after each page’s narration before playing the next and stops at the end of the book. Use ⬅️ or ➡️ to turn pages immediately, or ⏸️ to pause; if the browser blocks autoplay, tap Play. Select **Review** beside **Delete** to open the image at its submitted resolution without interrupting playback. New signed-in captures keep the full-size page, thumbnail, text and audio until deleted. Older recordings whose originals were discarded can still be replayed. Already-expired captures cannot be recovered. Replay does not make a provider call.

Both the image and narration text go to OpenRouter and its routed providers; their retention policies apply. The local server keeps temporary jobs/audio in memory, not permanent history. Discard removes them; jobs older than one hour are removed on the next local job request. Restarting the API or reloading the browser loses recovery state. Abrupt tab closure may leave temporary server data until expiry/restart. Cancellation cannot undo a provider call already billed.

HTTPS (or localhost) is required for cameras. Real Safari/iOS and Chrome/Android validation is pending. The browser's camera permission remains authoritative; the switch control requests the opposite facing direction but devices with one camera may keep the same camera.

## Configuration and contracts

Copy [.env.example](.env.example) to `.env` and set `READER_OPENROUTER_API_KEY`. Existing dotenv-style `.envrc` assignments such as `export OPENAI_API_KEY=...` and `export OPENROUTER_API_KEY=...` are also read **only by the API**, so existing local credentials need not be copied. Files remain git-ignored and are never modified. Shell code is not executed; computed shell values must instead be exported in the launching environment. Process environment overrides files; `.env` overrides `.envrc`, with empty values ignored. Never use `VITE_` for credentials. **Restart `npm run dev` after backend/configuration changes**, then reload the browser; Vite hot reload alone does not restart Python.

- [backend/contracts.py](backend/contracts.py) defines camera/limit settings, state enums, and validated vision field combinations. The numeric motion/sharpness defaults are starting hypotheses, not calibrated automatic detection.
- `npm run contracts` exports the serialized JSON schema and TypeScript types in [frontend/src/api/](frontend/src/api/). Regenerate after contract changes; do not hand-edit generated files.
- `GET /api/health` (not `/health`) returns minimal service health. `GET /api/config` exposes allowlisted camera settings, limits, provider-configuration availability flags, and an actionable setup message. Availability means configured, not that provider access/credits have been tested. Config requires a Cognito `reader/read` token on AWS, but not in loopback local mode.
- Local-only `POST /api/local/readings/{requestId}?mode=spread|single` accepts a raw JPEG and immediately returns `202`; `GET` polls state, `POST .../retry` resumes failures, `GET .../audio/{index}` returns WAV, and `DELETE` cancels/discards. Requests require a random per-tab `X-Reader-Run` UUID; the request UUID makes submission idempotent. Cross-origin browser requests are blocked. AWS instead registers JWT-scoped `/api/readings/...` routes and derives ownership exclusively from validated Cognito claims. Cloud media routes issue short-lived S3 URLs; access tokens are never forwarded to S3.
- `GET /api/readings` (`reader/read`) lists the signed-in owner’s READY captures newest first. Fetch additional pages with `?cursor=<next_cursor>` until `next_cursor` is null. Each item includes `id`, `run_id`, `created_at`, narration, language, chunk count, and a nullable private `thumbnail_url`. Replay and delete use the entry’s `X-Reader-Run` header. Local `/api/local/readings` lists only the current run, subject to its one-hour expiry and in-memory capacity.
- Evaluation candidates default to `google/gemini-3.5-flash-lite`, `google/gemini-3.8-flash-tts`, and `Aoede`. The live Gemini route rejected MP3; speech now requests signed 16-bit little-endian mono PCM and wraps it in WAV at `READER_SPEECH_PCM_RATE=24000`. Completed WAV responses are validated rather than double-wrapped. `READER_SPEECH_CHUNK_CHARACTERS=1000` bounds exact-text, sentence-preferred chunks without truncation. A separate delivery instruction requests gentle teacher-style narration, without adding directions to the spoken text. Three-voice audition, real-book quality, target-device/language validation, and listening fidelity remain pending. The OpenAI Realtime fallback is not implemented; selecting `openai` disables reading with a setup message instead of silently substituting a retiring model.
- Local limits: one unfinished capture per tab, two concurrent processing jobs, 16 retained jobs total, 16 MiB audio per job, and 200 vision/speech provider attempts each per UTC day, including retries/chunks. These process-memory counters reset on server restart; they are not durable quotas or a hard spending cap. Production quotas, accepted-page limits, session lifecycle, and durable fencing remain M2/M4 work.

## Checks

```bash
npm run check
```

Runs Python lint/format/type checks and tests; regenerates contracts; checks TypeScript; runs framing and CDK assertions; builds the frontend; packages Linux ARM64 Python wheels; and synthesizes all four CDK stacks. Packaging may need network access on its first run. No credentials, deployment, model calls, or AWS resource lookups are needed. Commit both dependency lockfiles and regenerated contracts when changing dependencies/contracts.

Useful subsets: `uv run --locked pytest -q`, `npm test`, `npm run typecheck`, `npm run build`. The full check writes only generated contracts and ignored build/cache outputs.

An optional **billable** provider smoke check generates an original synthetic spread, verifies exact OCR, requests speech, and saves JPEG/WAV/measurements under ignored `.local/provider-smoke/`. It never uses real-book photos and is not part of baseline checks:

```bash
uv run --locked python -m scripts.smoke_providers --live
```

Only this explicit synthetic-content CLI prints redacted provider diagnostics. The application logs job IDs/stages rather than provider bodies, book text, image bytes, or credentials.

The additional **billable** Chinese OCR evaluator generates four original sentences under upright, sideways, upside-down, dim, small-print, and tilted conditions, plus an unreadable-text retake case. It compares exact characters (including punctuation), without speech calls. Supply a local Chinese font; on macOS, for example:

```bash
uv run --locked python -m scripts.evaluate_vision --live \
  --font '/System/Library/Fonts/Supplemental/Songti.ttc' \
  --models google/gemini-3.5-flash-lite
```

Images/results go to ignored `.local/vision-evaluation/`. It never uploads the user's shared photos. The default follows the configured OpenRouter vision model.

## Private AWS deployment

**App:** <https://dn9bjxhghgekj.cloudfront.net> — adult sign-in required. See first-login instructions below; no passwords are printed here.

See [deployment and first login](docs/deployment.md) and [operations/recovery](docs/operations.md). Deployment requires explicit account/region selection, adult email, and AWS/provider charges. It verifies the selected account, reviews CDK changes, creates the private environment, configures exact callback/media origins, bootstraps secrets and the adult account without printing credentials, then builds/publishes the authenticated frontend.

- `ReaderDataStack`: retained on-demand table/stream/maintenance index, private non-versioned media bucket, processing queue/DLQ, server-only Secrets Manager container.
- `ReaderAuthStack`: adult pool with public signup disabled, managed authorization-code/PKCE login, public client and read/write scopes.
- `ReaderApiStack`: ARM64 API, stream dispatcher, fenced/checkpointed SQS worker, scheduled maintenance, 14-day logs, error/queue/DLQ alarms and an email SNS subscription requiring confirmation.
- `ReaderWebStack`: private frontend/OAC/CloudFront, uncached API/index, cached fingerprinted assets, scoped CSP and HTTPS security headers.

The private deployment processes one manual page at a time and keeps completed recordings until the owner deletes them. Daily per-owner request quotas persist across Lambda restarts. Operational alarms are **not financial budgets or spending caps**. The selected account has only 10 total Lambda concurrency, so SQS limits processing to two workers without reserved capacity. Review the deployment documentation rather than deploying an unauthenticated local server.

Dependency note: the current CDK release brings a moderate `minimatch` advisory (two entries in `npm audit`, including the parent package). npm offers no semver-compatible CDK fix. It is restricted to development infrastructure tooling, not the browser or Python runtime. Recheck before deployment; do not apply `npm audit fix --force` blindly.

Next: [model evaluation and remaining gates](docs/model-evaluation.md).
