"""The Acta API: JSON endpoints under /api plus the dashboard's static files.

    uvicorn acta.api.app:app          (or: python -m acta serve)

Most reads open acta.db read-only; the few endpoints that write (logging food,
feelings, workouts, finance) go through open_db_rw() or the events.json log.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from acta import config
from acta.api import finance_auth, schema
from acta.api.routers import (
    auspex,
    biocharge,
    body,
    capture,
    finance,
    food,
    journal,
    overview,
    portfolio,
    productivity,
    sleep,
    training,
    vitals,
    wishlist,
    workout,
)

ROUTERS = (overview, biocharge, sleep, vitals, journal, training, body, food, workout,
           finance_auth, finance, portfolio, wishlist, capture, auspex, productivity)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    schema.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Acta API", docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=lifespan)

    @app.middleware("http")
    async def no_cache(request, call_next):
        """Every response — dashboard files and API JSON alike — must be
        revalidated, never silently reused from the browser's HTTP cache.
        StaticFiles sends only Last-Modified/ETag, so without this a browser may
        apply heuristic caching and keep showing a pre-deploy index.html even
        after the service worker's cache version is bumped. ETag/Last-Modified
        still make repeat loads cheap (a 304 when nothing changed). Exercise
        media is excluded: large, static, meant to be cached and range-requested."""
        response = await call_next(request)
        if not request.url.path.startswith("/exercise-videos/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    for module in ROUTERS:
        app.include_router(module.router)

    # Mounted last so /api/* routes always win.
    app.mount("/exercise-videos", StaticFiles(directory=config.MEDIA_DIR, check_dir=False),
              name="exercise-videos")
    app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
    return app


app = create_app()
