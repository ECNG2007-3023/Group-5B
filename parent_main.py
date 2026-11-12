"""One backend for P5 Cloud VHDL Services: P5A and P5B on the same port.

    uvicorn parent_main:app --port 8000

P5A's code is used exactly as it is in the Group-5A repository; nothing in it
is changed. This file imports P5A's FastAPI app (so its routes, error handlers,
CORS settings and start-up clean-up all stay) and adds P5B to it:

    POST /simulate, /simulate/vcd, GET /health, /    P5A, unchanged
    WS   /ws/language-service                        P5B
    GET  /language-service/health                    P5B
    POST /check-and-simulate                         both: VHDL-LS first, GHDL only if it is clean

Where P5A is found: the folder in P5A_DIR, otherwise a Group-5A or
Group-5A-main folder next to this repository.

GHDL is run with VHDL-2008 (P5A's P5A_GHDL_STD setting), because that is what
VHDL-LS checks against. Set P5A_GHDL_STD yourself before starting to use
another standard.
"""
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

HERE = Path(__file__).resolve().parent

log = logging.getLogger("p5.parent")


def find_p5a():
    if os.environ.get("P5A_DIR"):
        candidates = [Path(os.environ["P5A_DIR"])]
    else:
        candidates = [HERE.parent / "Group-5A", HERE.parent / "Group-5A-main"]
    for folder in candidates:
        if (folder / "app" / "main.py").is_file():
            return folder.resolve()
    tried = ", ".join(str(c) for c in candidates)
    raise RuntimeError(f"P5A (the Group-5A repository) was not found. Tried: {tried}. "
                       "Clone it next to this repository or set P5A_DIR to its folder.")


# Must be set before P5A's config module is imported, which reads it once.
os.environ.setdefault("P5A_GHDL_STD", "08")

P5A_DIR = find_p5a()
if str(P5A_DIR) not in sys.path:
    sys.path.append(str(P5A_DIR))

from app import docker_runner                                         # noqa: E402  (P5A)
from app import main as p5a_main                                      # noqa: E402  (P5A)
from app.models import SimulationRequest                              # noqa: E402  (P5A)
from gateway.jsonrpc import GatewayError                              # noqa: E402
from gateway.router import router as p5b_router                       # noqa: E402
from gateway.router import shutdown as p5b_shutdown                   # noqa: E402
from gateway.vhdl_ls_bridge import LanguageServerError                # noqa: E402

app = p5a_main.app
app.include_router(p5b_router)

# P5A's app already has a lifespan (it removes old job containers at start-up).
# Keep it, and close all P5B sessions after it when the server stops.
_p5a_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(a):
    async with _p5a_lifespan(a) as state:
        yield state
        try:
            await p5b_shutdown()
        except Exception:
            log.exception("could not close the P5B sessions")


app.router.lifespan_context = _lifespan


def _failure(status_code, error, message, diagnostics=None):
    """A reply in P5A's Result shape, plus the P5B diagnostics."""
    return JSONResponse(status_code=status_code, content={
        "job_id": None, "status": "failed", "stage": "language_service", "error": error,
        "message": message, "stages": [], "vcd": None, "diagnostics": diagnostics or []})


@app.post("/check-and-simulate", tags=["P5A + P5B"])
async def check_and_simulate(request: SimulationRequest):
    """Check the files with VHDL-LS (P5B) first. If it finds an error, no GHDL
    job is created and the diagnostics are returned. Otherwise the request goes
    to P5A's /simulate and its result comes back with an empty diagnostics list
    for every file.

    Same request body as P5A's POST /simulate.
    """
    try:
        docker_runner.validate_request(request.files, request.top, request.stop_time)
    except docker_runner.JobValidationError:
        # Bad request: let P5A give its usual validation_error reply.
        return await run_in_threadpool(p5a_main.simulate, request)

    files = [{"name": f.name, "content": f.content} for f in request.files]
    try:
        checked = await p5b_router.get_manager().check_files(files)
    except GatewayError as exc:
        if exc.category == "invalid_params":
            return _failure(400, "validation_error", f"Invalid request: {exc.detail}")
        return _failure(503, "internal_error", f"Language service unavailable: {exc.message}")
    except LanguageServerError:
        log.exception("VHDL-LS failed during check-and-simulate")
        return _failure(503, "internal_error",
                        "Language service unavailable: VHDL-LS stopped while checking the files")

    errors = sum(1 for c in checked for d in c["diagnostics"] if d["severity"] == 1)
    if errors:
        word = "error" if errors == 1 else "errors"
        return _failure(200, "analysis_error",
                        f"VHDL-LS found {errors} {word}, so no GHDL job was started.", checked)

    result = await run_in_threadpool(p5a_main.simulate, request)
    if isinstance(result, JSONResponse):
        return result  # P5A's own failure reply (e.g. Docker not running)
    result["diagnostics"] = checked
    return result
