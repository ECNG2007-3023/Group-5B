# P5B: testing techniques applied (Milestone 5)

How each technique from the ECNG 2007 sessions is used in this repository:
**Assertions and Exceptions**, **Using Invariants**, and **White Box and Black
Box Testing**. Every item points to the code that does it and the test that
proves it.

**Latest run: 273 passed, 0 failed, 0 skipped** (VHDL-LS 0.88.0, P5A with
GHDL 4.1.0 in Docker, 19 Nov 2026). **Coverage: 96%** statement + branch
(coverage.py).

| Technique (lecture) | Where in the code | Tests |
| --- | --- | --- |
| Assertions | `assert` statements in `gateway/` | `test_invariants.py::test_assertions_guard_programmer_assumptions` |
| Exceptions (try / throw / catch) | `GatewayError`, `LanguageServerError` family, `InvariantError`; `Connection.handle_raw` | `test_gateway.py`, `test_failures.py`, `test_blackbox.py` |
| Loop invariant | completion de-duplication loop in `mappers.to_completion()` | `test_invariants.py` (trace test) |
| Class invariants | `Session`, `SessionManager` | `test_invariants.py` |
| State machine (state invariant) | connection lifecycle, `Connection._set_state` | `test_invariants.py`, `test_blackbox.py` TC-B-36 … 45 |
| Data-structure invariants | every outgoing message and reply | `test_invariants.py` |
| White box: statement, branch, path | whole code base; worked example on `summarize()` | coverage.py, `test_paths.py`, `test_failures.py`, `test_bridge.py` |
| Black box: equivalence partitions, boundary values | limits, file names, positions, request ids | `test_blackbox.py` TC-B-01 … 30 |
| Black box: decision table | `POST /check-and-simulate` | `test_blackbox.py` TC-B-31 … 35 |
| Black box: state transitions | connection lifecycle | `test_blackbox.py` TC-B-36 … 45 |
| Black box: error guessing | malformed messages, odd files, Windows files, dropped client | `test_blackbox.py` TC-B-46 … 55 |
| Writing test cases | the tables in this document | – |

---

## 1. Assertions

An assertion checks an assumption the programmer made; if it is false the
program stops at once (lecture slide 2). We use them for **preconditions**
that the calling code has already guaranteed:

| `assert` | Assumption it checks |
| --- | --- |
| `assert is_allowed_filename(name)` in `methods._update_file` and `Session.path_for` | only names already checked by `resolve_uri()` reach the disk |
| `assert session_id not in self._sessions` in `SessionManager.open` | session ids (random 128-bit) never collide |
| `assert isinstance(version, int)` in `mappers.to_publish_diagnostics` | callers pass the document version as a whole number |
| `assert all(d["severity"] in SEVERITY_WORDS ...)` in `mappers.summarize` | the mapper only ever produces severities 1-4 |

**Assert in production or not?** The lecture (slide 4) gives both sides. We
use plain `assert` for cheap internal assumptions (on in normal runs and
tests, removed by `python -O`) and `InvariantError` (section 3) for rules that
must never be switched off. `InvariantError` is a real exception, so `-O`
cannot remove it, and the client only ever sees "Internal error", never the
internal details.

## 2. Exceptions

C++ uses `try` / `throw` / `catch`; Python's words are `try` / `raise` /
`except`. The lecturer's example and ours side by side:

| Lecturer's C++ example (`Search`) | P5B (`Connection.handle_raw`) |
| --- | --- |
| `throw runtime_error("Student ID not found!");` | `raise GatewayError(METHOD_NOT_FOUND, f"Method not supported: ...", ...)` |
| `try { ... }` | `try: result = await handler(self, msg.params)` |
| `catch (const exception& e) { cout << e.what(); }` | `except GatewayError as exc: await self.send(error_from(exc, msg.id))` |

When we use them, following slide 7:

| Slide 7 reason | Exception in P5B | What the client gets |
| --- | --- | --- |
| Possible invalid operations | `GatewayError` (bad JSON, unknown method, bad params, not initialized, limits); `InvalidFilename`, `UnknownSession` in the store | JSON-RPC error with the right code (-32700, -32600, -32601, -32602, -32002) and a category |
| Code relies on external systems | `LanguageServerDied`, `LanguageServerTimeout`, `LanguageServerError` (VHDL-LS); `OSError` from the disk | -32603 `language_server_failure` / `internal_error`; other sessions carry on |
| Debugging and testing | `InvariantError`, `AssertionError` | -32603 "Internal error" (HTTP 500 on `/check-and-simulate`), never a malformed reply |
| Anything else | any other `Exception` (catch-all in `handle_raw`) | -32603 "Internal error"; the session carries on |

`test_failures.py` and `test_bridge.py` make each external dependency fail in
turn: the disk full, a file that cannot be written or deleted, VHDL-LS that
will not close, sends garbage, cuts an answer off, or ignores `exit`.

## 3. Invariants

An invariant is a property that stays true at specified points while the
program changes state (Using Invariants, slide 4). All of ours are checked
**while the Gateway runs** (`gateway/invariants.py`), not only in the tests.
The lecture names three kinds (slide 5); P5B uses all three.

### 3.1 Loop invariant: removing repeated completion items

**Goal:** `to_completion()` must return each distinct suggestion once.
VHDL-LS can send the same suggestion more than once.

**Invariant:** *at the start of every iteration, `seen` holds exactly the
keys (label, kind, insert text) of the items kept so far, so no item has been
kept twice.*

| Step | Why it holds | Checked by |
| --- | --- | --- |
| Establishment | before the loop both `out` and `seen` are empty | `check_dedupe_invariant(out, seen)` before the loop |
| Preservation | an item is added to `out` and its key to `seen` together, and only if the key was not already in `seen` | `len(out) == len(seen)` after every kept item |
| Exit | every item has been looked at, so `out` holds each distinct item exactly once | `check_dedupe_invariant` after the loop |
| Termination | a `for` loop over a finite list: items left drops by 1 each iteration | – (guaranteed by the loop form) |

**Trace table** for the items `a`, `a`, `b`:

| Iteration | Item | Kept so far | `seen` | Invariant |
| --- | --- | --- | --- | --- |
| start | – | `[]` | `{}` | true |
| 1 | a | `[a]` | `{a}` | true |
| 2 | a (repeat) | `[a]` | `{a}` | true: skipped |
| 3 | b | `[a, b]` | `{a, b}` | true |
| exit | – | each distinct item once | | sorted and returned |

Tested by `test_dedupe_loop_invariant_trace` and
`test_dedupe_loop_invariant_catches_a_repeat`.

### 3.2 Class invariants

**`Session`** (`Session.check_invariant`, true after creation and after every
operation that changes it):

* files with published diagnostics are open;
* every open file has a version;
* every stored name is in the prescribed file set;
* a session never holds more than 20 files.

Establishment: checked when the session is created. Preservation: checked at
the end of every didOpen / didChange and after every didClose. Tests:
`test_session_class_invariant` (each rule broken),
`test_session_class_invariant_holds_through_a_real_session`,
`test_broken_session_invariant_is_caught_in_the_gateway`.

**`SessionManager`** (`check_manager`, after every open and close): each
session is stored under its own id, the starting count is never negative,
and at most 8 sessions are active. Test: `test_manager_class_invariant`.

### 3.3 State machine of a connection

A connection is always in one of four states, and may only change state along
these transitions (`invariants.TRANSITIONS`, checked on every change by
`Connection._set_state`):

```
            initialize                 VHDL-LS dies
   new  ───────────────▶  ready  ───────────────▶  failed
                          │   ▲                       │
                 shutdown │   └────── initialize ─────┤
                          ▼                           │ shutdown
                       shutdown  ◀────────────────────┘
                          │ exit
                          ▼
                     (connection closed)
```

State invariant: *a connection has a session exactly when it is `ready`.*
Tests: `test_every_allowed_transition_passes_and_others_are_caught`,
`test_illegal_transition_is_refused_by_the_connection`, and the black-box
state-transition tests (section 5.3).

### 3.4 Data-structure invariants: every reply

* **Every message sent** is valid JSON-RPC 2.0: a reply has exactly one of
  result/error; an error has an integer code and data with status `failed`,
  stage `language_service`, a category and a detail (`check_outgoing`, run in
  `Connection.send` on every message).
* **Every diagnostics reply** is for a file in the prescribed set, has
  severity 1-4 and non-negative positions, is sorted, and its summary matches
  ("No errors present" exactly when there is no error).
* **Every completion reply** has labels, no repeats, and is sorted.
* Only a folder directly inside the sessions folder is ever deleted.
* `/check-and-simulate`: one diagnostics entry per file, and **no GHDL job if
  VHDL-LS found an error** (FR-P5-01).

`test_mapper_output_always_passes_for_any_input` feeds the mapper 500 random,
partly nonsense VHDL-LS replies; every output obeys the rules.

## 4. White-box testing

### 4.1 Coverage, measured with coverage.py

```
python -m coverage run -m pytest tests     # statement + branch (see .coveragerc)
python -m coverage report -m
```

| File | Statement + branch coverage |
| --- | --- |
| `gateway/jsonrpc.py`, `session_store.py`, `main.py` | 100% |
| `gateway/session.py` | 99% |
| `gateway/invariants.py`, `mappers.py`, `methods.py` | 98% |
| `gateway/router.py`, `fake_bridge.py`, `parent_main.py` | 97% |
| `gateway/connection.py`, `config.py` | 94% |
| `gateway/vhdl_ls_bridge.py` | 92% |
| **Total** | **96%** |

**Review of uncovered code** (the lecture's "run, review uncovered, add
tests" loop). The first run gave 91%. The lines no test reached were error
handling, so two sets of tests were added:

* `tests/test_failures.py`: the disk failing, VHDL-LS that will not close, a
  folder that cannot be deleted, and the combined backend's error replies.
  `session.py` went from 90% to 99% and `parent_main.py` from 84% to 97%.
* New bad behaviours for the fake language-server process
  (`tests/fake_vhdl_ls.py`): broken framing, a missing Content-Length, an
  answer cut off half-way, ignoring `exit`, and asking the client a question.
  `vhdl_ls_bridge.py` went from 84% to 92%.

What is left uncovered is mostly operating-system race paths (a pipe closing
between two calls). As the lecture warns, coverage only shows the code ran;
the black-box tests below check the answers are right.

### 4.2 Worked example: `summarize(diagnostics)`

```
D1: if there are no diagnostics:        return "No errors present."
    count each severity, build parts ("2 errors", "1 warning", ...)
D2: if there is only one kind:          text = that part
    else:                               text = "a, b and c"
D3: if any are errors:                  return text + " found."
    else:                               return "No errors present; " + text + " found."
```

3 decisions, so cyclomatic complexity 4. Paths: 1 (D1 true) + 2 × 2 = 5.

| Coverage | Goal | Tests (`tests/test_paths.py`) | Inputs |
| --- | --- | --- | --- |
| Statement | every line runs | 3 | P1 none, P2 one error, P5 a warning and 2 hints |
| Branch | D1, D2, D3 each true and false | 3 | P1, P2, P5 |
| Path | every route | 5 | P1 `[]`, P2 `[error]`, P3 `[warning]`, P4 `[error, error, warning]`, P5 `[warning, hint, hint]` |

## 5. Black-box testing

Designed from the SRS and the contract only (`tests/test_blackbox.py`), using
the Gateway's WebSocket and HTTP interfaces. VHDL-LS is replaced by the fake
bridge where the Gateway's own rules are tested, and the real VHDL-LS is used
where its answer matters.

### 5.1 Equivalence partitions and boundary values

| Input | Partitions (one test from each) | Boundary values (below / on / above) | Test IDs |
| --- | --- | --- | --- |
| Files per session | 1–20 valid · >20 invalid | 19 · 20 · 21 | TC-B-01 … 03 |
| File size | 0–200,000 bytes valid · >200,000 invalid | 199,999 · 200,000 · 200,001 | TC-B-04 … 06 |
| Sessions at once | 1–8 valid · >8 invalid | 8th accepted · 9th refused · freed slot reused | TC-B-07 … 09 |
| File name (URI) | valid flat name · wrong extension · folder · `..` · encoded `..` · Windows path · not a file URI | 100 characters · 101 characters | TC-B-10 … 19 |
| Completion position | whole number ≥ 0 · negative · text · decimal | 0 accepted · −1 refused | TC-B-20 … 24 |
| Request id | string or whole number · true/false · decimal · object | 0 accepted | TC-B-25 … 30 |

### 5.2 Decision table: `POST /check-and-simulate`

| Rule | Request valid | VHDL-LS starts | VHDL-LS finds an error | GHDL result | Reply | Test |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | N | – | – | – | 400 `validation_error`, no job | TC-B-31 |
| R2 | Y | N | – | – | 503 `internal_error`, no job | TC-B-32 |
| R3 | Y | Y | Y | – | 200 `failed`, **no GHDL job** (FR-P5-01) | TC-B-33 |
| R4 | Y | Y | N | success | 200 `success` + VCD (FR-P5-02) | TC-B-34 |
| R5 | Y | Y | N | failed | 200 `failed` with GHDL's error | TC-B-35 |

### 5.3 State transitions: a connection's lifecycle

| State | Event | Reply | Next state | Test |
| --- | --- | --- | --- | --- |
| new | any request | -32002 not initialized | new | TC-B-36 |
| new | initialize | capabilities | ready | TC-B-37 |
| ready | initialize | -32600 already initialized | ready | TC-B-38 |
| ready | didOpen | diagnostics | ready | TC-B-37, 38 |
| ready | VHDL-LS dies | -32603 `language_server_failure` | failed | TC-B-39 |
| failed | didOpen | -32603 `language_server_failure` | failed | TC-B-40 |
| failed | initialize | capabilities, new session | ready | TC-B-41 |
| ready | shutdown | null | shutdown | TC-B-42 |
| failed | shutdown | null | shutdown | TC-B-43 |
| shutdown | anything but exit | -32600 session shut down | shutdown | TC-B-44 |
| shutdown | exit | connection closed | – | TC-B-45 |

### 5.4 Error guessing

* **Empty inputs / data types:** not JSON, a JSON array, wrong `jsonrpc` version, unknown method, `params` as text (TC-B-46 … 50); an empty file, a NUL byte, a 150,000-character line (TC-B-51).
* **Windows behaviour:** files with a UTF-8 BOM, CRLF line endings and tabs, checked with the real VHDL-LS (TC-B-53 … 55).
* **Security:** encoded `..`, Windows paths and non-file URIs, with nothing written to disk (TC-B-16 … 19).
* **Buffer limits:** 21 files, 200,001 bytes, 9 sessions, a 1 MB message (`test_gateway.py`).
* **Environmental disruptions:** the client vanishing mid-session (TC-B-52); VHDL-LS dying, hanging or sending garbage (`test_gateway.py`, `test_bridge.py`); disk errors (`test_failures.py`).

## 6. Test cases

Written in the lecture's format. "Actual" is from the latest run.

| ID | Objective | Preconditions | Steps | Test data | Expected | Actual | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| TC-B-02 | Upper boundary of files per session is accepted | Session initialized | didOpen 20 files | 20 small files | diagnostics for each | diagnostics for each | Pass |
| TC-B-03 | One file over the limit is refused | 20 files open | didOpen a 21st | `f20.vhd` | -32602 | -32602 | Pass |
| TC-B-05 | Largest allowed file is accepted | Session initialized | didOpen | 200,000 bytes | diagnostics | diagnostics | Pass |
| TC-B-06 | One byte over the size limit is refused | Session initialized | didOpen | 200,001 bytes | -32602 | -32602 | Pass |
| TC-B-08 | A 9th session is refused | 8 sessions open | initialize | – | -32603 `session_error` | -32603 `session_error` | Pass |
| TC-B-09 | A freed session slot can be reused | 8 open, then one shut down | initialize | – | capabilities | capabilities | Pass |
| TC-B-17 | Encoded path traversal is refused | Session initialized | didOpen | `file:///%2e%2e/design.vhd` | -32602, nothing written | -32602, nothing written | Pass |
| TC-B-21 | A negative completion position is refused | File open | completion | line −1 | -32602 | -32602 | Pass |
| TC-B-33 | A syntax error stops the GHDL job (FR-P5-01) | P5A present | POST /check-and-simulate | file with `x <= y` and no `;` | 200 `failed`, no job | 200 `failed`, no job | Pass |
| TC-B-34 | Clean files run on P5A (FR-P5-02) | P5A present | POST /check-and-simulate | clean entity | 200 `success` | 200 `success` | Pass |
| TC-B-36 | Requests before initialize are refused | New connection | completion | – | -32002 | -32002 | Pass |
| TC-B-39 | A dead VHDL-LS is reported | Session initialized | kill VHDL-LS, didOpen | `fx01_valid.vhd` | -32603 `language_server_failure` | -32603 `language_server_failure` | Pass |
| TC-B-44 | Only exit is accepted after shutdown | After shutdown | initialize, didOpen | – | -32600 each | -32600 each | Pass |
| TC-B-52 | A dropped client leaves nothing behind | Session with a file open | close the socket with no shutdown | – | folder deleted, VHDL-LS stopped | folder deleted, VHDL-LS stopped | Pass |
| TC-B-53 | A file saved by Notepad (UTF-8 BOM) gives no errors | Real VHDL-LS | didOpen | `fx01_valid.vhd` starting with a BOM | "No errors present." | "No errors present." | Pass (Fail before fix 1) |
| TC-B-54 | CRLF line endings give the error on the same line | Real VHDL-LS | didOpen | `fx02_missing_semicolon.vhd` with CRLF | same lines as LF | same lines as LF | Pass |

### Defects found by these tests (fixed)

| # | Found by | Test | Actual before the fix | Fix |
| --- | --- | --- | --- | --- |
| 1 | Error guessing | TC-B-53 | A correct file saved with a BOM got "1 error found: Found invalid latin-1 character" | A leading BOM is removed before the file is stored or given to VHDL-LS (`mappers.without_bom`) |
