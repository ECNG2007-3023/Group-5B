"""JSON-RPC 2.0 messages: reading the client's message and building replies.

Only the standard JSON-RPC error codes are used, plus LSP's -32002 for
"send initialize first". What went wrong is also given in error.data.category.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603      # also used when vhdl_ls stops (category says which)
NOT_INITIALIZED = -32002     # LSP: ServerNotInitialized

STAGE = "language_service"


class GatewayError(Exception):
    """An error the client is told about. The text never contains host paths."""

    def __init__(self, code, message, category, detail="", id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.detail = detail or message
        self.id = id


@dataclass
class Message:
    method: str
    params: object
    id: object
    is_notification: bool


def result_response(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}


def error_response(id, code, message, category, detail=""):
    # "failed" is the same word P5A uses in its results.
    return {"jsonrpc": "2.0", "id": id, "error": {
        "code": code, "message": message,
        "data": {"status": "failed", "category": category, "stage": STAGE,
                 "detail": detail or message}}}


def error_from(exc, id=None):
    return error_response(exc.id if id is None else id, exc.code, exc.message,
                          exc.category, exc.detail)


def notification(method, params):
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _is_valid_id(value):
    if isinstance(value, bool):
        return False
    return value is None or isinstance(value, (str, int))


def parse_message(raw, max_bytes):
    """Check one incoming message. Raises GatewayError if it is not valid JSON-RPC."""
    if len(raw) > max_bytes or len(raw.encode("utf-8", "ignore")) > max_bytes:
        raise GatewayError(INVALID_REQUEST, "Message too large", "invalid_request",
                           f"Messages must be at most {max_bytes} bytes.")
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        raise GatewayError(PARSE_ERROR, "Parse error: message is not valid JSON",
                           "invalid_request", "Message is not valid JSON.")
    if isinstance(obj, list):
        raise GatewayError(INVALID_REQUEST, "Invalid Request", "invalid_request",
                           "Batch requests are not supported.")
    if not isinstance(obj, dict):
        raise GatewayError(INVALID_REQUEST, "Invalid Request", "invalid_request",
                           "Message must be a JSON object.")

    request_id = obj.get("id") if _is_valid_id(obj.get("id")) else None
    if obj.get("jsonrpc") != "2.0":
        raise GatewayError(INVALID_REQUEST, "Invalid Request", "invalid_request",
                           "'jsonrpc' must be \"2.0\".", id=request_id)
    method = obj.get("method")
    if not isinstance(method, str) or not method:
        raise GatewayError(INVALID_REQUEST, "Invalid Request", "invalid_request",
                           "'method' must be a non-empty string.", id=request_id)
    if "id" in obj and not _is_valid_id(obj["id"]):
        raise GatewayError(INVALID_REQUEST, "Invalid Request", "invalid_request",
                           "'id' must be a string, number or null.")
    params = obj.get("params")
    if params is None:  # LSP clients often leave params out
        params = {}
    if not isinstance(params, (dict, list)):
        raise GatewayError(INVALID_REQUEST, "Invalid Request", "invalid_request",
                           "'params' must be an object or array.", id=request_id)
    return Message(method=method, params=params, id=obj.get("id"),
                   is_notification="id" not in obj)
