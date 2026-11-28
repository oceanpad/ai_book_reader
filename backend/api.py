import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from backend.cloud import CloudStore
from backend.config import FileSettings, Settings
from backend.contracts import (
    Book,
    BookCompletion,
    BookCoverRequest,
    BookCreate,
    Books,
    BookUpdate,
    CaptureState,
    Health,
    HomeworkEntry,
    HomeworkHistory,
    NarrationEdit,
    PublicConfig,
    ReadCount,
    ReadingJob,
    RecentCaptures,
)
from backend.homework import CloudHomework, LocalHomework, read_submission
from backend.local_reader import LocalError, LocalReader
from backend.providers import Providers

RunId = Annotated[UUID, Header(alias="X-Reader-Run")]


def has_scope(jwt: dict[str, object], scope: str) -> bool:
    # API Gateway verifies the same pool issuer and browser client for both token flows.
    scopes = jwt.get("scopes", [])
    claims = jwt.get("claims", {})
    return isinstance(scopes, list) and (
        scope in scopes
        or (
            "aws.cognito.signin.user.admin" in scopes
            and isinstance(claims, dict)
            and claims.get("token_use") == "access"
        )
    )


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    config = settings if settings is not None else FileSettings()
    client = httpx.AsyncClient(follow_redirects=False, transport=transport)
    reader = LocalReader(config, Providers(config, client))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await reader.close()
            await client.aclose()

    app = FastAPI(
        title="AI Book Reader", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.reader = reader
    cloud_store: CloudStore | None = None

    def cloud(request: Request, scope: str) -> tuple[CloudStore, str]:
        nonlocal cloud_store
        jwt = (
            request.scope.get("aws.event", {})
            .get("requestContext", {})
            .get("authorizer", {})
            .get("jwt", {})
        )
        owner = jwt.get("claims", {}).get("sub")
        if not owner:
            raise HTTPException(status_code=401, detail="Sign in to read this capture.")
        if not has_scope(jwt, scope):
            raise HTTPException(
                status_code=403, detail="This account is missing the required reading scope."
            )
        if not config.table_name or not config.media_bucket:
            raise HTTPException(
                status_code=503, detail="Cloud reading resources are not configured."
            )
        if cloud_store is None:
            cloud_store = CloudStore(config)
        return cloud_store, str(owner)

    @app.exception_handler(LocalError)
    async def local_error(request: Request, exc: LocalError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if config.mode == "local" and request.url.path.startswith("/api/local/"):
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin is not None
                and origin
                not in {
                    "http://localhost:5173",
                    "http://127.0.0.1:5173",
                    "http://localhost:8000",
                    "http://127.0.0.1:8000",
                }
            ):
                return JSONResponse({"detail": "Cross-origin local reading is not allowed."}, 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/health", response_model=Health)
    def health() -> Health:
        return Health()

    @app.get("/api/config", response_model=PublicConfig)
    def public_config(request: Request) -> PublicConfig:
        if config.mode == "aws":
            jwt = (
                request.scope.get("aws.event", {})
                .get("requestContext", {})
                .get("authorizer", {})
                .get("jwt", {})
            )
            if not jwt.get("claims", {}).get("sub") or not has_scope(jwt, "reader/read"):
                raise HTTPException(status_code=401, detail="Sign in with the reader/read scope")
        return config.public_config()

    if config.mode == "local":
        homework = LocalHomework(reader)

        @app.post("/api/local/homework", status_code=202)
        async def local_submit_homework(request: Request, x_reader_run: RunId) -> HomeworkEntry:
            entry, images = await read_submission(request)
            return await homework.submit(str(x_reader_run), entry, images)

        @app.get("/api/local/homework")
        def local_homework_history(
            x_reader_run: RunId, cursor: str | None = None
        ) -> HomeworkHistory:
            return homework.history(str(x_reader_run), cursor)

        @app.get("/api/local/homework/{entry_id}")
        def local_get_homework(entry_id: str, x_reader_run: RunId) -> HomeworkEntry:
            return homework.get(str(x_reader_run), entry_id)

        @app.delete("/api/local/homework/{entry_id}", status_code=204)
        def local_delete_homework(entry_id: str, x_reader_run: RunId) -> Response:
            homework.delete(str(x_reader_run), entry_id)
            return Response(status_code=204)

        @app.get("/api/local/books", response_model_exclude_none=True)
        async def local_books(x_reader_run: RunId) -> Books:
            return reader.books(str(x_reader_run))

        @app.post("/api/local/books", status_code=201, response_model_exclude_none=True)
        def local_create_book(x_reader_run: RunId, body: BookCreate) -> Book:
            return reader.create_book(
                str(x_reader_run), body.name, level=body.level, language=body.language
            )

        @app.post("/api/local/books/{book_id}/completions")
        def local_completion(book_id: str, x_reader_run: RunId, body: BookCompletion) -> ReadCount:
            count = reader.complete_book(str(x_reader_run), book_id, str(body.completion_id))
            return ReadCount(read_count=count)

        @app.post("/api/local/books/{book_id}/cover", response_model_exclude_none=True)
        async def local_cover(book_id: str, x_reader_run: RunId, body: BookCoverRequest) -> Book:
            return reader.set_cover(str(x_reader_run), book_id, str(body.run_id), str(body.page_id))

        @app.patch("/api/local/books/{book_id}", status_code=204)
        def local_update_book(book_id: str, x_reader_run: RunId, body: BookUpdate) -> Response:
            reader.update_book(str(x_reader_run), book_id, body)
            return Response(status_code=204)

        @app.delete("/api/local/books/{book_id}", status_code=204)
        async def local_delete_book(book_id: str, x_reader_run: RunId) -> Response:
            reader.delete_book(str(x_reader_run), book_id)
            return Response(status_code=204)

        @app.get("/api/local/readings", response_model=RecentCaptures)
        async def recent_readings(
            x_reader_run: RunId, cursor: str | None = None, book_id: str | None = None
        ) -> RecentCaptures:
            return reader.recent(str(x_reader_run), cursor, book_id)

        @app.post("/api/local/readings/{job_id}", status_code=202, response_model=ReadingJob)
        async def capture(
            job_id: UUID,
            request: Request,
            x_reader_run: RunId,
            mode: Literal["spread", "single"] = "spread",
            book_id: str | None = None,
        ) -> ReadingJob:
            available = config.public_config()
            if not available.recognition_available or not available.speech_available:
                raise LocalError(
                    available.setup_message or "Configure providers before reading.", 503
                )
            if request.headers.get("content-type", "").split(";")[0] != "image/jpeg":
                raise LocalError("Send the framed image as image/jpeg.", 415)
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > config.camera.max_upload_bytes:
                    raise LocalError("The captured image exceeds the upload size limit.", 413)
                data.extend(chunk)
            return reader.submit(str(x_reader_run), str(job_id), bytes(data), mode, book_id)

        @app.get("/api/local/readings/{job_id}", response_model=ReadingJob)
        async def reading(job_id: UUID, x_reader_run: RunId) -> ReadingJob:
            return reader.get(str(x_reader_run), str(job_id)).view

        @app.post("/api/local/readings/{job_id}/retry", status_code=202, response_model=ReadingJob)
        async def retry(job_id: UUID, x_reader_run: RunId) -> ReadingJob:
            return reader.retry(str(x_reader_run), str(job_id))

        @app.post("/api/local/readings/{job_id}/narration", status_code=202)
        async def edit_narration(
            job_id: UUID, x_reader_run: RunId, body: NarrationEdit
        ) -> ReadingJob:
            return reader.edit_narration(str(x_reader_run), str(job_id), body.narration)

        @app.get("/api/local/readings/{job_id}/original")
        async def original(job_id: UUID, x_reader_run: RunId) -> Response:
            job = reader.get(str(x_reader_run), str(job_id))
            if not (job.view.state == CaptureState.READY or job.edited) or not job.image:
                raise LocalError("No original image is available for this capture.", 404)
            return Response(
                job.image, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
            )

        @app.get("/api/local/readings/{job_id}/audio/{chunk}")
        async def audio(job_id: UUID, chunk: int, x_reader_run: RunId) -> Response:
            job = reader.get(str(x_reader_run), str(job_id))
            if job.view.state != CaptureState.READY or chunk < 0 or chunk >= len(job.audio):
                raise LocalError("Audio is not ready for this capture.", 409)
            return Response(job.audio[chunk], media_type="audio/wav")

        @app.delete("/api/local/readings/{job_id}", status_code=204)
        async def discard(job_id: UUID, x_reader_run: RunId) -> Response:
            reader.remove(str(x_reader_run), str(job_id))
            return Response(status_code=204)

    else:

        @app.post("/api/homework", status_code=202)
        async def cloud_submit_homework(request: Request) -> HomeworkEntry:
            store, owner = cloud(request, "reader/write")
            entry, images = await read_submission(request)
            return await asyncio.to_thread(CloudHomework(store).submit, owner, entry, images)

        @app.get("/api/homework")
        def cloud_homework_history(request: Request, cursor: str | None = None) -> HomeworkHistory:
            store, owner = cloud(request, "reader/read")
            return CloudHomework(store).history(owner, cursor)

        @app.get("/api/homework/{entry_id}")
        def cloud_get_homework(entry_id: str, request: Request) -> HomeworkEntry:
            store, owner = cloud(request, "reader/read")
            return CloudHomework(store).get(owner, entry_id)

        @app.delete("/api/homework/{entry_id}", status_code=204)
        def cloud_delete_homework(entry_id: str, request: Request) -> Response:
            store, owner = cloud(request, "reader/write")
            CloudHomework(store).delete(owner, entry_id)
            return Response(status_code=204)

        @app.get("/api/books", response_model_exclude_none=True)
        def cloud_books(request: Request) -> Books:
            store, owner = cloud(request, "reader/read")
            return store.books(owner)

        @app.post("/api/books", status_code=201, response_model_exclude_none=True)
        def cloud_create_book(request: Request, body: BookCreate) -> Book:
            store, owner = cloud(request, "reader/write")
            return store.create_book(owner, body.name, level=body.level, language=body.language)

        @app.post("/api/books/{book_id}/completions")
        def cloud_completion(book_id: str, request: Request, body: BookCompletion) -> ReadCount:
            store, owner = cloud(request, "reader/write")
            return ReadCount(
                read_count=store.complete_book(owner, book_id, str(body.completion_id))
            )

        @app.post("/api/books/{book_id}/cover", response_model_exclude_none=True)
        def cloud_cover(
            book_id: str, request: Request, body: BookCoverRequest, x_reader_run: RunId
        ) -> Book:
            store, owner = cloud(request, "reader/write")
            return store.set_cover(owner, book_id, str(body.run_id), str(body.page_id))

        @app.patch("/api/books/{book_id}", status_code=204)
        def cloud_update_book(book_id: str, request: Request, body: BookUpdate) -> Response:
            store, owner = cloud(request, "reader/write")
            store.update_book(owner, book_id, body)
            return Response(status_code=204)

        @app.delete("/api/books/{book_id}", status_code=204)
        def cloud_delete_book(book_id: str, request: Request) -> Response:
            store, owner = cloud(request, "reader/write")
            store.delete_book(owner, book_id)
            return Response(status_code=204)

        @app.get("/api/readings", response_model=RecentCaptures)
        def cloud_recent_readings(
            request: Request, cursor: str | None = None, book_id: str | None = None
        ) -> RecentCaptures:
            store, owner = cloud(request, "reader/read")
            return store.recent(owner, cursor=cursor, book_id=book_id)

        @app.post("/api/readings/{job_id}", status_code=202, response_model=ReadingJob)
        async def cloud_capture(
            job_id: UUID,
            request: Request,
            x_reader_run: RunId,
            mode: Literal["spread", "single"] = "spread",
            book_id: str | None = None,
        ) -> ReadingJob:
            store, owner = cloud(request, "reader/write")
            if request.headers.get("content-type", "").split(";")[0] != "image/jpeg":
                raise LocalError("Send the framed image as image/jpeg.", 415)
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > config.camera.max_upload_bytes:
                    raise LocalError("The captured image exceeds the upload size limit.", 413)
                data.extend(chunk)
            return store.submit(owner, str(x_reader_run), str(job_id), bytes(data), mode, book_id)

        @app.get("/api/readings/{job_id}", response_model=ReadingJob)
        async def cloud_reading(job_id: UUID, request: Request, x_reader_run: RunId) -> ReadingJob:
            store, owner = cloud(request, "reader/read")
            return store.view(store.get(owner, str(x_reader_run), str(job_id)))

        @app.post("/api/readings/{job_id}/retry", status_code=202, response_model=ReadingJob)
        async def cloud_retry(job_id: UUID, request: Request, x_reader_run: RunId) -> ReadingJob:
            store, owner = cloud(request, "reader/write")
            return store.retry(owner, str(x_reader_run), str(job_id))

        @app.post("/api/readings/{job_id}/narration", status_code=202)
        def cloud_edit_narration(
            job_id: UUID, request: Request, x_reader_run: RunId, body: NarrationEdit
        ) -> ReadingJob:
            store, owner = cloud(request, "reader/write")
            return store.edit_narration(owner, str(x_reader_run), str(job_id), body.narration)

        @app.get("/api/readings/{job_id}/original")
        async def cloud_original(
            job_id: UUID, request: Request, x_reader_run: RunId
        ) -> dict[str, str]:
            store, owner = cloud(request, "reader/read")
            return {"url": store.original_url(owner, str(x_reader_run), str(job_id))}

        @app.get("/api/readings/{job_id}/thumbnail")
        async def cloud_thumbnail(
            job_id: UUID, request: Request, x_reader_run: RunId
        ) -> dict[str, str]:
            store, owner = cloud(request, "reader/read")
            return {"url": store.thumbnail_url(owner, str(x_reader_run), str(job_id))}

        @app.get("/api/readings/{job_id}/audio/{chunk}")
        async def cloud_audio(
            job_id: UUID, chunk: int, request: Request, x_reader_run: RunId
        ) -> dict[str, str]:
            store, owner = cloud(request, "reader/read")
            return {"url": store.audio_url(owner, str(x_reader_run), str(job_id), chunk)}

        @app.delete("/api/readings/{job_id}", status_code=204)
        async def cloud_discard(job_id: UUID, request: Request, x_reader_run: RunId) -> Response:
            store, owner = cloud(request, "reader/write")
            store.cancel(owner, str(x_reader_run), str(job_id))
            return Response(status_code=204)

    return app


app = create_app()
