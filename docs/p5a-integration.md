# P5B and P5A: one backend

How the Gateway and P5A's GHDL Execution Service run together (P5-StR4.1:
"one submitted backend supports a reference GHDL job and a reference
language-service case without source restructuring"). No P5A file is changed.

## Answers to the questions in P5A's `docs/p5b-integration.md`

1. **Does P5B call P5A?** The front end can call each service directly with
   the same files: the language service over the WebSocket, the simulation
   over `POST /simulate`. The parent backend also has `POST /check-and-simulate`,
   which checks the files with VHDL-LS first and only creates a GHDL job if
   there are no errors (the team's integration tests FR-P5-01 and FR-P5-02).
2. **P5B's diagnostic format.** The LSP `Diagnostic`: `range.start.line` and
   `character` (0-based), `severity` (1 error, 2 warning), `message`, sent in
   `textDocument/publishDiagnostics` for `uri = file:///<name>`. GHDL's
   `file.vhd:line:col` counts from 1, so LSP line = GHDL line - 1. VHDL-LS
   can point at the next token, so the two can differ by one line.
3. **VHDL standard.** Both use VHDL-2008. VHDL-LS uses it by default, and
   `parent_main.py` sets P5A's `P5A_GHDL_STD` to `08` unless it is already
   set. P5A run on its own keeps its default.
4. **Ports.** One process and one port (8000) for both: `parent_main:app`.

## The shared file model

| | P5A request | P5B session |
| --- | --- | --- |
| file name | `files[].name` | `uri = file:///<name>` |
| file text | `files[].content` | `textDocument.text` (didOpen), `contentChanges[0].text` (didChange) |
| name rule | `FILE_NAME_RE` | the same rule (`gateway/session_store.py`) |
| limits | 20 files, 200,000 bytes each | the same |

`SessionManager.p5a_request(session_id, top, stop_time)` turns a language
session's files into the body for P5A's `POST /simulate`.

## Running both

```
some-folder\
    Group-5A\      (or Group-5A-main, or set $env:P5A_DIR)
    Group-5B\
```

From `Group-5B`, with the virtual environment active:

```
pip install -r requirements.txt                # same versions as P5A, plus websockets
docker build -t p5a-ghdl ..\Group-5A\docker    # once, Docker Desktop running
uvicorn parent_main:app --port 8000
```

`parent_main.py` imports P5A's own `app` object and adds P5B's router to it,
so P5A keeps its routes, start-up clean-up, JSON error handlers and CORS
settings. P5B's sessions are closed after P5A's shutdown when the server stops.
Both run on the host: P5A starts each GHDL stage as a Docker container, and
P5B starts `vhdl_ls` as a child process.

## Checks

```
python -m pytest tests/test_combined.py -v -rs    # P5A + P5B in one app
python tools/check_gateway.py                     # with parent_main running: P5B cases
python ..\Group-5A\docker\m4_api_check.py         # with parent_main running: P5A's own cases
```

`tests/test_combined.py`:

| Test | Needs |
| --- | --- |
| One app serves both; P5A's `/health`, `/` and error replies are unchanged | P5A |
| A session's files pass P5A's own validator and run through `POST /simulate` | P5A |
| P5B still works when P5A reports Docker down | P5A |
| `/check-and-simulate`: an error means no job; clean files go to P5A; bad request gives P5A's 400 | P5A |
| A real GHDL job on the session files | P5A + Docker + `p5a-ghdl` image |
| A syntax error is found by both, on the same line (give or take one) | + real `vhdl_ls` |
| FR-P5-01 and FR-P5-02 end to end through `/check-and-simulate` | + real `vhdl_ls` |
| VHDL-2008 code gets the same verdict from both | + real `vhdl_ls` |

## Known limits

* P5A rejects an empty file and two names that differ only in letter case. A
  language session accepts them (an editor can be empty), so such a file set
  fails at the P5A step with P5A's own message.
* GHDL and VHDL-LS are different tools. They agree on whether a file has an
  error, but word it differently and may point one token apart.
