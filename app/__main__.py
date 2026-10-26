"""Entry point: `python -m app` starts the local server and opens the app window."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import APP_ID, __version__
from .config import DATA_DIR

DEFAULT_PORT = 8765

BROWSERS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


def _is_ours(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/health", timeout=0.7) as r:
            return json.load(r).get("app") == APP_ID
    except (OSError, ValueError):
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def open_window(url: str, mode: str) -> None:
    """Open the UI as a standalone app window (Edge/Chrome --app), else in the default browser."""
    if mode == "none":
        return
    if mode == "app":
        for candidate in BROWSERS:
            exe = Path(os.path.expandvars(candidate))
            if exe.exists():
                # own profile without sync/extensions: the browser would otherwise sign in and pull the
                # user's extensions into the app window (e.g. grammar checkers reading the typed text)
                subprocess.Popen([
                    str(exe), f"--app={url}", f"--user-data-dir={DATA_DIR / 'window'}",
                    "--window-size=1000,860", "--no-first-run", "--no-default-browser-check",
                    "--disable-extensions", "--disable-sync",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
    webbrowser.open(url)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):   # never crash on a console that can't print Cyrillic
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="souffleur")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--window", choices=["app", "browser", "none"], default="app",
                        help="app: standalone window (default), browser: a browser tab, none: don't open")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    if _is_ours(url):
        print("Souffleur уже запущен — открываю окно.")
        open_window(url, args.window)
        return
    port = args.port if _port_free(args.port) else _free_port()
    url = f"http://127.0.0.1:{port}/"

    import uvicorn

    from .server import app, controller

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    controller.request_exit = lambda: setattr(server, "should_exit", True)

    def open_when_ready() -> None:
        while not server.started:
            if server.should_exit:
                return
            time.sleep(0.05)
        open_window(url, args.window)

    threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"Souffleur {__version__} — {url}")
    print("Закройте окно приложения или нажмите Ctrl+C, чтобы выйти.")
    server.run()


if __name__ == "__main__":
    main()
