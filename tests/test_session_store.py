"""Tests for gateway/session_store.py: the shared file/session model on disk (FR-P5B-03, FR-P5B-07)."""
import re

import pytest

from gateway.session_store import InvalidFilename, SessionStore, UnknownSession, is_allowed_filename

# Copied from P5A's app/docker_runner.py (FILE_NAME_RE).
P5A_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}\.(vhd|vhdl)$", re.I)

GOOD_NAMES = ["design.vhd", "and_gate.vhdl", "A1_b2.vhd", "FX-01.vhd", "and-gate.vhd",
              "a.b.vhd", "AND_GATE.VHD", "_x.vhd", "a" * 96 + ".vhd"]
BAD_NAMES = ["", "x.txt", "x.v", "a b.vhd", "../x.vhd", "a/b.vhd", "a\\b.vhd", "x.vhd\n",
             ".vhd", "-x.vhd", ".hidden.vhd", "x.VHD.exe", "x", "x.vhd.txt", "a" * 97 + ".vhd",
             None, 5]


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path)


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_allowed_names_are_also_allowed_by_p5a(name):
    assert is_allowed_filename(name)
    assert P5A_FILE_NAME_RE.match(name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_names_outside_the_prescribed_set_are_rejected(name):
    assert not is_allowed_filename(name)


def test_layout(store, tmp_path):
    sid = store.create_session()
    folder = tmp_path / "sessions" / sid
    assert (folder / "src").is_dir()
    assert (folder / "vhdl_ls.toml").is_file()
    assert store.list_files(sid) == []


def test_write_and_read_back(store, tmp_path):
    sid = store.create_session()
    path = store.write_file(sid, "design.vhd", "-- hello\n")
    assert path == tmp_path / "sessions" / sid / "src" / "design.vhd"
    assert store.read_file(sid, "design.vhd") == "-- hello\n"
    assert store.list_files(sid) == ["design.vhd"]


@pytest.mark.parametrize("text", ["a\nb\n", "a\r\nb\r\n", "a\rb"])
def test_line_endings_are_kept_exactly(store, tmp_path, text):
    # On Windows a text-mode write would change them and shift the line numbers.
    sid = store.create_session()
    store.write_file(sid, "design.vhd", text)
    assert (tmp_path / "sessions" / sid / "src" / "design.vhd").read_bytes() == text.encode("utf-8")
    assert store.read_file(sid, "design.vhd") == text


def test_toml_lists_src_and_leaves_the_standard_at_vhdl_2008(store, tmp_path):
    sid = store.create_session()
    toml = (tmp_path / "sessions" / sid / "vhdl_ls.toml").read_text()
    assert "src/*.vhd" in toml and "src/*.vhdl" in toml
    assert "standard" not in toml


def test_bad_name_is_rejected_and_nothing_is_written(store):
    sid = store.create_session()
    with pytest.raises(InvalidFilename):
        store.write_file(sid, "../escape.vhd", "x")
    assert store.list_files(sid) == []


def test_sessions_are_separate(store):
    a = store.create_session()
    b = store.create_session()
    assert a != b
    store.write_file(a, "design.vhd", "A")
    assert store.list_files(b) == []


def test_delete_removes_everything(store, tmp_path):
    sid = store.create_session()
    store.write_file(sid, "design.vhd", "x")
    store.delete_session(sid)
    assert not (tmp_path / "sessions" / sid).exists()
    with pytest.raises(UnknownSession):
        store.src_dir(sid)


@pytest.mark.parametrize("sid", ["", "../..", "abc", "g" * 32, None])
def test_bad_session_ids(store, sid):
    with pytest.raises(UnknownSession):
        store.session_dir(sid)
