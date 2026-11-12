"""Settings for the P5B Language-Service Gateway.

Every value can be overridden with an environment variable, e.g. in PowerShell:
    $env:P5B_MAX_SESSIONS = "4"
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _env(name, default, cast=str):
    value = os.environ.get(name)
    return cast(value) if value not in (None, "") else default


@dataclass(frozen=True)
class Config:
    # "real" runs vhdl_ls for each session, "fake" uses gateway/fake_bridge.py.
    bridge: str = "real"
    # Session folders go in <session_root>/sessions/. Empty = the system temp folder.
    session_root: str = ""
    max_sessions: int = 8
    max_files: int = 20               # per session, same as P5A
    max_file_bytes: int = 200_000     # per file, same as P5A
    max_message_bytes: int = 1_000_000
    idle_timeout_s: float = 300.0     # a session with no messages for this long is closed
    request_timeout_s: float = 10.0   # longest wait for any one vhdl_ls call
    diagnostics_timeout_s: float = 5.0
    watchdog_interval_s: float = 0.5  # how often a session checks that vhdl_ls is still running

    @classmethod
    def from_env(cls):
        d = cls()
        return cls(
            bridge=_env("P5B_BRIDGE", d.bridge),
            session_root=_env("P5B_SESSION_ROOT", d.session_root),
            max_sessions=_env("P5B_MAX_SESSIONS", d.max_sessions, int),
            max_files=_env("P5B_MAX_FILES", d.max_files, int),
            max_file_bytes=_env("P5B_MAX_FILE_BYTES", d.max_file_bytes, int),
            max_message_bytes=_env("P5B_MAX_MESSAGE_BYTES", d.max_message_bytes, int),
            idle_timeout_s=_env("P5B_IDLE_TIMEOUT_S", d.idle_timeout_s, float),
            request_timeout_s=_env("P5B_REQUEST_TIMEOUT_S", d.request_timeout_s, float),
            diagnostics_timeout_s=_env("P5B_DIAGNOSTICS_TIMEOUT_S", d.diagnostics_timeout_s, float),
            watchdog_interval_s=_env("P5B_WATCHDOG_INTERVAL_S", d.watchdog_interval_s, float),
        )

    @property
    def root_path(self):
        if self.session_root:
            return Path(self.session_root)
        return Path(tempfile.gettempdir()) / "p5b"

    @property
    def diagnostics_wait_s(self):
        # Kept below request_timeout_s so vhdl_ls's own timeout is reported first.
        return min(self.diagnostics_timeout_s, self.request_timeout_s * 0.8)
