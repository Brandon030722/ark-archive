"""Release-pinned knowledge queries and append-only question audit endpoints."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import __version__, models, services
from .config import Settings, get_settings
from .db import make_engine, session_factory
from .storage import SnapshotStore


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=2000)
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    module: Literal["auto", "operator", "recruitment", "garrison", "roguelike"] = "auto"
    planner: Literal["local", "openai"] = "local"
    tags: list[str] | None = Field(default=None, max_length=5)
    duration_minutes: int | None = Field(default=None, ge=60, le=540)


class RecruitmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tags: list[str] = Field(max_length=5)
    duration_minutes: int = Field(default=540, ge=60, le=540)
    release_id: str | None = Field(default=None, min_length=1, max_length=128)


def _coverage(release: models.Release, module: str | None = None) -> dict[str, Any]:
    datasets = release.manifest.get("datasets", {})
    if module is not None:
        if module not in datasets:
            raise services.NotFound(f"Release {release.id} does not contain module {module}")
        datasets = {module: datasets[module]}
    return {
        name: {"scope": dataset.get("scope"), "coverage": dataset.get("coverage")}
        for name, dataset in datasets.items()
    }


def _metadata(release: models.Release, module: str | None = None) -> dict[str, Any]:
    return {
        "release_id": release.id,
        "manifest_hash": release.manifest_hash,
        "coverage": _coverage(release, module),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the app without writing schema, publishing data, or activating a release.

    Run with ``uvicorn arknights_agent.api:create_app --factory``. Configuration
    comes from Settings; local publishing and activation belong to the CLI.
    """
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        application.state.engine.dispose()

    app = FastAPI(
        title="Arknights Encyclopedia Agent",
        version=__version__,
        description=(
            "Version-pinned operator QA and deterministic recruitment tools. "
            "Garrison answers verified phase metadata and individually corroborated "
            "historical configuration fields and phase-pinned documented descriptions; "
            "incomplete runtime mechanisms remain gated and retain review evidence."
        ),
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.snapshot_store = SnapshotStore(settings.snapshot_root)

    def get_session() -> Iterator[Session]:
        with app.state.session_factory() as session:
            yield session

    SessionDependency = Annotated[Session, Depends(get_session)]

    def pin_release(
        session: SessionDependency,
        release_id: Annotated[str | None, Query(min_length=1)] = None,
    ) -> models.Release:
        # FastAPI caches this dependency during a request. The active pointer is
        # resolved exactly once; every downstream lookup receives this object.
        return services.resolve_release(session, release_id)

    ReleaseDependency = Annotated[models.Release, Depends(pin_release)]

    def error_response(status_code: int):
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            from .runtime.security import redact

            return JSONResponse(status_code=status_code, content={"detail": redact(str(exc))})

        return handler

    app.add_exception_handler(services.NotFound, error_response(404))
    app.add_exception_handler(services.Conflict, error_response(409))
    app.add_exception_handler(services.InvalidData, error_response(422))

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        from .runtime.security import redact

        # FastAPI's normal validation detail includes submitted input values.
        # Never echo credentials accidentally pasted in an invalid body.
        return JSONResponse(status_code=422, content={"detail": [
            {"type": item["type"], "loc": redact(list(item["loc"])), "msg": "Invalid request field"}
            for item in exc.errors()
        ]})

    @app.get("/health")
    def health(session: SessionDependency) -> dict[str, str]:
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            # Never include database URLs or credentials in readiness responses.
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    @app.get("/v1/releases")
    def releases(
        session: SessionDependency,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        rows = session.scalars(
            select(models.Release)
            .order_by(models.Release.created_at.desc(), models.Release.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return {
            "items": [
                services.release_view(services.resolve_release(session, row.id)) for row in rows
            ],
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/releases/active")
    def active_release(session: SessionDependency) -> dict[str, Any]:
        return services.release_view(services.resolve_release(session))

    @app.get("/v1/releases/{release_id}/manifest")
    def manifest(release_id: str, session: SessionDependency) -> dict[str, Any]:
        release = services.resolve_release(session, release_id)
        return {**_metadata(release), "manifest": release.manifest}

    @app.get("/v1/records")
    def records(
        session: SessionDependency,
        release: ReleaseDependency,
        module: Annotated[str | None, Query(min_length=1)] = None,
        kind: Annotated[str | None, Query(min_length=1)] = None,
        name: Annotated[str | None, Query(min_length=1)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        metadata = _metadata(release, module)
        items = services.list_records(
            session, release, module=module, kind=kind, name=name, limit=limit, offset=offset,
            relevance=True,
        )
        return {**metadata, "items": items, "limit": limit, "offset": offset}

    @app.get("/v1/records/{record_id}")
    def record(
        record_id: str, session: SessionDependency, release: ReleaseDependency
    ) -> dict[str, Any]:
        item = services.get_record(session, release, record_id)
        return {**_metadata(release, item.get("module")), "record": item}

    @app.get("/v1/evidence/{evidence_id}")
    def evidence(
        evidence_id: str, session: SessionDependency, release: ReleaseDependency
    ) -> dict[str, Any]:
        item = services.get_evidence(session, release, evidence_id)
        return {**_metadata(release), "evidence": item}

    def approved_snapshot(
        session: Session, release: models.Release, snapshot_id: str
    ) -> dict[str, Any]:
        referenced_ids = {
            item_id
            for dataset in release.manifest.get("datasets", {}).values()
            for item_id in dataset.get("source_snapshot_ids", [])
        }
        if snapshot_id not in referenced_ids:
            raise services.NotFound(f"Snapshot {snapshot_id} is not part of release {release.id}")
        return services.get_snapshot(session, release, snapshot_id)

    @app.get("/v1/snapshots/{snapshot_id}")
    def snapshot(
        snapshot_id: str, session: SessionDependency, release: ReleaseDependency
    ) -> dict[str, Any]:
        item = approved_snapshot(session, release, snapshot_id)
        # storage_key is an implementation detail and is never a user-controlled
        # filesystem path. Public metadata gives a release-pinned raw endpoint.
        public_item = {key: value for key, value in item.items() if key != "storage_key"}
        return {**_metadata(release), "snapshot": public_item}

    @app.get("/v1/snapshots/{snapshot_id}/raw")
    def snapshot_raw(
        snapshot_id: str, session: SessionDependency, release: ReleaseDependency
    ) -> Response:
        item = approved_snapshot(session, release, snapshot_id)
        raw = app.state.snapshot_store.read_verified(item["content_hash"])
        if len(raw) != item["byte_size"]:
            raise services.Conflict(f"Snapshot {snapshot_id} byte size does not match its metadata")
        # Return the bytes that were actually verified, not a path to be reopened
        # after verification. This also prevents a verify/read race.
        return Response(
            content=raw,
            media_type=item["media_type"] or "application/octet-stream",
            headers={
                "ETag": f'"{item["content_hash"]}"',
                "X-Knowledge-Release": release.id,
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'attachment; filename="snapshot.bin"',
            },
        )

    @app.post("/v1/qa")
    def question(body: QuestionRequest, session: SessionDependency):
        from .agent import dispatch_question, record_run

        release = services.resolve_release(session, body.release_id)
        result = dispatch_question(
            session,
            release,
            body.question,
            app.state.snapshot_store,
            settings,
            module=body.module,
            planner=body.planner,
            tags=body.tags,
            duration_minutes=body.duration_minutes,
        )
        record_run(session, release, body.question, result)
        session.commit()
        return result

    @app.post("/v1/recruitment/calculate")
    def recruitment(body: RecruitmentRequest, session: SessionDependency):
        from .agent import record_run, render_recruitment_answer
        from .recruitment import calculate

        release = services.resolve_release(session, body.release_id)
        result = calculate(
            session, release, body.tags, body.duration_minutes, store=app.state.snapshot_store
        )
        result["answer"] = render_recruitment_answer(result)
        record_run(session, release, "公开招募标签组合计算", result)
        session.commit()
        return result

    @app.get("/v1/qa/runs/{run_id}")
    def qa_run(
        run_id: str, session: SessionDependency, release_id: Annotated[str, Query(min_length=1)]
    ):
        release = services.resolve_release(session, release_id)
        row = session.get(models.QaRun, run_id)
        if row is None or row.release_id != release.id:
            raise services.NotFound("QA run is not included in this release")
        result = row.trace["result"]
        if services.content_digest(result) != row.trace["response_hash"]:
            raise services.Conflict("QA run failed integrity verification")
        from .runtime.security import contains_secret, redact

        if contains_secret(row.question) or contains_secret(result) or redact(result) != result:
            raise services.InvalidData("sensitive historical audit output")
        public = {
            "run_id": row.id,
            "release_id": row.release_id,
            "question": row.question,
            "result": result,
            "created_at": row.created_at.isoformat(),
            "runtime_implementation_hash": row.trace["runtime_implementation_hash"],
        }
        if contains_secret(public) or redact(public) != public:
            raise services.InvalidData("sensitive historical audit output")
        return public

    from .runtime.http import install_routes

    install_routes(app, settings)

    from .game_tools import install_routes as install_game_tools

    install_game_tools(app)

    # Keep the local interface on the same origin as its pinned knowledge APIs.
    # Only packaged web assets are reachable; backend sources and credential
    # files never become part of the static directory.
    web_root = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web_root, check_dir=False), name="web-assets")

    def workbench_page(*, reload_assets: bool = False) -> HTMLResponse:
        page = (web_root / "index.html").read_text(encoding="utf-8")
        # Give each release of the static files its own URL. An explicit
        # reset also bypasses copies of the same release held by a webview.
        refresh = f"-{uuid4().hex}" if reload_assets else ""
        for filename, marker in (("app.js", "__APP_VERSION__"), ("styles.css", "__STYLE_VERSION__"),
                                 ("game-tools.js", "__TOOLS_VERSION__")):
            digest = sha256((web_root / filename).read_bytes()).hexdigest()[:16]
            page = page.replace(marker, digest + refresh)
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "frame-ancestors 'none'",
            },
        )

    @app.get("/", include_in_schema=False)
    def web_index() -> HTMLResponse:
        return workbench_page()

    @app.get("/refresh-ui", include_in_schema=False)
    def refresh_ui() -> HTMLResponse:
        return workbench_page(reload_assets=True)

    @app.middleware("http")
    async def web_asset_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/"):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/v1/account/credential":
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app
