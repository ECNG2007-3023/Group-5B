"""Tests for gateway/vhdl_ls_bridge.py, run against tests/fake_vhdl_ls.py.

They check the LSP plumbing (framing, waiting for diagnostics, errors,
shutdown). What vhdl_ls itself reports is checked in test_real_vhdl_ls.py.
"""
import asyncio
import io
import sys
from pathlib import Path

import pytest

from gateway.vhdl_ls_bridge import (LanguageServerDied, LanguageServerError, LanguageServerTimeout,
                                    encode_message, read_message, start_bridge, uri_key)

FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_vhdl_ls.py")]

VALID = "entity e is\nend entity e;\n\narchitecture rtl of e is\nbegin\n  y <= a and b;\nend architecture rtl;\n"
BROKEN = VALID.replace("a and b;", "a and b")
SIGNALS = ("architecture rtl of e is\n  signal sig_clock : bit;\n  signal sig_reset : bit;\n"
           "begin\nend architecture rtl;\n")


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_LS_MODE", raising=False)
    monkeypatch.delenv("FAKE_LS_URI_STYLE", raising=False)
    (tmp_path / "src").mkdir()
    (tmp_path / "vhdl_ls.toml").write_text('[libraries]\nproj.files = ["src/*.vhd"]\n')
    return tmp_path


def run(coro):
    return asyncio.run(coro)


async def started(session_dir):
    return await start_bridge(session_dir, command=FAKE)


def test_framing_round_trip():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"text": "é"}}
    assert read_message(io.BytesIO(encode_message(msg))) == msg
    assert read_message(io.BytesIO(b"")) is None


def test_valid_then_broken_then_fixed(session_dir):
    path = session_dir / "src" / "design.vhd"

    async def go():
        bridge = await started(session_dir)
        try:
            assert bridge.is_alive is True
            await bridge.open_file(path, VALID)
            assert await bridge.get_diagnostics(path, 2.0) == []
            await bridge.open_file(path, BROKEN)            # sent as didChange
            diags = await bridge.get_diagnostics(path, 2.0)
            assert len(diags) == 1 and diags[0]["severity"] == 1
            assert diags[0]["range"]["start"]["line"] == 5
            assert bridge.latest_diagnostics(path) == diags
            await bridge.open_file(path, VALID)
            assert await bridge.get_diagnostics(path, 2.0) == []
        finally:
            await bridge.close()
    run(go())


def test_diagnostics_are_found_when_the_uri_is_written_differently(session_dir, monkeypatch):
    monkeypatch.setenv("FAKE_LS_URI_STYLE", "encoded")
    path = session_dir / "src" / "design.vhd"

    async def go():
        bridge = await started(session_dir)
        try:
            await bridge.open_file(path, BROKEN)
            diags = await bridge.get_diagnostics(path, 2.0)
            assert len(diags) == 1 and diags[0]["severity"] == 1
        finally:
            await bridge.close()
    run(go())


def test_completion_returns_the_declared_signals(session_dir):
    path = session_dir / "src" / "design.vhd"

    async def go():
        bridge = await started(session_dir)
        try:
            await bridge.open_file(path, SIGNALS)
            await bridge.get_diagnostics(path, 2.0)
            items = await bridge.completion(path, 3, 0)
            assert {i["label"] for i in items} == {"sig_clock", "sig_reset"}
        finally:
            await bridge.close()
    run(go())


def test_latest_diagnostics_is_none_until_something_is_sent(session_dir):
    path = session_dir / "src" / "design.vhd"

    async def go():
        bridge = await started(session_dir)
        try:
            assert bridge.latest_diagnostics(path) is None
            await bridge.open_file(path, VALID)
            await bridge.get_diagnostics(path, 2.0)
            assert bridge.latest_diagnostics(path) is None     # clean file: nothing was sent
        finally:
            await bridge.close()
    run(go())


def test_dead_process_raises_died_and_is_not_alive(session_dir, monkeypatch):
    monkeypatch.setenv("FAKE_LS_MODE", "die")
    path = session_dir / "src" / "design.vhd"

    async def go():
        bridge = await started(session_dir)
        try:
            await bridge.open_file(path, VALID)
            with pytest.raises(LanguageServerDied):
                await bridge.get_diagnostics(path, 2.0)        # the request kills the fake
            assert bridge.is_alive is False
            with pytest.raises(LanguageServerDied):
                await bridge.completion(path, 0, 0)
        finally:
            await bridge.close()
    run(go())


def test_silent_process_raises_timeout(session_dir, monkeypatch):
    monkeypatch.setenv("FAKE_LS_MODE", "hang")
    path = session_dir / "src" / "design.vhd"

    async def go():
        bridge = await started(session_dir)
        try:
            await bridge.open_file(path, VALID)
            with pytest.raises(LanguageServerTimeout):
                await bridge.get_diagnostics(path, 0.5)
        finally:
            await bridge.close()
    run(go())


def test_error_answer_is_a_plain_language_server_error(session_dir, monkeypatch):
    monkeypatch.setenv("FAKE_LS_MODE", "error")
    path = session_dir / "src" / "design.vhd"

    async def go():
        bridge = await started(session_dir)
        try:
            await bridge.open_file(path, VALID)
            assert await bridge.get_diagnostics(path, 2.0) == []   # still works
            with pytest.raises(LanguageServerError) as exc:
                await bridge.completion(path, 0, 0)
            assert not isinstance(exc.value, (LanguageServerDied, LanguageServerTimeout))
            assert bridge.is_alive is True
        finally:
            await bridge.close()
    run(go())


def test_missing_program_raises_died(session_dir):
    with pytest.raises(LanguageServerDied):
        run(start_bridge(session_dir, command=["no_such_vhdl_ls_program"]))


def test_close_twice_is_fine_and_stops_the_process(session_dir):
    async def go():
        bridge = await started(session_dir)
        await bridge.close()
        await bridge.close()
        assert bridge.is_alive is False
    run(go())


# ---- uri_key: two spellings of one file URI give the same key ---------------------------------

def test_uri_key_percent_encoding():
    assert uri_key("file:///tmp/s1/src/%64esign.vhd") == uri_key("file:///tmp/s1/src/design.vhd")


def test_uri_key_windows_drive_letter_and_case():
    a = uri_key("file:///C:/Users/Ethan/Temp/p5b/src/Design.vhd")
    b = uri_key("file:///c%3A/users/ethan/temp/p5b/src/design.vhd")
    assert a == b


def test_uri_key_spaces():
    assert uri_key("file:///C:/Users/Ethan%20Smith/a.vhd") == uri_key("file:///c:/Users/Ethan Smith/a.vhd")


def test_uri_key_linux_paths_stay_case_sensitive():
    assert uri_key("file:///tmp/A.vhd") != uri_key("file:///tmp/a.vhd")
    assert uri_key("file:///tmp/s1/src/a.vhd") != uri_key("file:///tmp/s1/src/b.vhd")
    assert uri_key(None) is None
