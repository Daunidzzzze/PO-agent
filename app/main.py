import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .agent import active_prompt
from .db import Session, init_db
from .routes_panel import router as panel_router
from .routes_student import router as student_router
from .templating import templates

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

RUN_SCHEDULER = os.getenv("RUN_SCHEDULER", "1") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with Session() as s:
        await active_prompt(s)  # засеваем системный промпт версии 1
    sch = None
    if RUN_SCHEDULER:
        from .scheduler import build_scheduler
        sch = build_scheduler()
        sch.start()
        log.info("scheduler started")
    yield
    if sch:
        sch.shutdown(wait=False)


app = FastAPI(title="ИИ Product Owner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.ROOT / "static")), name="static")
app.include_router(student_router)
app.include_router(panel_router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.exception_handler(HTTPException)
async def unauthorized(request: Request, exc: HTTPException):
    """401 в браузере — это редирект на нужный вход, а не голый JSON."""
    if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(
            "/panel/login" if request.url.path.startswith("/panel") else "/",
            status_code=303)
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request, "error.html", {"detail": exc.detail}, status_code=exc.status_code)
    return await http_exception_handler(request, exc)
