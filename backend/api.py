from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from backend.cloud import CloudStore
from backend.config import FileSettings, Settings
from backend.contracts import CaptureState, Health, PublicConfig, ReadingJob, RecentCaptures
from backend.local_reader import LocalError, LocalReader
from backend.providers import Providers

RunId = Annotated[UUID, Header(alias="X-Reader-Run")]


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
        if scope not in jwt.get("scopes", []):
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
            if not jwt.get("claims", {}).get("sub") or "reader/read" not in jwt.get("scopes", []):
                raise HTTPException(status_code=401, detail="Sign in with the reader/read scope")
        return config.public_config()

    if config.mode == "local":

        @app.get("/api/local/readings", response_model=RecentCaptures)
        async def recent_readings(x_reader_run: RunId, cursor: str | None = None) -> RecentCaptures:
            return reader.recent(str(x_reader_run), cursor)

        @app.post("/api/local/readings/{job_id}", status_code=202, response_model=ReadingJob)
        async def capture(
            job_id: UUID,
            request: Request,
            x_reader_run: RunId,
            mode: Literal["spread", "single"] = "spread",
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
            return reader.submit(str(x_reader_run), str(job_id), bytes(data), mode)

        @app.get("/api/local/readings/{job_id}", response_model=ReadingJob)
        async def reading(job_id: UUID, x_reader_run: RunId) -> ReadingJob:
            return reader.get(str(x_reader_run), str(job_id)).view

        @app.post("/api/local/readings/{job_id}/retry", status_code=202, response_model=ReadingJob)
        async def retry(job_id: UUID, x_reader_run: RunId) -> ReadingJob:
            return reader.retry(str(x_reader_run), str(job_id))

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

        @app.get("/api/readings", response_model=RecentCaptures)
        def cloud_recent_readings(request: Request, cursor: str | None = None) -> RecentCaptures:
            store, owner = cloud(request, "reader/read")
            return store.recent(owner, cursor=cursor)

        @app.post("/api/readings/{job_id}", status_code=202, response_model=ReadingJob)
        async def cloud_capture(
            job_id: UUID,
            request: Request,
            x_reader_run: RunId,
            mode: Literal["spread", "single"] = "spread",
        ) -> ReadingJob:
            store, owner = cloud(request, "reader/write")
            if request.headers.get("content-type", "").split(";")[0] != "image/jpeg":
                raise LocalError("Send the framed image as image/jpeg.", 415)
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > config.camera.max_upload_bytes:
                    raise LocalError("The captured image exceeds the upload size limit.", 413)
                data.extend(chunk)
            return store.submit(owner, str(x_reader_run), str(job_id), bytes(data), mode)

        @app.get("/api/readings/{job_id}", response_model=ReadingJob)
        async def cloud_reading(job_id: UUID, request: Request, x_reader_run: RunId) -> ReadingJob:
            store, owner = cloud(request, "reader/read")
            return store.view(store.get(owner, str(x_reader_run), str(job_id)))

        @app.post("/api/readings/{job_id}/retry", status_code=202, response_model=ReadingJob)
        async def cloud_retry(job_id: UUID, request: Request, x_reader_run: RunId) -> ReadingJob:
            store, owner = cloud(request, "reader/write")
            return store.retry(owner, str(x_reader_run), str(job_id))

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
