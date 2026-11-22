"""Session folders on disk: the shared file/session model.

    <root>/sessions/<session_id>/
        src/            the VHDL files of the session (the shared file set)
        vhdl_ls.toml    tells vhdl_ls which files belong to the project

A file in src/ is stored exactly as the client sent it, under the same name
it will have in a P5A request, so the same file set can be sent to P5A's
POST /simulate without any changes (see SessionManager.p5a_request).
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from gateway.invariants import check_inside

# P5A's file name rule (FILE_NAME_RE in P5A's app/docker_runner.py): letters,
# digits, "_", "." and "-", not starting with "." or "-", at most 100
# characters, ending in .vhd or .vhdl in any letter case. fullmatch is used,
# so unlike P5A's "$" a trailing newline is not let through.
FILE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}\.(vhd|vhdl)", re.IGNORECASE)
MAX_NAME_LENGTH = 100
SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")

# No "standard" line, so vhdl_ls uses its default, VHDL-2008. parent_main.py
# makes P5A's GHDL use 2008 as well, so both check the same version of VHDL.
VHDL_LS_TOML = """\
[libraries]
proj.files = [
  "src/*.vhd",
  "src/*.vhdl",
]
"""


class InvalidFilename(ValueError):
    """The name is not in the prescribed file set."""


class UnknownSession(KeyError):
    """No session folder with this id."""


def is_allowed_filename(name):
    return (isinstance(name, str) and len(name) <= MAX_NAME_LENGTH
            and FILE_NAME_RE.fullmatch(name) is not None)


class SessionStore:
    def __init__(self, root):
        self.root = Path(root) / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def create_session(self):
        session_id = uuid.uuid4().hex
        folder = self.root / session_id
        (folder / "src").mkdir(parents=True)
        (folder / "vhdl_ls.toml").write_text(VHDL_LS_TOML, encoding="utf-8")
        return session_id

    def delete_session(self, session_id):
        folder = self.session_dir(session_id)
        check_inside(folder, self.root)      # never delete anything else
        shutil.rmtree(folder)

    def session_dir(self, session_id):
        if not isinstance(session_id, str) or SESSION_ID_RE.fullmatch(session_id) is None:
            raise UnknownSession(session_id)
        folder = self.root / session_id
        if not folder.is_dir():
            raise UnknownSession(session_id)
        return folder

    def src_dir(self, session_id):
        return self.session_dir(session_id) / "src"

    def file_path(self, session_id, filename):
        if not is_allowed_filename(filename):
            raise InvalidFilename(filename)
        return self.src_dir(session_id) / filename

    def write_file(self, session_id, filename, text):
        path = self.file_path(session_id, filename)
        # Written as bytes: on Windows write_text would turn "\n" into "\r\n",
        # so the file on disk would not match what the client sent.
        path.write_bytes(text.encode("utf-8"))
        return path

    def read_file(self, session_id, filename):
        return self.file_path(session_id, filename).read_bytes().decode("utf-8")

    def list_files(self, session_id):
        return sorted(p.name for p in self.src_dir(session_id).iterdir() if p.is_file())
