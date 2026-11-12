"""Starts the Gateway with uvicorn (a real server on a free port) and runs the
cases from tools/check_gateway.py against it twice, over a real WebSocket.

Uses the real vhdl_ls if it is installed, otherwise the fake bridge.
P5A and Project 6 are not used (FR-P5B-06, independent verification).
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import ROOT, VHDL_LS

sys.path.insert(0, str(ROOT / "tools"))
import check_gateway  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = free_port()
    root = tmp_path_factory.mktemp("p5b-live")
    env = {**os.environ, "P5B_SESSION_ROOT": str(root),
           "P5B_BRIDGE": "real" if VHDL_LS else "fake"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gateway.main:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/language-service/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the Gateway did not start")
        yield f"ws://127.0.0.1:{port}/ws/language-service", root
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def test_every_case_passes_twice_with_identical_replies(server):
    url, root = server
    first = check_gateway.run_once(url)
    second = check_gateway.run_once(url)
    for name, problems, _ in first + second:
        assert problems == [], f"{name}: {problems}"
    assert [r for _, _, r in first] == [r for _, _, r in second]


def test_no_session_folders_are_left_behind(server):
    url, root = server
    check_gateway.run_once(url)
    sessions = root / "sessions"
    for _ in range(50):
        if not sessions.exists() or not any(sessions.iterdir()):
            break
        time.sleep(0.1)
    assert not sessions.exists() or list(sessions.iterdir()) == []


def test_health(server):
    url, _ = server
    port = url.split(":")[2].split("/")[0]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/language-service/health") as r:
        assert r.status == 200
