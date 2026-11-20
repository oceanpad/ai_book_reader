# M1 evaluation status

Status: **partial technical/Chinese rotation verification; not quality-certified**. Local manual capture -> structured vision -> speech -> playback is implemented. The original English spread passed exact-text OCR and live speech generation; the updated recognition path also passed 24 Chinese variants and one blur/retake fixture. The deployed reader now uses OpenRouter `google/gemini-3.5-flash-lite` for recognition; narration remains `google/gemini-3.8-flash-tts` with `Aoede`. After the switch, the deployed synthetic English spread matched exactly and private audio was ready in 18.414 seconds end to end. Earlier four-view Chinese results below concern the prior model, not certification of Gemini on real books. No shared real-book photos were submitted during development, no three-voice audition was performed, and target-mobile acceptance remains pending.

## Plain-text OCR benchmark (2026-11-20)

The standalone [benchmark](../scripts/ocr/benchmark.py) compares one image per request, not the production four-view recognition pipeline. Four original Chinese sentences were rendered at 0°, 90°, 180°, and 270° respectively (55 reference characters total). No personal photos were uploaded. All 12 calls completed.

| Route | Exact | Character error rate | Median seconds | USD/image |
| --- | ---: | ---: | ---: | ---: |
| OpenRouter `qwen/qwen3.8-flash` | 4/4 | 0% | 2.25 | 0.000237 |
| OpenAI `gpt-6.1-terra` | 4/4 | 0% | 1.96 | 0.001791 |
| OpenAI `gpt-6-luna` | 0/4 | 34.5% | 2.48 | 0.000179 |

Qwen is the first budget candidate to test on a larger labeled set; Terra had the lowest observed median. These are single observations per image, not production speed or quality guarantees. Cost uses OpenRouter's returned usage cost or a standard-tier OpenAI token estimate, not an invoice. Rates: [Qwen](https://openrouter.ai/qwen/qwen3.8-flash), [Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra), [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna).

**Image budget (after the run above):** The API and benchmark now use the same preprocessing: honor orientation, cap the longest edge at 1,600 pixels without upscaling, then encode each view to **strictly less than 500,000 bytes**. Small views can stay lossless; larger ones use JPEG with lower quality or further downscaling as needed. The API still sends four orientations per capture; the benchmark sends one view and records its encoded `image_bytes`. This limit is before base64 and is per view, not per request. Browser upload limits and originals on disk do not change. Model charges depend on dimensions/detail and provider tokenization, not JPEG bytes alone. The accuracy and cost numbers above predate resizing; remeasure on labeled pages, especially small print.

**Post-resize OpenRouter comparison:** On the same four synthetic images, [Gemini 3.5 Flash-Lite](https://openrouter.ai/google/gemini-3.5-flash-lite) and Qwen 3.8 Flash each returned 4/4 exact (0% CER). Gemini had lower median latency (1.72 s vs 2.82 s); Qwen had lower reported mean cost ($0.000207 vs $0.000350 per image). Both are now benchmark defaults. This one-pass comparison is not a production latency distribution or a real-book accuracy test.

From the repository root with `OPENAI_API_KEY` and `OPENROUTER_API_KEY` in the environment or local ignored config, run:

```bash
uv run python -m scripts.ocr.benchmark --live --font '/System/Library/Fonts/Supplemental/Songti.ttc' --limit 4
```

For permitted photos, use `--images /path/to/labeled-images` instead of `--font`. Provide a UTF-8 reference alongside every JPEG/PNG (`page01.jpg`, `page01.txt`); convert HEIC locally first. Only image pixels and the fixed prompt are sent, not references. Use `--models provider:model ...` to select routes and `--repeat N` for repeated timings. Calls = selected images × models × repeats. The default JSON report at `.local/ocr-benchmark/results.json` is replaced each run; use `--report` to keep separate runs. It records per-call usage, elapsed time and errors, plus summaries. CER is total character edits divided by total reference characters, ignoring whitespace but preserving punctuation. CER and latency percentiles exclude failed/incomplete requests; always compare completion counts as well. Missing cost is null, not zero.

## Plan review (2026-11-19)

- The asynchronous worker architecture is appropriate; a health/config Lambda is not that worker. Keep API and model calls separate.
- The [OpenRouter model listing](https://openrouter.ai/google/gemini-3.8-flash-tts) documents `POST /api/v1/audio/speech` and generally lists PCM/MP3. **The live Gemini route rejected MP3 with HTTP 400: `Gemini TTS only supports response_format="pcm"`.** The adapter now requests PCM and losslessly wraps signed 16-bit little-endian mono samples as 24 kHz WAV for the browser. Existing valid WAV is preserved rather than double-wrapped. Account access worked for the synthetic sample; maximum input/output limits are not certified. Format requests must follow observed routed-model support, not only the general endpoint schema.
- The [OpenAI speech guide](https://developers.openai.com/api/docs/guides/text-to-speech) confirms the plan's January 6, 2027 shutdown warning for `tts-1`, `tts-1-hd`, and `gpt-4o-mini-tts`. Its replacement uses a different Realtime workflow, not a drop-in Speech API model. Do not implement against a retiring baseline.
- Keep the conservative visual identity rule: identical text alone must never discard a visually different page. Deduplication is not implemented in this manual evaluation path.
- The local prototype is intentionally unauthenticated and loopback-only. Do not carry its authentication bypass into deployed domain routes.

## Evidence still required

| Gate | Evidence required | Result |
| --- | --- | --- |
| Fixtures | At least 20 labeled permitted/synthetic images and same/different-page pairs | 25 Chinese orientation/blur fixtures generated; full mixed-content and duplicate-pair corpus pending |
| Vision | Pinned model, schema/prompt versions, per-fixture output, readable English word accuracy >=95%, no accepted non-books or fabricated text | Pending |
| Picture-only/unreadable | Grounded descriptions versus explicit retake outcomes for every fixture | Pending |
| Image settings | Actual encoded dimensions/size and OCR quality, including small/curved/rotated text | Pending |
| Voice | At least three supported adult voices, prose/dialogue/names, fidelity, supported launch languages | Pending |
| Speech workflow | Model access, exact endpoint, audio format, text/chunk limits, timeouts, ordered chunk playback | Live access/PCM-to-WAV and single-chunk desktop decoding passed; full limits/fidelity/mobile gate pending |
| Devices | Exact iOS Safari and Android Chrome device/browser versions; permission/background/playback/network checks | Pending |
| Performance | Stage latency, warm/cold distributions, provider usage and estimated per-page cost | Pending |

Browser checks with a synthetic camera can validate crop mechanics and lifecycle recovery, but cannot establish OCR accuracy, narration quality, real camera stability, mobile compatibility, or paid-provider availability. Keep M1 unchecked until those measurements exist. The current crop/quality thresholds are provisional.

## Chinese recognition correction (2026-11-19)

The reported capture had sideways, small Chinese text accepted with incorrect substitutions. The prior single easy English fixture was insufficient evidence. Only original, locally rendered Chinese text was submitted for the following comparisons; the user's book screenshot was not uploaded.

| Recognition path | Model | Identical six Chinese cases | Character edits / expected characters |
| --- | --- | --- | --- |
| v2, one image | `gpt-4.1-mini` | 3/6 exact | 40/84 |
| v2, one image | `gpt-5.4-2026-03-05` | 2/6 exact | 20/84 |
| v3, four orientations in one request | `gpt-4.1-mini` | 6/6 exact | 0/84 |
| v3, four orientations in one request | `gpt-5.4-2026-03-05` | 6/6 exact | 0/84 |

- A larger model **alone did not solve the problem**. Retained the existing lower-cost runtime model and corrected the input pipeline: include 0/90/180/270-degree views, instruct selection of the upright view and transcription exactly once, and retain the validated readable/picture-only/retake result union.
- Final v3 uses exact pixel transposes and lossless PNG for rotated images, not another lossy JPEG encoding. Server JPEG metadata sanitization now uses 4:4:4 chroma sampling. Original capture dimensions are preserved; this does not create detail missing from the camera.
- Expanded live check: **24/24 exact Chinese transcriptions, zero edits across 330 expected characters**, and **1/1 unreadable blurred page rejected**. These are six variants of each of four original sentences, not 24 independent books: rotations 0/90/180/270 plus 85/275-degree tilted variants, 22-30 pixel Songti text, and brightness reduced to 55% for difficult cases. Base images are 1,728 x 810 before rotation. This limited corpus cannot certify general Chinese recognition.
- Four orientations increase image tokens and payload, but remain a single vision API call/attempt. There is no hidden second-model fallback or extra vision call during speech retry. Real-book errors, glare, severe perspective, very small strokes, traditional/vertical text, and handwritten/stylized characters remain to be evaluated.
- Final regression checks passed: 36 Python tests and 4 TypeScript/CDK tests, lint/type checks, frontend build, Linux ARM64 packaging, and four-stack synth. The existing English live smoke also passed with v3 (exact text, 4.170-second vision stage, 3.374-second speech stage, 193,964-byte WAV). These timings are individual samples, not performance distributions.
- Reproduce the current path with `scripts/evaluate_vision.py --live --font <local-Chinese-font> --models gpt-4.1-mini`. Reports and generated fixtures live only under ignored `.local/vision-evaluation/`; the measured summary is recorded here before temporary outputs are cleaned up. M1 remains incomplete.

## Manual reading verification (2026-11-19)

- An initial permissive structured schema allowed inconsistent readable-page/description combinations; runtime validation correctly rejected them. `reader-vision-v2` now constrains a nested union of readable, picture-only, unreadable, and non-book results at schema generation time as well as validating runtime combinations. Refusals and incomplete results do not become narration.
- `scripts/smoke_providers.py --live` generated a 1,600 x 1,100 spread (72,277-byte JPEG) with original text: “A small green boat. It floats on a pond.” The final check preserved the words exactly, with vision taking 2.678 seconds and speech taking 3.982 seconds. It generated a 184,364-byte WAV. This is one sample, not a latency distribution or a listening-fidelity score. Provider-token usage/cost measurement remains pending.
- An additional **real local API** smoke test exercised `POST` -> background processing -> `GET` status -> WAV download -> `DELETE`. It returned `READY`, exact synthetic text, one 211,244-byte WAV chunk, and completed in 8.098 seconds. The temporary job was deleted and subsequently returned 404. This was not an AWS integration test.
- Browser playback used mocked job responses with the **actual generated WAV**: one audio element decoded 3.84 seconds of audio; a forced autoplay rejection exposed Play; Play/Pause/Replay worked, backgrounding kept playback paused on return, and Stop cleared the preview/player. The audio was muted during automated validation; voice quality was not auditioned. Mocked browser checks and live API checks are separate evidence, not a claimed full real-camera/browser/provider run.
- Focused automated cases cover original-language text, picture-only/retake results, schema/refusal/provider errors, malformed/oversized images, stripped metadata, idempotency/run isolation, cancellation of an in-flight call, discarded-ID fencing, bounded transient retries/quotas, speech checkpoint reuse, lossless PCM-to-WAV wrapping, invalid audio, and exact chunk reconstruction.
- Full validation passed with 35 Python tests and 4 TypeScript/CDK tests, lint/type checks, frontend production build, packaging, and four-stack synth. Adding Pillow required changing cross-installed Linux ARM64 wheels from manylinux2014 to manylinux 2.28, compatible with Python 3.12 Lambda's Amazon Linux 2023 runtime; packaged Pillow extensions were verified as ELF/aarch64, not macOS binaries. Actual Lambda execution remains untested.
- Local evaluation state remains single-process and temporary. The separately deployed private AWS path now adds durable jobs/quotas, fenced leases, scoped login and ownership checks; it still has no saved history, reading-ahead queue or cross-page deduplication. Do not mark full M2/M4 complete on the earlier local evidence.

## Private AWS integration verification (2026-11-20)

- Four stacks deployed to the user-confirmed AWS target; see [deployment](deployment.md) for the live URL and first login. Server provider credentials are in Secrets Manager, readable only by processing roles; no credential values were placed in browser configuration or templates.
- Final live check passed managed Cognito authorization-code/PKCE login with two isolated test users, missing/foreign-owner denials, JPEG upload, DynamoDB stream/SQS dispatch, recognition, private WAV download, replay and discard. The original synthetic English text matched exactly; one final sample became ready in 13.308 seconds with a 205,484-byte WAV. This is not a warm/cold latency distribution or a voice-quality audition.
- Synthetic media/test users were removed. Cancellation strips recognized text/checkpoints while retaining non-content fencing tombstones for at least 24 hours. A direct invocation of deployed maintenance passed. The adult's first password change and operational SNS subscription confirmation remain owner actions.
- Final source validation passed 47 Python tests and 9 TypeScript/CDK tests, types/lint, frontend build, Linux ARM64 packaging and synthesis. First account deployment exposed a Lambda concurrency quota of ten; the application now limits SQS processing to two without reserving account capacity. No unrelated stack or account quota was changed.
- Mobile acceptance, three-voice listening, full mixed-content/real-book quality, saved history, automatic detection and financial-budget setup remain pending. The deployment is a private manual reader, not completion of every release gate.

## Foundation verification (2026-11-19)

- `npm run check` passed: 17 Python tests, 4 TypeScript/CDK tests, Python/TypeScript type checks, lint/format checks, production frontend build, Linux ARM64 wheel packaging, and synthesis of four stacks. Native packaged extensions were identified as Linux ELF/aarch64, not macOS binaries. No deployment was performed.
- The loopback API health/config routes and Vite API proxy responded successfully without provider credentials.
- Integrated desktop browser: Electron 43.8.6 / Chromium 150.0.7871.250, macOS user agent. A generated 3,840 x 2,160 canvas camera produced a framed 2,560 x 1,200 JPEG of 31,126 bytes, within both configured limits. No real-book fixture or OCR assertion was involved.
- Synthetic permission denial showed recovery; backgrounding released tracks and required Continue; Stop discarded the preview; a camera request resolving after Stop released its late stream without restarting. No microphone was requested.
- Browser viewport checks at widths 320, 390, 768, and 1,280 pixels found no horizontal overflow after correcting the viewfinder's mobile sizing. These are desktop viewport checks, not mobile-device acceptance.
- Runtime npm audit reported zero advisories; the full development audit retains the CDK/minimatch issue documented in the README. Python tests emitted upstream Starlette/Mangum deprecation warnings, not test failures.
