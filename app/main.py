from pathlib import Path
import asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from app.job_manager import job_manager
from app.routes import router
from app.worker_pool import pool
from app.config import settings

app = FastAPI(title="DeepSeek Web API Bridge V20", version="20.0.0")
app.include_router(router)

web_dir = Path(__file__).resolve().parent.parent / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")



@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "path": str(request.url.path),
        },
    )

@app.on_event("startup")
async def on_startup():
    await job_manager.ensure_started()
    await pool.ensure_health_task()
    await asyncio.sleep(0.1)
    try:
        from app.runtime_settings import runtime_store
        if runtime_store.get().autoStartWorker:
            await pool.start_all()
    except Exception:
        pass


def main() -> None:
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
