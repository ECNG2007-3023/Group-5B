"""P5B on its own:  uvicorn gateway.main:app --port 8000

For P5A and P5B together in one backend, use parent_main.py instead.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.router import router, shutdown


@asynccontextmanager
async def lifespan(_app):
    yield
    await shutdown()


app = FastAPI(
    title="VHDL Language-Service Gateway",
    description="P5B: VHDL-LS diagnostics and code completion over "
                "WebSocket/JSON-RPC at /ws/language-service.",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/")
def home():
    return {"message": "VHDL Language-Service Gateway is running"}
