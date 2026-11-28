# AI Book Reader — Actionable Implementation Plan

Updated: 2026-11-20, Asia/Singapore. Status: M0 complete; private manual capture -> recognition -> narration deployed to AWS and smoke-tested with managed login. Full M1 quality/voice/device gates and M2/M3/full release remain pending.

Build a browser app that recognizes a stable storybook page, extracts its words, and reads them aloud in a warm kindergarten-teacher voice. For a book page containing illustrations without text, generate a short, gentle description. Save accepted pages privately and read new captures after the current narration finishes.

The repository contains the React/TypeScript frontend, Python API, local and persistent AWS recognition/speech jobs, Cognito/PKCE login, shared contracts, tests, packaging/deployment commands, and four deployed CDK stacks. The reference project is `../kitty_cards_github/`; its patterns were inspected and reused with independent resources and an explicitly verified deployment account. See [README.md](README.md) for setup and current limitations. Saved sessions, ordered reading ahead, and cross-capture deduplication are not implemented yet.

Planning estimate: 8–12 focused engineering days. Reassess after the camera/model evaluation in M1. Check each milestone item only after completing it.

## 1. MVP decisions

These defaults allow implementation to start without blocking on optional preferences.

| Area | MVP decision |
| --- | --- |
| Account | One adult account in Cognito; public registration disabled; children use the signed-in reading screen. |
| Devices | Mobile Safari on iPhone/iPad and Chrome on Android; desktop Chrome for development/webcam use. Record the exact tested devices and browser versions. |
| Capture unit | One framed image containing a single page or an open spread. Default to spread mode; offer single-page mode. A spread is stored and narrated as one capture in the correct reading order. |
| Language | Preserve the printed language; no automatic translation. English is the initial acceptance baseline. Evaluate additional launch languages in M1 before claiming support. |
| Reading style | Preserve printed words. Use a warm, sweet adult female voice with clear pronunciation and gentle pacing. Apply teacher-style wording to illustration descriptions. |
| Picture-only pages | Describe visible illustrations in 1–3 short sentences. A valid description counts as narration and follows the same save/playback pipeline as OCR text. |
| Unreadable text | Ask for a clearer capture. Never treat unreadable text as absent text or invent missing words. |
| Duplicates | Skip automatic permanent storage and playback of the same page within a session. Repeated words on a visually different page are valid. Explicit Replay remains available. |
| Reading ahead | Capture/analyze while narration plays. Preserve capture order. Allow at most three unresolved or unplayed captures ahead of the current narration; pause capture when full. |
| History | Save accepted images, text/descriptions, and audio. Support reopening a session, explicit replay, and session deletion. |
| Hosting | AWS region `ap-southeast-1` by default. Verify the configured AWS account/profile during deployment setup. |
| Providers | OpenAI vision. Prefer `google/gemini-3.8-flash-tts` via OpenRouter for speech if a key is available; otherwise validate the supported OpenAI speech workflow in M1. Pin the selected models and voice in server configuration. |

Defer public registration, separate child profiles, public book sharing, offline OCR/TTS, native apps, voice cloning, translation, word highlighting, and custom computer-vision model training.

## 2. Architecture and responsibilities

~~~mermaid
flowchart LR
    Browser[React browser] --> CF[CloudFront]
    CF --> Web[Private S3 frontend]
    Browser --> Auth[Cognito managed login]
    CF --> API[HTTP API + API Lambda]
    Browser -->|Short-lived signed upload| Media[Private S3 media]
    API --> DB[DynamoDB]
    DB -->|Capture state stream| Dispatch[Dispatcher Lambda]
    Dispatch --> Queue[SQS + DLQ]
    Queue --> Worker[Processing Lambda]
    Worker --> Vision[OpenAI vision]
    Worker --> Speech[TTS provider]
    Worker --> DB
    Worker --> Media
    Worker --> Secrets[Secrets Manager]
    Schedule[EventBridge schedule] --> Maintenance[Maintenance Lambda]
    Maintenance --> DB
    Maintenance --> Queue
    Maintenance --> Media
~~~

- **Frontend:** camera permission, framing, motion/blur checks, submission, polling, ordered narration, saved history, and accessible controls. Provider credentials never reach the browser.
- **API Lambda:** authentication, ownership, quotas, capture reservation/sequences, signed uploads/downloads, job submission/retry, and session management. It never waits for a model response.
- **Stream dispatcher:** send a job when a capture enters `QUEUED` with a new attempt number. This avoids committing a job and then losing a separate API queue-send operation. Retry failures and configure a failure destination.
- **Processing Lambda:** validate image bytes, perform vision, decide duplicates, generate audio, and checkpoint results. Use conditional writes and attempt/lease tokens to reject stale work.
- **Maintenance Lambda:** repair stranded jobs, expire abandoned uploads, finish deletion, and remove orphan media. Run every five minutes initially.
- **S3:** separate the frontend bucket from the private media bucket. Media prefixes are `incoming/`, `pages/`, and `audio/`. No public media access.
- **CloudFront:** cache fingerprinted frontend assets; disable caching on `/api/*`. Private media uses short-lived S3 URLs issued after ownership checks. Configure CSP and S3 CORS for the exact deployed origins and required upload/media operations.

HTTP API integrations have a maximum 30-second timeout. Model calls therefore run in the worker. [AWS HTTP API quotas](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-quotas.html)

Use four CDK stacks: `ReaderDataStack` (table, media, queues), `ReaderAuthStack` (Cognito), `ReaderApiStack` (API, functions, schedule, logs/alarms), and `ReaderWebStack` (frontend bucket/distribution). Keep the worker outside a VPC unless a demonstrated requirement appears.

## 3. Capture, recognition, and narration contracts

### 3.1 Camera and stability

- Start camera/audio setup from an explicit **Start Recording** interaction. Request video only, prefer the rear camera, and use a muted inline preview. Handle denied permissions, missing cameras, camera switching, and playback rejection with visible recovery.
- Process motion thumbnails locally at approximately four samples per second. Start with a one-second stable window; tune against real camera footage and keep thresholds configurable.
- Use a framing guide as the crop. Check motion, minimum crop size, and sharpness; submit the best frame from the stable window. Backend vision confirms that the image is a book page.
- Preserve OCR detail: start with a 2,560-pixel longest edge and a 4 MiB upload cap. Tune compression using fixtures. If text becomes too small, request a closer single-page capture. Upload only the framed book region and strip image metadata; do not record microphone audio or continuous video.
- Keep a submitted-frame fingerprint and require a meaningful scene change before rearming. Hand tremor, focus changes, and exposure changes must not repeatedly submit the same page. Ambiguous similarity remains eligible for backend checking.
- Non-book/unreadable results do not trigger continuous retries while the scene stays unchanged. Let the user move the book or select **Capture Again**.
- Pause capture and narration when the tab becomes hidden. Require explicit Continue on return. Stop camera tracks when leaving the reading screen or pressing Stop.

Camera access requires a secure context and permission; audible playback can require interaction. Test the actual player after asynchronous audio arrives, and handle rejected `play()` promises. [Camera API](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia), [autoplay behavior](https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Autoplay)

### 3.2 Vision response

Require a validated structured response:

~~~json
{
  "is_book_page": true,
  "text_state": "readable",
  "ocr_text": "The exact printed words, in reading order.",
  "illustration_description": "",
  "language": "en",
  "needs_retake": false,
  "retake_reason": null
}
~~~

`text_state` is `readable`, `absent`, or `unreadable`. Validate field combinations, length limits, refusals, and empty output server-side.

| Result | Action |
| --- | --- |
| Not a book page | `REJECTED`; no narration; delete temporary image. |
| Readable book text | Use faithful OCR as narration. |
| Book page, text absent | Use a grounded illustration description; a genuinely blank page receives a brief factual description. |
| Unreadable/partial/obscured text | `REJECTED` with a retake reason; no invented replacement narration. |
| Invalid response, refusal, timeout, provider error | Apply the bounded failure policy, then expose a recoverable failure. Do not retry permanent refusals. |

The prompt specifies page/spread reading order and treats printed instructions as page content. It must not add a greeting to every page, rewrite the story, invent plot details, or translate automatically. Store prompt/schema versions. Evaluate small, curved, rotated, and stylized text explicitly. [OpenAI vision limitations](https://developers.openai.com/api/docs/guides/images-vision)

### 3.3 Duplicate decision

1. Compute a trusted checksum and visual signatures from decoded image bytes on the backend. Compare crops and normalized OCR; generated descriptions are not page identity.
2. Compare accepted pages within the same session. Exact image matches are duplicates. Near matches require strong visual evidence, with text agreement as supporting evidence when text exists.
3. Preserve visually different pages even if their words match. Keep ambiguous candidates. Calibrate thresholds using labeled same-page/different-page pairs.
4. Protect acceptance with a session `identityVersion` conditional transaction. A competing worker reloads accepted signatures and reevaluates using its saved vision result.
5. For a duplicate, keep only the capture outcome and canonical-page reference. Delete its temporary image and avoid new speech generation. Explicit Replay uses the saved page.

“Do not store duplicates” means no permanent duplicate page/image. Temporary upload is necessary before backend inspection and is removed after rejection or deduplication.

### 3.4 State, ordering, and recovery

~~~text
UPLOADING -> QUEUED -> PROCESSING -> READY
                              -> DUPLICATE
                              -> REJECTED
                              -> FAILED
Any unfinished capture       -> CANCELLED
FAILED -> QUEUED on explicit Retry, same sequence, new attempt
~~~

Store stage separately: `validate`, `vision`, `deduplicate`, or `speech`. Accepted pages have `AUDIO_PENDING`, `READY`, or `AUDIO_FAILED` status. Speech failure retains the recognized page.

- Reserve a monotonically increasing capture sequence before upload. Reuse capture ID and idempotency key after network retries. Reserve in capture order while allowing processing to overlap.
- Advance narration by sequence, never completion time. A ready third capture waits for the second to resolve.
- `DUPLICATE`, `REJECTED`, and `CANCELLED` advance silently. `FAILED` pauses advancement and offers Retry or Skip. Expired/failed uploads become terminal captures so they cannot leave an invisible queue gap.
- Pause affects narration; capture may continue until the three-capture limit. Skip bypasses the current narration or failed capture; accepted pages remain saved.
- Stop immediately stops camera/audio, clears the local automatic queue, changes the local session generation, and marks the server session `STOPPED`. Ignore stale callbacks and reject new automatic commits. Retain already accepted pages.
- On reload, offer Continue from the recorded completed/skipped sequence; never resume audio automatically. An interrupted page may replay explicitly.
- Serialize Replay through the same audio player. Refresh expired media URLs without regenerating audio.
- Checkpoint vision output and completed audio chunks. Cache audio within the owner/session using narration hash, provider, model, voice/settings, and prompt version. Respect explicit provider chunk limits; never silently truncate.
- Retry transient errors with backoff, at most three automatic attempts per failed stage. Provider calls can repeat after a crash before checkpointing; exactly-once billing is not guaranteed.
- Queue messages contain IDs and attempt numbers. Track per-stage retry counts separately. Ignore stale attempts/terminal jobs. Use expiring leases with fencing tokens so an older worker cannot commit over a recovered attempt.
- A Retry on a stopped session is an explicit page-recovery action, not a resumption of automatic capture. Allow it only for an existing saved page, and only while the session is not deleting; starting a new automatic reading run creates a new active session.

Event-source processing can deliver duplicates. Both dispatcher and worker must be idempotent. [DynamoDB stream processing](https://docs.aws.amazon.com/lambda/latest/dg/with-ddb.html), [SQS processing](https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html)

## 4. API and data design

All routes use `/api` and require a validated Cognito access token except a minimal health endpoint. Configure issuer/client validation and explicit `reader/read` or `reader/write` route scopes. Derive ownership from the token, never a supplied `userId`. Every page/capture route verifies its session owner. [JWT authorizers and route scopes](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html)

| Method and route | Responsibility |
| --- | --- |
| `POST /sessions` | Create an active session; request-key idempotency. |
| `GET /sessions` | Paginated history for the signed-in owner. |
| `GET /sessions/{sid}` | Session state, capture outcomes, pages, playback progress; paginate results. |
| `POST /sessions/{sid}/captures` | Reserve ID/sequence/quota; return a short-lived size-constrained S3 POST upload. |
| `POST /sessions/{sid}/captures/{cid}/submit` | Verify uploaded object, freeze expected checksum/metadata, commit `QUEUED`; return `202`. Repeated submission is idempotent. |
| `GET /sessions/{sid}/captures/{cid}` | Status, reason, or accepted page reference. |
| `POST /sessions/{sid}/captures/{cid}/retry` | Resume from saved stage with a new attempt and enforced quota. Missing input requires a fresh capture. |
| `GET /sessions/{sid}/pages/{pid}/media` | Issue short-lived image/audio URLs after ownership check. |
| `PUT /sessions/{sid}/progress` | Record completed/skipped playback progress without regression. |
| `POST /sessions/{sid}/stop` | Idempotently stop automatic work. |
| `DELETE /sessions/{sid}` | Mark `DELETING`, block work, asynchronously remove media/records; return `202`. |

Signed POST policies constrain upload size, key, and content type. The worker validates actual format, dimensions, checksum, and decoding bounds. Reserve unique object keys, verify bytes against the frozen checksum, and copy accepted images to deterministic final keys before publishing a ready page. [Boto3 signed POST policy options](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3/client/generate_presigned_post.html)

Use one on-demand DynamoDB table:

| Record | Key and important fields |
| --- | --- |
| Session | `PK=USER#<sub>`, `SK=SESSION#<sid>`; lifecycle, next sequence, identity version, progress, counts, timestamps. |
| Capture | `PK=USER#<sub>#SESSION#<sid>`, `SK=CAPTURE#<cid>`; sequence, job/stage attempt counters, automatic or explicit-audio-retry mode, stage, lease, expected image metadata, vision checkpoint, outcome, canonical page ID, timestamps. |
| Page | Same session partition, `SK=PAGE#<pid>`; source sequence, image/audio keys, text/description, language/type, signatures, provider/prompt versions, audio status. |
| Request receipt | Owner/session partition, `SK=REQUEST#<key>`; request hash, resource IDs, expiration. |

Add a maintenance index on work state/last-updated time for expired uploads, stranded queued work, stuck processing, and deleting-session tombstones. Keep binary content in S3. Handle pagination explicitly; playback uses sequence, not UUID ordering. Poll every second while jobs are active, back off when idle or offline, and stop polling closed sessions.

Initial configurable limits: three captures ahead, 100 accepted captures per session, 200 capture attempts per owner/day including retries, and 200 speech-generation attempts per owner/day. Reserve quota before provider calls, including recovery calls/chunks. Over-limit requests stop new paid work with a clear message. M1 determines safe narration/chunk size for the pinned provider.

Accepted pages remain until session deletion. Target removal of abandoned uploads after 24 hours through maintenance, with S3 lifecycle as a fallback; delete rejected/duplicate images promptly. Failed input remains retryable for at most 24 hours; logs retain 14 days. Use a non-versioned media bucket initially to simplify deletion. Retain deletion tombstones for at least 24 hours and beyond all outstanding upload URLs/worker leases. Conditional final writes and repeated cleanup must cover late uploads and workers already running when deletion starts.

## 5. Repository layout

~~~text
frontend/src/
  auth/                 # Cognito login, callbacks, sign-out
  camera/               # preview, crop, motion, sharpness, capture
  reader/               # session state, ordered queue, one audio player
  history/              # saved sessions, replay, deletion
  api/                  # typed contracts, polling, idempotent requests
backend/
  api.py                # FastAPI routes and Lambda adapter
  contracts.py          # validation and state enums
  sessions.py           # ownership, quotas, lifecycle, progress
  captures.py           # reservation, submit, retry, transitions
  recognition.py        # OpenAI adapter and versioned prompt
  deduplication.py      # comparison and acceptance transaction
  speech.py             # provider adapter, chunks, cache
  worker.py             # staged processing and checkpoints
  dispatcher.py         # DynamoDB stream -> SQS
  maintenance.py        # repair, expiry, deletion, orphan cleanup
  storage.py            # DynamoDB/S3 and local adapters
infra/lib/              # CDK stacks and infrastructure tests
scripts/                # dev, checks, build, deploy, bootstrap, smoke
tests/                  # domain, API, worker, queue tests
docs/                   # evaluation, deployment, operations
~~~

Use React + TypeScript + Vite, Python 3.12 + FastAPI/Pydantic, and TypeScript CDK. Package image dependencies for Lambda Linux ARM64; do not deploy macOS binaries. Local mode uses the same domain services with in-memory storage and an in-process dispatcher; deployed tests verify actual AWS integration.

## 6. Milestones

### M0 — Repository and configuration (0.5 day)

Dependencies: none.

- [x] Scaffold runnable frontend, backend, infrastructure, scripts, tests, and docs; add feature modules from the layout above as their milestones begin. No existing `dist/` was present; unrelated work was preserved.
- [x] Extend `.gitignore` beyond its current `node_modules` entry: ignore `.envrc`, swap files, environment/credential files, `.venv`, caches, builds, `cdk.out`, and deployment outputs.
- [x] Add a value-free configuration example. Keep credentials server-only.
- [x] Add dependency lockfiles, a local development command, and one check command for type checks, relevant tests, frontend build, and CDK synth.
- [x] Define shared contracts/states and configuration for camera thresholds, providers/voice, quotas, media limits, and queue size. Thresholds are provisional; quota enforcement and state transitions belong to later milestones.

Exit: a clean checkout can install, run a static reader screen and health endpoint, and pass baseline checks without credentials. Source/output/browser bundles contain no secret values.

### M1 — Camera, recognition, and voice evaluation (1–1.5 days)

Dependencies: M0 and available OpenAI credentials. The proposed OpenRouter path also needs an OpenRouter key; other work can continue while selecting speech.

- [x] Build a manual camera/crop prototype before automatic detection. Local-only JPEG preview; no uploads or provider calls. Synthetic desktop-browser checks cover framing/encoding, denied permission, Stop, and explicit Continue after backgrounding; real target devices remain pending.
- [ ] Assemble at least 20 labeled images: readable pages/spreads, identical words on different pages, picture-only pages, blurred/obscured text, and non-book scenes. Keep private real-book photos out of git; commit only synthetic or permitted fixtures.
- [x] Implement structured vision and validate every result. The local adapter uses a strict nested union for readable, picture-only, unreadable, and non-book outcomes; prompt version `reader-vision-v3` supplies all four orientations of the same capture in one request. Candidate `gpt-4.1-mini` passed 24 synthetic Chinese orientation/dim/tilt variants exactly and rejected one unreadable-text fixture. This is not real-book certification.
- [ ] Evaluate the complete fixture set and compare a more capable candidate if accuracy requires it; certify/pin the candidate only after passing the full gate.
- [ ] Measure upload size, OCR errors, processing latency, and provider usage. Choose resolution/compression from evidence.
- [ ] Audition at least three supported adult female voices using prose, dialogue, names, and intended languages. Pin the selected voice/settings.
- [ ] Verify speech endpoint, exact text fidelity, format, mobile playback, timeout, and input limits. Define sentence-boundary chunks if required and prefetch them in order. Local implementation now uses exact-text sentence-preferred chunks, bounded timeouts/retries, and PCM-to-WAV conversion verified against the live Gemini route; real-device playback, listening fidelity, and provider boundary evaluation remain pending.
- [ ] Record provider/model/voice, prompt/schema versions, limitations, and measured latency/cost in `docs/model-evaluation.md`.

Exit: no fabricated story text or accepted non-book scenes in the labeled set; at least 95% word accuracy on clearly readable English fixtures; every picture-only fixture receives a grounded description; unreadable fixtures request retakes. These are fixture targets. Record voice listening/playback results on both target mobile platforms.

### M2 — Manual end-to-end reading (1.5–2 days)

Dependencies: M1 chooses a working vision/speech combination; M0 contracts.

- [ ] Implement sessions, capture reservation/upload/submit, job status, and history with local adapters.
- [ ] Implement the worker, validated results, checkpoints, and deterministic media keys.
- [ ] Save accepted image/text/audio and canonical-page references. Speech retry reuses saved vision.
- [ ] Build preview, Capture, current narration text, progress, Pause/Continue, Skip, Replay, Stop, and recovery controls.
- [ ] Implement one player and a sequence-ordered queue, including failure barriers and expired URL refresh.
- [ ] Persist progress and require explicit Continue after reload.

Exit: capture A/B/C while A plays; force C to finish processing before B; narration remains A → B → C without overlap. Picture-only pages are saved/read. Speech retry avoids another vision call. Stop prevents late results from restarting playback.

### M3 — Automatic capture and deduplication (1.5–2 days)

Dependencies: M2 and M1 fixtures.

- [ ] Implement motion/blur checks, best-frame selection, scene-change rearming, and the three-capture bound.
- [ ] Implement conservative image/text comparison and conditional concurrent acceptance.
- [ ] Add framing, “hold steady,” retake, and queue-full states. Preserve manual Capture.
- [ ] Cover camera movement, focus/exposure changes, hand removal, repeated text, picture-only pages, rapid turns, and returning to a previous page.
- [ ] Verify pause/background/stop behavior against section 3.4.

Exit: a stationary page produces one submission during a 60-second hold after acceptance. Visually different pages with identical text are read. Picture-only duplicates are compared visually. Work ahead never exceeds the bound. Record missed captures/false duplicates from real-device footage before tuning.

### M4 — Authenticated AWS processing (2–3 days)

Dependencies: M2/M3 and configured AWS target. Infrastructure work can begin after M0.

Private manual-reader subset deployed at <https://dn9bjxhghgekj.cloudfront.net> in the explicitly confirmed account/region. This subset uses temporary owner/run jobs and a size-limited raw-JPEG API upload, not saved sessions, transactional cross-capture deduplication, or the future signed-upload API. See [deployment](docs/deployment.md) and [operations](docs/operations.md). Successful private deployment does not complete the entire M4/M5 release gate.

- [x] Implement the four CDK stacks and isolated resource names; reuse private S3/OAC, conditional-write, logging, and failure-queue patterns.
- [x] Configure Cognito managed login with authorization code + PKCE, public-client settings, disabled public signup, HTTP API JWT authorization, and `reader/read`/`reader/write` scopes. Include first-login password change and sign-out. Managed OAuth verified with isolated test accounts; the adult's first password change remains an owner action.
- [x] Add account bootstrap with invitation messages suppressed. Save the generated temporary password once to an ignored local file with mode `0600`; omit it from logs/outputs and provide checked removal after successful first login. [Cognito account provisioning](https://docs.aws.amazon.com/cognito/latest/developerguide/how-to-create-user-accounts.html)
- [x] Declare Secrets Manager containers in infrastructure and populate values with a server-side bootstrap script. Scope secret access to processing roles. Keep values out of CDK templates and command arguments.
- [ ] Implement DynamoDB/S3 persistence, stream dispatch, SQS workers, leases, transactional deduplication, maintenance repair, and asynchronous deletion.
- [x] Set worker/provider timeouts using measured prototype behavior; configure SQS visibility, partial-batch failure handling, failure destinations, and DLQs. Worker timeout is ten minutes with 60-minute SQS visibility; provider calls have bounded wall time. Full long-page performance evaluation remains pending.
- [x] Configure signed media access, strict CORS/CSP, and disabled API caching. Check ownership on every status/media/mutation route. Verified against missing/foreign-owner tokens in the deployed manual API.
- [ ] Enforce quotas and usage limits. Log job IDs, stage timings, and usage without page text, raw images, credentials, tokens, or signed URLs.
- [ ] Alarm on function errors, stuck jobs, dispatcher failures, queue age, and DLQs. Configure cost alerts and record their notification destination.
- [x] Deploy the initial private manual environment for AWS integration checks. Its isolated live smoke passed login -> upload -> stream/SQS worker -> exact synthetic OCR -> private speech -> replay -> discard. Operational SNS confirmation, financial budgets, saved sessions/deduplication, and remaining release checks are still pending.

Exit: login/password change work; missing/foreign tokens cannot access another owner's data; media is private. Committed jobs survive dispatcher failure. Duplicate messages create one canonical page. Old attempts cannot overwrite newer work. Session deletion during processing cannot recreate pages/media.

### M5 — Verify, deploy, and hand over (1–1.5 days)

Dependencies: M4. Completion requires all release checks below.

- [ ] Run the check command, Linux ARM64 packaging, production build, and CDK synth/diff. Inspect resource changes and frontend configuration.
- [ ] Verify account/region; deploy validated updates, populate any missing secrets, publish the frontend, and verify the adult account setup.
- [ ] Run a deployed smoke script using only its own temporary session; verify upload → recognition → speech → replay → delete and clean up.
- [ ] Complete Safari/Android camera/audio checks, including permission denial, orientation, backgrounding, expired login, and network loss.
- [ ] Read at least three real books, including repeated text and picture-only pages. Record limitations and tune against recorded failures.
- [ ] Write `docs/deployment.md` and `docs/operations.md`: URL, local commands, secret rotation, account recovery, alarms, quotas, job recovery, deletion, rollback, and redeploy. Exclude credentials.

Exit: the private deployed app passes release checks, the owner can sign in/read books, temporary test data is removed, and recovery/rollback steps are documented.

## 7. Release checks and measurements

| Check | Required result |
| --- | --- |
| Stable page | One submission while a page stays still for 60 seconds after acceptance. |
| Page turn | Each held, readable page in the test sequence is captured. |
| Repeated words | Visually different pages with identical words are both saved/narrated. |
| Duplicate picture-only page | No extra permanent page/image/audio; Replay reuses saved content. |
| Ordering | A/B/C remain in capture order under reordered responses. |
| Quality | Unreadable text requests retake; readable text is preserved; descriptions stay grounded. |
| Recovery | Upload/vision/speech failures have visible recovery and no invisible sequence gaps. |
| Duplicate delivery/crash | One canonical page; completed stages are reused. |
| Stop/background/reload | No stale automatic playback or overlapping players; Continue is explicit. |
| Deletion race | Tombstones prevent resurrection; media and records are eventually removed. |
| Ownership | Missing, expired, and other-owner tokens cannot read/mutate data or obtain media URLs. |
| Secrets | No credentials in git, frontend artifacts, logs, templates, or deployment outputs. |

Initial performance goals: recognize local stability within roughly 1.5 seconds of a page becoming still; warm reservation/status API p95 under one second; median stable-capture-to-first-audio-ready at most eight seconds and p95 at most twenty seconds for a short English page without deliberate narration backlog. Measure cold starts separately. Validate these goals; if missed, resolve provider/capture settings before launch and record the measured limitations.

Track unique pages, rejects, duplicates, OCR errors, retries, queue depth, stage latency, speech attempts, provider usage, and estimated cost per accepted page. Estimate session cost from measured page usage and current provider prices. Request limits constrain usage; delayed budget alerts are not a hard spending cap.

## 8. Model notes and remaining inputs

- `google/gemini-3.8-flash-tts` is listed on OpenRouter. M1 must verify this account's access, voice configuration, text fidelity, and latency. [Model listing](https://openrouter.ai/google/gemini-3.8-flash-tts), [audio documentation](https://openrouter.ai/docs/guides/overview/multimodal/audio)
- OpenAI currently lists `tts-1`, `tts-1-hd`, and `gpt-4o-mini-tts` for shutdown on January 6, 2027, and describes a different Realtime API workflow for its suggested replacement. Avoid those retiring models as this project's production baseline. Validate the supported replacement in M1 if selecting OpenAI speech. [OpenAI speech documentation](https://developers.openai.com/api/docs/guides/text-to-speech)
- Show a concise “AI-generated voice” disclosure near voice/settings information. [OpenAI disclosure requirement](https://developers.openai.com/api/docs/guides/text-to-speech)
- Remaining inputs: working credentials for the chosen speech provider, actual target mobile devices, and any launch language beyond English. Resolve these in M1; they do not prevent M0.

Next implementation task: complete real-device/language and voice evaluation before implementing M2's saved/ordered pipeline. The compact local/private-cloud manual reader processes one page at a time with spinner feedback, speech checkpoint retry, Pause/Play/Replay and optional Discard; there is no Stop button. AWS jobs, authentication and private media are deployed, but saved sessions, the three-ahead queue and cross-capture deduplication are not. Synthetic cloud integration checks do not establish complete M1 quality or production-release gates; see [docs/model-evaluation.md](docs/model-evaluation.md) and [deployment](docs/deployment.md).
