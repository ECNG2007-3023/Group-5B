"""Tests for clients/checks.py, the helpers the test client uses to judge replies."""
from clients import checks

SEMICOLON = {"expect": "errors", "min_errors": 1, "lines": [12], "message_contains": "';'"}


def diag(line, severity=1, message="Expected ';' or 'when'"):
    return {"severity": severity, "message": message, "range": {"start": {"line": line}}}


def test_none_passes_only_on_an_empty_list():
    assert checks.check_diagnostics([], {"expect": "none"}) == []
    assert checks.check_diagnostics([diag(0)], {"expect": "none"})


def test_error_on_the_expected_line_passes():
    assert checks.check_diagnostics([diag(12)], SEMICOLON) == []


def test_error_on_another_line_fails():
    assert checks.check_diagnostics([diag(3)], SEMICOLON)


def test_a_warning_is_not_an_error():
    assert checks.check_diagnostics([diag(12, severity=2)], SEMICOLON)


def test_message_must_contain_the_text():
    assert checks.check_diagnostics([diag(12, message="something else")], SEMICOLON)


def test_unknown_expectation_is_reported():
    assert checks.check_diagnostics([], {"expect": "banana"})


def test_completion_with_list_or_dict():
    items = [{"label": "sig_clock"}, {"label": "sig_reset"}]
    expected = {"must_include": ["sig_clock", "sig_reset"]}
    assert checks.check_completion(items, expected) == []
    assert checks.check_completion({"items": items}, expected) == []
    assert checks.check_completion([{"label": "sig_clock"}], expected)
    assert checks.completion_items(None) == []


def test_error_checks():
    hover = {"id": 1, "error": {"code": -32601, "message": "Method not supported: textDocument/hover"}}
    assert checks.check_error(hover, -32601, "textDocument/hover") == []
    assert checks.check_error(hover, -32700)
    assert checks.check_error({"id": 1, "error": {"code": -32601, "message": "nope"}},
                              -32601, "textDocument/hover")
    success = {"id": 1, "result": []}
    assert checks.check_error(success, -32601)
    assert checks.is_success(success) and not checks.is_error(success)
    assert checks.is_error(hover) and not checks.is_success(hover)
