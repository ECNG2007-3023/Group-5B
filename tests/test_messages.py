"""Unit tests for gateway/jsonrpc.py and gateway/mappers.py (no WebSocket, no vhdl_ls)."""
import json

import pytest

from gateway import mappers
from gateway.jsonrpc import GatewayError, parse_message

MAX = 10_000


def error_code(raw):
    with pytest.raises(GatewayError) as exc:
        parse_message(raw, MAX)
    return exc.value.code


def test_request_and_notification_are_told_apart():
    r = parse_message('{"jsonrpc":"2.0","id":1,"method":"initialize"}', MAX)
    assert not r.is_notification and r.id == 1 and r.params == {}
    n = parse_message('{"jsonrpc":"2.0","method":"initialized","params":null}', MAX)
    assert n.is_notification and n.params == {}


def test_bad_messages_get_the_right_code():
    assert error_code("{not json") == -32700
    assert error_code("[]") == -32600                          # batches are not supported
    assert error_code("42") == -32600
    assert error_code('{"jsonrpc":"1.0","method":"x"}') == -32600
    assert error_code('{"jsonrpc":"2.0","method":5}') == -32600
    assert error_code('{"jsonrpc":"2.0","id":true,"method":"x"}') == -32600
    assert error_code('{"jsonrpc":"2.0","method":"x","params":3}') == -32600
    assert error_code(json.dumps({"jsonrpc": "2.0", "method": "x" * (MAX + 1)})) == -32600


def test_diagnostics_are_sorted_cleaned_and_summarised():
    raw = [
        {"range": {"start": {"line": 5, "character": 1}, "end": {"line": 5, "character": 2}},
         "severity": 2, "message": "w"},
        {"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}},
         "severity": "error", "code": {"value": "E1"}, "message": "bad /tmp/s1/src/a.vhd here"},
    ]
    out = mappers.to_publish_diagnostics("file:///a.vhd", 3, raw, mappers.make_scrubber("/tmp/s1"))
    assert [d["range"]["start"]["line"] for d in out["diagnostics"]] == [1, 5]
    assert out["diagnostics"][0]["message"] == "bad a.vhd here"
    assert out["diagnostics"][0]["code"] == "E1"
    assert out["diagnostics"][1]["source"] == "vhdl_ls"
    assert out["summary"] == "1 error and 1 warning found."
    assert out["uri"] == "file:///a.vhd" and out["version"] == 3


def test_scrubber_handles_windows_paths_and_uris():
    base = r"C:\Users\Ethan Smith\Temp\p5b\sessions\abc123"
    scrub = mappers.make_scrubber(base)
    assert scrub(base + r"\src\design.vhd" + " line 3") == "design.vhd line 3"
    assert scrub("at file:///C:/Users/Ethan%20Smith/Temp/p5b/sessions/abc123/src/design.vhd") == "at design.vhd"
    assert scrub("root " + base) == "root <session>"


def test_summary_wording():
    assert mappers.summarize([]) == "No errors present."
    assert mappers.summarize([{"severity": 2}]) == "No errors present; 1 warning found."
    assert mappers.summarize([{"severity": 1}, {"severity": 1}]) == "2 errors found."
    three = [{"severity": 1}, {"severity": 2}, {"severity": 4}]
    assert mappers.summarize(three) == "1 error, 1 warning and 1 hint found."


@pytest.mark.parametrize("raw", [None, [], {"items": []}])
def test_empty_completion_shapes(raw):
    assert mappers.to_completion(raw) == {"isIncomplete": False, "items": []}


def test_completion_list_and_completionlist_give_the_same_result():
    items = [{"label": "b", "kind": 6}, {"label": "a", "textEdit": {"newText": "A"}},
             {"label": "b", "kind": 6}]
    a = mappers.to_completion(items)
    b = mappers.to_completion({"isIncomplete": False, "items": items})
    assert a == b
    assert [i["label"] for i in a["items"]] == ["a", "b"]      # sorted, duplicate removed
    assert a["items"][0]["insertText"] == "A"


@pytest.mark.parametrize("uri,name", [
    ("file:///design.vhd", "design.vhd"), ("design.vhd", "design.vhd"),
    ("file:///and_gate.vhdl", "and_gate.vhdl"), ("FX-01.vhd", "FX-01.vhd"),
    ("file:///and-gate.vhd", "and-gate.vhd"), ("file:///a.b.vhd", "a.b.vhd"),
    ("file:///AND_GATE.VHD", "AND_GATE.VHD"),
    ("file:///x/y/and_gate.vhdl", None), ("file:///../x.vhd", None), ("file://host/design.vhd", None),
    ("file:///bad name.txt", None), ("file:///bad%20name.vhd", None), ("../etc/passwd", None),
    ("a\\b.vhd", None), ("http://h/a.vhd", None), ("a.txt", None), (5, None),
    ("file:///-x.vhd", None), ("file:///x.vhd%0A", None)])
def test_uri_to_name(uri, name):
    assert mappers.uri_to_name(uri) == name


def test_safe_text_is_short_and_printable():
    assert mappers.safe_text("a\nb\x00" + "x" * 200).startswith("a?b?")
    assert len(mappers.safe_text("x" * 200)) == 80
