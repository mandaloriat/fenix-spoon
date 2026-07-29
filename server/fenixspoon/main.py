"""App factory. Run with: uvicorn fenixspoon.main:app --reload"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .jobs import PURGE_INTERVAL_SECONDS, JobManager

log = logging.getLogger(__name__)

# In a repo checkout the examples live two levels up from this file; when the package is
# pip-installed without the repo, /demo simply isn't mounted.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples"
_CLIENT_PACKAGES_DIR = _REPO_ROOT / "client" / "packages"


def _mount_client_packages(app: FastAPI) -> list[str]:
    """Serve built browser packages at /packages/<name>/, for demos that use them.

    Only what has actually been built is mounted, so a checkout without `npm run build`
    simply serves nothing here and the widget demo says so rather than half-loading.
    """
    mounted: list[str] = []
    if not _CLIENT_PACKAGES_DIR.is_dir():
        return mounted
    for package in sorted(_CLIENT_PACKAGES_DIR.iterdir()):
        dist = package / "dist"
        if dist.is_dir():
            app.mount(f"/packages/{package.name}", StaticFiles(directory=dist), name=package.name)
            mounted.append(package.name)
    return mounted


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Reconcile what the last process lifetime left behind, then sweep on a timer."""
    manager: JobManager = app.state.jobs
    stranded = manager.reconcile()
    if stranded:
        log.warning("failed %d job(s) left running by a previous process", len(stranded))
    purged = manager.purge_expired()
    if purged:
        log.info("purged %d expired job(s)", len(purged))

    async def sweep() -> None:
        while True:
            await asyncio.sleep(PURGE_INTERVAL_SECONDS)
            # A failing sweep must not take the server down with it.
            try:
                manager.purge_expired()
            except Exception:
                log.exception("retention sweep failed")

    task = asyncio.create_task(sweep())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        manager.store.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fenix Spoon",
        version=__version__,
        description="Toolkit server for FEniCSx-powered web applications",
        lifespan=lifespan,
    )
    app.state.jobs = JobManager()
    # Dev default: open CORS so widgets on any origin can talk to the server.
    # Production deployments must restrict origins (roadmap M3).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    app.state.client_packages = _mount_client_packages(app)

    if _EXAMPLES_DIR.is_dir():
        app.mount("/demo", StaticFiles(directory=_EXAMPLES_DIR, html=True), name="demo")

        @app.get("/", include_in_schema=False)
        def index() -> RedirectResponse:
            return RedirectResponse(url="/demo/index.html")

    return app


app = create_app()
