"""App factory. Run with: uvicorn fenixspoon.main:app --reload"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router
from .jobs import JobManager

# In a repo checkout the examples live two levels up from this file; when the package is
# pip-installed without the repo, /demo simply isn't mounted.
_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Fenix Spoon",
        version=__version__,
        description="Toolkit server for FEniCSx-powered web applications",
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

    if _EXAMPLES_DIR.is_dir():
        app.mount("/demo", StaticFiles(directory=_EXAMPLES_DIR, html=True), name="demo")

        @app.get("/", include_in_schema=False)
        def index() -> RedirectResponse:
            return RedirectResponse(url="/demo/airfoil-2d/index.html")

    return app


app = create_app()
