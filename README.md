# Group-5B: P5B VHDL Language-Service Gateway

A FastAPI backend that gives clients VHDL-LS diagnostics and code completion
over a WebSocket, using JSON-RPC 2.0 messages with LSP method names. Every
session gets its own folder and its own `vhdl_ls` process; VHDL-LS does all
the language analysis. Together with P5A (GHDL execution service, the
Group-5A repository) it makes one P5 backend: `parent_main.py`.

Message format: [docs/language-service-contract.md](docs/language-service-contract.md)

## Run it (Windows, PowerShell, from the repo root)

1. Install VHDL-LS **v0.88.0**: download `vhdl_ls-x86_64-pc-windows-msvc.zip`
   from https://github.com/VHDL-LS/rust_hdl/releases/tag/v0.88.0 and unzip it,
   e.g. to `C:\tools`. Keep the `vhdl_libraries` folder next to `bin`
   (that is where VHDL-LS finds `std` and `ieee`). Then either add
   `C:\tools\vhdl_ls-x86_64-pc-windows-msvc\bin` to PATH or set
   `$env:P5B_VHDL_LS_PATH = "C:\tools\vhdl_ls-x86_64-pc-windows-msvc\bin\vhdl_ls.exe"`.
2. Set up Python (once):
   ```
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. Start the Gateway: `uvicorn gateway.main:app --port 8000`
4. Connect to `ws://127.0.0.1:8000/ws/language-service`, or run the check
   below. `http://127.0.0.1:8000/language-service/health` shows that it is up.

## Run P5A and P5B as one backend

Clone the two repositories next to each other (or set `$env:P5A_DIR` to the
P5A folder):

```
some-folder\
    Group-5A\     P5A, unchanged
    Group-5B\     this repository
```

Build P5A's GHDL image once (`docker build -t p5a-ghdl ..\Group-5A\docker`,
Docker Desktop running), then from this repo:

```
uvicorn parent_main:app --port 8000
```

One port serves P5A's `POST /simulate`, `/simulate/vcd`, `/health` and P5B's
WebSocket, plus `POST /check-and-simulate` (VHDL-LS first, GHDL only if there
are no errors). The combined backend runs GHDL with VHDL-2008, the same
standard VHDL-LS uses; nothing has to be set for that.
Details: [docs/p5a-integration.md](docs/p5a-integration.md).

## Test it

| Command | What it checks |
| --- | --- |
| `python -m pytest -v -rs` | All tests. Tests that need the real `vhdl_ls`, P5A, or Docker are skipped (not failed) when those are missing; `-rs` shows why |
| `python -m pytest tests/test_real_vhdl_ls.py -v` | The system-test cases with the real VHDL-LS |
| `python -m pytest tests/test_combined.py -v` | P5A + P5B in one backend, including FR-P5-01 and FR-P5-02 |
| `python tools/check_gateway.py` | With the Gateway running: runs every system-test case twice over a real WebSocket, prints PASS/FAIL, and checks both runs gave identical replies |

GitHub runs the tests on every push and pull request
(`.github/workflows/tests.yml`), with the real VHDL-LS.
What each test proves: [docs/test-plan.md](docs/test-plan.md).

## Use it from another project (P6A, scripts)

```python
from clients.p5b_client import LanguageServiceClient

with LanguageServiceClient("ws://127.0.0.1:8000/ws/language-service") as ls:
    ls.initialize()
    ls.open_file("and_gate.vhd", open("and_gate.vhd").read())
    print(ls.wait_for_diagnostics())          # [] when there are no problems
    ls.shutdown()
```

An LSP client library (e.g. monaco-languageclient in P6A) can connect
directly, since the messages are LSP messages.

## Layout

| Path | Purpose |
| --- | --- |
| `gateway/main.py` | The app for P5B alone |
| `gateway/router.py` | WebSocket endpoint and health route |
| `gateway/connection.py` | One connection: message handling, VHDL-LS failure detection, idle timeout |
| `gateway/methods.py` | The approved methods (anything else is rejected) and their parameter checks |
| `gateway/session.py` | Sessions: start/stop VHDL-LS, hand-off to P5A, one-off file checks |
| `gateway/session_store.py` | The shared file/session model on disk and the file name rule (same as P5A) |
| `gateway/vhdl_ls_bridge.py` | Runs `vhdl_ls` and speaks LSP to it over stdin/stdout |
| `gateway/mappers.py` | VHDL-LS replies to the contract format; removes real paths |
| `gateway/jsonrpc.py` | JSON-RPC parsing, replies and error codes |
| `gateway/fake_bridge.py` | Stand-in for VHDL-LS used by the tests |
| `gateway/config.py` | Limits (8 sessions, 20 files, 200,000 bytes per file...); all overridable by environment variable |
| `parent_main.py` | One backend: P5A's app with P5B added |
| `clients/p5b_client.py` | Python WebSocket client (the automated test client) |
| `clients/checks.py` | Compares replies with the expected results |
| `tools/check_gateway.py` | Live check against a running Gateway |
| `tests/` | Tests; `tests/vhdl/` holds the reference files and `tests/vhdl/expected/` the expected results |
| `docs/` | Contract (P5B part), P5A integration notes, test plan |
