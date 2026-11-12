# HDL Service Contract v1: P5B Language-Service Gateway

The language-service part of the contract. P5A's part (GHDL execution, `POST
/simulate`) is in the Group-5A repository, `docs/hdl-service-contract-v1.md`.
Both parts use the same file model: a flat list of named VHDL files.

## Connection

| | |
| --- | --- |
| Address | `ws://127.0.0.1:8000/ws/language-service` |
| Messages | JSON-RPC 2.0, one message per text frame, LSP method names |
| Session | One per WebSocket connection, with its own folder and `vhdl_ls` process |
| Positions | 0-based lines and characters (LSP). GHDL counts lines from 1 |
| Health | `GET /language-service/health` gives `{"status": "ok", "sessions": <n>}` |

## Approved methods (and nothing else)

| Method | Kind | What happens |
| --- | --- | --- |
| `initialize` | request | Creates the session folder and starts VHDL-LS. Reply: `{capabilities, serverInfo, sessionId}` |
| `initialized` | notification | Nothing (handshake step) |
| `textDocument/didOpen` | notification | `{textDocument: {uri, text, version?}}`. Saves the file, then sends `publishDiagnostics` |
| `textDocument/didChange` | notification | `{textDocument: {uri, version?}, contentChanges: [{text}]}` with the whole text. Sends `publishDiagnostics` |
| `textDocument/didClose` | notification | Closes the file in VHDL-LS |
| `textDocument/completion` | request | `{textDocument: {uri}, position: {line, character}}`. The file must be open |
| `shutdown`, then `exit` | request, notification | Stops VHDL-LS and deletes the session folder |

Every other method (hover, definition, rename, formatting, ...) is rejected
with `-32601` and never reaches VHDL-LS. `initialize` only advertises
`textDocumentSync` (full text) and `completionProvider`. LSP's optional `$/...`
notifications (such as `$/cancelRequest`) are ignored.

## Files (the prescribed file set)

`uri` is exactly `file:///<name>`. `<name>` follows P5A's rule: letters,
digits, `_ . -`, not starting with `.` or `-`, at most 100 characters, ending
in `.vhd` or `.vhdl` (any letter case), no folders. Every name the Gateway
accepts, P5A accepts too. At most 20 files per session and 200,000 bytes per
file (the same as P5A).

## Diagnostics

Sent by the Gateway after every `didOpen` and `didChange`:

```json
{"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
  "uri": "file:///design.vhd", "version": 1,
  "diagnostics": [{"range": {"start": {"line": 12, "character": 14}, "end": {"line": 12, "character": 14}},
                   "severity": 1, "code": "syntax_error", "source": "vhdl ls",
                   "message": "Expected ';' or 'when'"}],
  "summary": "1 error found."}}
```

* No problems: `"diagnostics": []` and `"summary": "No errors present."`.
* `severity`: 1 error, 2 warning, 3 information, 4 hint.
* Sorted by position, then severity, then message, so the same file always
  gives the same reply.
* When one file changes the diagnostics of another open file (a testbench
  stops reporting "No primary unit 'and_gate'" once `and_gate.vhd` is
  opened), the other file's `publishDiagnostics` is sent again as well.

## Completion

```json
{"jsonrpc": "2.0", "id": 2, "result": {"isIncomplete": false, "items": [
  {"label": "sig_clock", "kind": 6, "detail": "...", "insertText": "sig_clock"}]}}
```

No suggestions: `"items": []`. Items are sorted by label.

## Errors

A reply has either `result` or `error`, never both. Every error is:

```json
{"jsonrpc": "2.0", "id": 3, "error": {"code": -32601,
  "message": "Method not supported: textDocument/hover",
  "data": {"status": "failed", "category": "unsupported_method",
           "stage": "language_service", "detail": "..."}}}
```

`"failed"` is the same word P5A uses. Errors for notifications (they have no
id) are sent with `"id": null`. No error contains a stack trace or a real
folder path.

| Situation | `code` | `category` |
| --- | --- | --- |
| Not valid JSON (or not UTF-8) | -32700 | `invalid_request` |
| Not a JSON-RPC 2.0 object, batch, too large (1 MB) | -32600 | `invalid_request` |
| Method outside the approved subset | -32601 | `unsupported_method` |
| Bad params, file name outside the set, too many files, file too large, file not open | -32602 | `invalid_params` |
| Request before `initialize` | -32002 | `session_error` |
| Second `initialize`, request after `shutdown` | -32600 | `session_error` |
| Session limit (8), session closed after 5 min idle | -32603 | `session_error` |
| VHDL-LS could not start, terminated, or stopped responding | -32603 | `language_server_failure` |

After a `language_server_failure` the session is closed; send `initialize`
again for a new one. Other sessions are not affected.

## Shared file/session model

```
<root>/sessions/<session_id>/
    src/            the session's VHDL files, stored exactly as sent
    vhdl_ls.toml    tells VHDL-LS that src/*.vhd and src/*.vhdl are one library
```

`<root>` is `P5B_SESSION_ROOT` (default: the system temp folder + `p5b`).
`SessionManager.p5a_request(session_id, top, stop_time)` turns the files in
`src/` into P5A's request body `{"files": [{"name", "content"}], "top",
"stop_time"}`. The client gives `top`, since only it knows which entity is
the testbench.

## VHDL standard

VHDL-LS uses VHDL-2008 (`vhdl_ls.toml` has no `standard` line). The combined
backend sets P5A's `P5A_GHDL_STD` to `08`, so both services check the same
version.

## Joint endpoint (parent backend only)

`POST /check-and-simulate` takes P5A's `/simulate` request body. The files
are checked with VHDL-LS first:

* any error: no GHDL job is created. The reply has P5A's Result fields with
  `"status": "failed"`, `"stage": "language_service"`, `"error":
  "analysis_error"`, `"job_id": null`, plus `"diagnostics"`: one
  `publishDiagnostics` params object per file.
* no errors: the request goes to P5A's `/simulate`, and its Result comes back
  with `"diagnostics"` added (an empty list for every file).
* bad request: P5A's usual `validation_error` reply (HTTP 400).
* VHDL-LS not available: HTTP 503, `"error": "internal_error"`.
