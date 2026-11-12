"""Runs vhdl_ls as a child process and talks LSP to it over stdin/stdout.

VhdlLsProcess does the real work. Its methods block, so the Gateway uses the
Bridge class at the bottom, which runs each call in a worker thread and can be
awaited. One Bridge (and one vhdl_ls process) is started for every session.

vhdl_ls finds the session's files through the vhdl_ls.toml file in the
session folder (written by session_store.py). Line and column numbers are
0-based, as in LSP.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse


class LanguageServerError(Exception):
    """vhdl_ls answered a request with an error."""


class LanguageServerDied(LanguageServerError):
    """vhdl_ls could not be started, exited, or sent something that is not LSP."""


class LanguageServerTimeout(LanguageServerError):
    """vhdl_ls did not answer in time."""


def vhdl_ls_command():
    """Which program to run: P5B_VHDL_LS_PATH if set, otherwise vhdl_ls on PATH."""
    return [os.environ.get("P5B_VHDL_LS_PATH") or "vhdl_ls"]


# ---------------------------------------------------------------- LSP framing

def encode_message(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


def read_message(stream):
    """Read one message (headers, blank line, JSON body). None at end of stream."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    if length is None:
        raise ValueError("LSP message without a Content-Length header")
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def path_to_uri(path):
    return Path(path).resolve().as_uri()


def uri_key(uri):
    """Key used to store diagnostics, so two spellings of one file URI match.

    vhdl_ls does not always write a URI the same way Python does (it may
    percent-encode some characters, and on Windows the drive letter can come
    back in a different case). Without this a file with errors could look clean.
    """
    if not isinstance(uri, str):
        return uri
    parsed = urlparse(uri)
    path = unquote(parsed.path).replace("\\", "/")
    if len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
        path = path.lower()  # Windows paths are not case sensitive
    return f"{parsed.scheme.lower()}://{parsed.netloc}{path}"


# ------------------------------------------------------------ vhdl_ls process

class VhdlLsProcess:
    def __init__(self, root_dir, command=None, request_timeout=10.0,
                 diagnostics_timeout=5.0, settle_time=0.3):
        self.root_dir = Path(root_dir).resolve()
        self.command = command or vhdl_ls_command()
        self.request_timeout = request_timeout
        self.diagnostics_timeout = diagnostics_timeout
        # vhdl_ls can send several diagnostics messages for one change, so we
        # wait this long after the last one before using the result.
        self.settle_time = settle_time

        self._proc = None
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}      # request id -> {"event": Event, "response": dict}
        self._versions = {}     # file key -> version number sent to vhdl_ls
        self._diags = {}        # file key -> latest diagnostics list from vhdl_ls
        self._diag_event = {}   # file key -> set when diagnostics arrive
        self._diag_time = {}    # file key -> when the latest diagnostics arrived
        self._dead = threading.Event()
        self._closing = False
        self._stderr_tail = []
        self._bad_output = None

    def start(self):
        if self._proc is not None:
            raise LanguageServerError("vhdl_ls is already running")
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=str(self.root_dir))
        except OSError as exc:  # not installed, not executable, ...
            raise LanguageServerDied(f"cannot start {self.command[0]!r}: {exc}") from exc

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        try:
            self._request("initialize", {
                "processId": os.getpid(),
                "rootUri": self.root_dir.as_uri(),
                "capabilities": {"textDocument": {
                    "publishDiagnostics": {},
                    "synchronization": {"didSave": False},
                    "completion": {"completionItem": {"snippetSupport": False}},
                }},
                "workspaceFolders": [{"uri": self.root_dir.as_uri(), "name": "session"}],
            })
            self._notify("initialized", {})
        except LanguageServerError as exc:
            self.close()
            raise LanguageServerDied(f"vhdl_ls did not start properly: {exc}") from exc

    def is_alive(self):
        return (self._proc is not None and self._proc.poll() is None
                and not self._dead.is_set())

    def close(self):
        """Polite shutdown first, then make sure the process is gone. Safe to call twice."""
        proc = self._proc
        if proc is None:
            return
        self._closing = True
        try:
            if self.is_alive():
                try:
                    self._request("shutdown", None, timeout=3.0)
                    self._notify("exit", None)
                except LanguageServerError:
                    pass
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
            self._dead.set()
            self._wake_all()
            self._proc = None

    # -------------------------------------------------------------- documents

    def open_file(self, path, text):
        """Send the whole file: didOpen the first time, didChange after that."""
        self._check_alive()
        uri = path_to_uri(path)
        key = uri_key(uri)
        with self._lock:
            version = self._versions.get(key, 0) + 1
            self._versions[key] = version
            self._diag_event[key] = threading.Event()  # wait for new diagnostics
        if version == 1:
            self._notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": "vhdl", "version": 1, "text": text}})
        else:
            self._notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}]})

    def close_file(self, path):
        self._check_alive()
        uri = path_to_uri(path)
        key = uri_key(uri)
        with self._lock:
            self._versions.pop(key, None)
            self._diags.pop(key, None)
            self._diag_event.pop(key, None)
        self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    def get_diagnostics(self, path, timeout=None):
        """Diagnostics for path after the latest open_file.

        vhdl_ls only sends diagnostics when there is something to report or
        something to clear, so for a clean file it may send nothing at all.
        Silence could also mean it has hung, so we send a cheap completion
        request and wait for its answer. vhdl_ls handles messages in order,
        so once it answers, the diagnostics for the change (if any) are in.
        """
        timeout = self.diagnostics_timeout if timeout is None else timeout
        uri = path_to_uri(path)
        key = uri_key(uri)
        event = self._diag_event.get(key)
        if event is None:
            raise LanguageServerError(f"file is not open: {Path(path).name}")
        self._check_alive()
        try:
            self._request("textDocument/completion", {
                "textDocument": {"uri": uri},
                "position": {"line": 0, "character": 0}}, timeout=timeout)
        except (LanguageServerDied, LanguageServerTimeout):
            raise
        except LanguageServerError:
            pass  # an error answer still shows vhdl_ls is alive and up to date

        event.wait(timeout=self.settle_time)
        self._check_alive()
        if not event.is_set():
            return []  # nothing was sent for this version: the file is clean
        while True:
            last = self._diag_time.get(key, 0.0)
            time.sleep(self.settle_time)
            self._check_alive()
            if self._diag_time.get(key, 0.0) == last:
                break
        return list(self._diags.get(key, []))

    def latest_diagnostics(self, path):
        """The last diagnostics vhdl_ls sent for path, without waiting.

        None if it has sent nothing since the file was opened. Used to update
        the other open files after one file changes (a testbench can become
        correct once its entity is opened, for example).
        """
        key = uri_key(path_to_uri(path))
        with self._lock:
            event = self._diag_event.get(key)
            if event is None or not event.is_set():
                return None
            return list(self._diags.get(key, []))

    def completion(self, path, line, col):
        """Completion items at a 0-based position."""
        self._check_alive()
        result = self._request("textDocument/completion", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": col}})
        if result is None:
            return []
        if isinstance(result, dict):
            return list(result.get("items", []))
        return list(result)

    # ---------------------------------------------------------------- plumbing

    def _died(self):
        if self._bad_output:
            return LanguageServerDied(f"vhdl_ls sent a message that is not LSP: {self._bad_output}")
        code = self._proc.poll() if self._proc else None
        tail = " | ".join(self._stderr_tail[-3:])
        return LanguageServerDied(f"vhdl_ls terminated unexpectedly (exit code {code}). {tail}".strip())

    def _check_alive(self):
        if self._proc is None:
            raise LanguageServerDied("vhdl_ls is not running")
        if not self.is_alive():
            raise self._died()

    def _send(self, payload):
        data = encode_message(payload)
        with self._write_lock:
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (OSError, ValueError, AttributeError) as exc:
                self._dead.set()
                raise self._died() from exc

    def _notify(self, method, params):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _request(self, method, params, timeout=None):
        timeout = self.request_timeout if timeout is None else timeout
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "response": None}
            self._pending[req_id] = slot
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None or method == "shutdown":
            msg["params"] = params
        try:
            self._send(msg)
            if not slot["event"].wait(timeout):
                raise LanguageServerTimeout(f"{method} got no answer in {timeout} s")
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
        response = slot["response"]
        if response is None:  # woken up because the process ended
            raise self._died()
        if "error" in response:
            raise LanguageServerError(f"{method} failed: {response['error']}")
        return response.get("result")

    def _read_stdout(self):
        stdout = self._proc.stdout
        try:
            while True:
                msg = read_message(stdout)
                if msg is None:
                    break
                self._handle(msg)
        except Exception as exc:  # broken framing or JSON from vhdl_ls
            if not self._closing:
                self._bad_output = f"{type(exc).__name__}: {exc}"
        finally:
            self._dead.set()
            self._wake_all()

    def _handle(self, msg):
        if "method" in msg:
            if msg["method"] == "textDocument/publishDiagnostics":
                params = msg.get("params") or {}
                key = uri_key(params.get("uri"))
                with self._lock:
                    self._diags[key] = params.get("diagnostics") or []
                    self._diag_time[key] = time.monotonic()
                    event = self._diag_event.setdefault(key, threading.Event())
                event.set()
            elif "id" in msg:
                # A request from vhdl_ls to us (e.g. client/registerCapability).
                try:
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
                except LanguageServerError:
                    pass
            # Other notifications (window/logMessage, ...) are ignored.
        elif "id" in msg:
            with self._lock:
                slot = self._pending.get(msg["id"])
            if slot:
                slot["response"] = msg
                slot["event"].set()

    def _wake_all(self):
        """Wake every waiting request so it can report that vhdl_ls has gone."""
        with self._lock:
            slots = list(self._pending.values())
            events = list(self._diag_event.values())
        for slot in slots:
            slot["event"].set()
        for event in events:
            event.set()

    def _read_stderr(self):
        try:
            for raw in iter(self._proc.stderr.readline, b""):
                self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip())
                del self._stderr_tail[:-20]
        except (OSError, ValueError, AttributeError):
            pass


# -------------------------------------------------------- async wrapper

class Bridge:
    """Awaitable version of VhdlLsProcess, used by the Gateway.

    Every call runs in a worker thread so a slow vhdl_ls never blocks the
    event loop (and with it every other WebSocket connection).
    """

    def __init__(self, process):
        self._process = process

    @property
    def is_alive(self):
        return self._process.is_alive()

    async def open_file(self, path, text):
        await asyncio.to_thread(self._process.open_file, path, text)

    async def close_file(self, path):
        await asyncio.to_thread(self._process.close_file, path)

    async def get_diagnostics(self, path, timeout):
        return await asyncio.to_thread(self._process.get_diagnostics, path, timeout)

    def latest_diagnostics(self, path):
        return self._process.latest_diagnostics(path)

    async def completion(self, path, line, col):
        return await asyncio.to_thread(self._process.completion, path, line, col)

    async def close(self):
        await asyncio.to_thread(self._process.close)


async def start_bridge(session_dir, command=None):
    """Start vhdl_ls for one session folder and finish the LSP handshake."""
    process = VhdlLsProcess(session_dir, command=command)
    await asyncio.to_thread(process.start)
    return Bridge(process)
