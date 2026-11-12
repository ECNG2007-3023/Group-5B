"""The approved subset of LSP methods, and the checks on their params.

Anything not in ALLOWED (hover, definition, rename, formatting, ...) is
rejected with -32601 and never reaches vhdl_ls, even though vhdl_ls supports it.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from gateway import mappers
from gateway.jsonrpc import INVALID_PARAMS, INVALID_REQUEST, GatewayError, notification


class _Params(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Position(_Params):
    line: StrictInt = Field(ge=0)
    character: StrictInt = Field(ge=0)


class Document(_Params):
    uri: StrictStr


class DocumentWithText(Document):
    text: StrictStr
    version: StrictInt | None = None


class VersionedDocument(Document):
    version: StrictInt | None = None


class Change(_Params):
    text: StrictStr
    range: dict | None = None


class DidOpenParams(_Params):
    textDocument: DocumentWithText


class DidChangeParams(_Params):
    textDocument: VersionedDocument
    contentChanges: list[Change] = Field(min_length=1)


class DidCloseParams(_Params):
    textDocument: Document


class CompletionParams(_Params):
    textDocument: Document
    position: Position


def parse(model, params):
    try:
        return model.model_validate(params)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) or "params" for e in exc.errors())
        raise GatewayError(INVALID_PARAMS, "Invalid params", "invalid_params",
                           f"Invalid or missing: {fields}.")


async def initialize(conn, params):
    """Opens the session: makes its folder and starts vhdl_ls. Files come later with didOpen."""
    conn.reject_if_shutdown()
    if conn.session is not None:
        raise GatewayError(INVALID_REQUEST, "Already initialized", "session_error",
                           "This connection already has an active session.")
    session = await conn.open_session()
    return {"capabilities": {"textDocumentSync": 1,
                             "completionProvider": {"resolveProvider": False}},
            "serverInfo": {"name": "p5b-gateway", "version": "1"},
            "sessionId": session.id}


async def initialized(conn, params):
    return None


async def _publish(conn, session, name, raw):
    params = mappers.to_publish_diagnostics(mappers.name_to_uri(name), session.versions[name],
                                            raw, session.scrub)
    session.published[name] = params["diagnostics"]
    await conn.send(notification("textDocument/publishDiagnostics", params))


async def _update_file(conn, session, name, text, version):
    """Save the file, give it to vhdl_ls and send its diagnostics back."""
    cfg = conn.cfg
    if len(text.encode("utf-8")) > cfg.max_file_bytes:
        raise GatewayError(INVALID_PARAMS, "File too large", "invalid_params",
                           f"A file may be at most {cfg.max_file_bytes} bytes.")
    if name not in session.versions and len(session.versions) >= cfg.max_files:
        raise GatewayError(INVALID_PARAMS, "Too many files", "invalid_params",
                           f"A session may hold at most {cfg.max_files} files.")

    # Disk first: vhdl_ls and P5A both read the files in src/.
    await conn.manager.write_file(session, name, text)
    if version is None:
        version = session.versions.get(name, 0) + 1
    session.versions[name] = version
    session.opened.add(name)

    path = session.path_for(name)
    await conn.call_bridge(session.bridge.open_file(path, text))
    raw = await conn.call_bridge(session.bridge.get_diagnostics(path, cfg.diagnostics_wait_s))
    await _publish(conn, session, name, raw)

    # A change to one file can change another file's diagnostics (a testbench
    # stops reporting "no primary unit" once its entity is opened), so send
    # any other open file whose diagnostics are now different.
    for other in sorted(session.opened - {name}):
        latest = session.bridge.latest_diagnostics(session.path_for(other))
        if latest is None:
            continue
        mapped = mappers.to_publish_diagnostics("", 0, latest, session.scrub)["diagnostics"]
        if mapped != session.published.get(other):
            await _publish(conn, session, other, latest)


async def did_open(conn, params):
    session = conn.require_ready()
    p = parse(DidOpenParams, params)
    name = session.resolve_uri(p.textDocument.uri)
    await _update_file(conn, session, name, p.textDocument.text, p.textDocument.version)


async def did_change(conn, params):
    session = conn.require_ready()
    p = parse(DidChangeParams, params)
    name = session.resolve_uri(p.textDocument.uri)
    if any(change.range is not None for change in p.contentChanges):
        raise GatewayError(INVALID_PARAMS, "Invalid params", "invalid_params",
                           "Only full-document sync is supported; send the whole text without a range.")
    await _update_file(conn, session, name, p.contentChanges[-1].text, p.textDocument.version)


async def did_close(conn, params):
    session = conn.require_ready()
    p = parse(DidCloseParams, params)
    name = session.resolve_uri(p.textDocument.uri)
    if name in session.opened:
        session.opened.discard(name)
        session.published.pop(name, None)
        await conn.call_bridge(session.bridge.close_file(session.path_for(name)))


async def completion(conn, params):
    session = conn.require_ready()
    p = parse(CompletionParams, params)
    name = session.resolve_uri(p.textDocument.uri)
    if name not in session.opened:
        raise GatewayError(INVALID_PARAMS, "File is not open", "invalid_params",
                           "Send textDocument/didOpen for this file first.")
    raw = await conn.call_bridge(session.bridge.completion(
        session.path_for(name), p.position.line, p.position.character))
    return mappers.to_completion(raw, session.scrub)


async def shutdown(conn, params):
    await conn.end_session()
    return None


async def exit_(conn, params):
    await conn.end_session()
    conn.should_close = True


ALLOWED = {
    "initialize": initialize,
    "initialized": initialized,
    "textDocument/didOpen": did_open,
    "textDocument/didChange": did_change,
    "textDocument/didClose": did_close,
    "textDocument/completion": completion,
    "shutdown": shutdown,
    "exit": exit_,
}
