# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import threading
from pathlib import Path

from flask import Flask

from config import MINIAPP_HOST, MINIAPP_PORT, MINIAPP_URL
from miniapp_api import register_shuibei_miniapp


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "miniapp_dist"


def create_web_app() -> Flask:
    app = Flask("shuibei_open_source")
    app.config.update(
        DEBUG=False,
        TESTING=False,
        PROPAGATE_EXCEPTIONS=False,
    )
    register_shuibei_miniapp(app, static_dir=str(STATIC_DIR))
    return app


def run_web() -> None:
    from werkzeug.serving import run_simple

    app = create_web_app()
    host = str(MINIAPP_HOST or "127.0.0.1")
    port = int(MINIAPP_PORT or 8080)
    print(
        f"[ShuiBei] MiniApp HTTP server listening on http://{host}:{port}/ShuiBei/app",
        flush=True,
    )
    if not str(MINIAPP_URL or "").startswith("https://"):
        print(
            "[ShuiBei] SHUIBEI_MINIAPP_URL is not configured with HTTPS; "
            "the Telegram MiniApp button will stay hidden.",
            flush=True,
        )
    run_simple(
        host,
        port,
        app,
        use_reloader=False,
        use_debugger=False,
        threaded=True,
    )


def run_bot() -> None:
    from start import main as bot_main

    bot_main()


def run_all() -> None:
    web_thread = threading.Thread(
        target=run_web,
        daemon=True,
        name="shuibei-miniapp-web",
    )
    web_thread.start()
    run_bot()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m audit_source",
        description="Run the ShuiBei source-available build.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("bot", "web", "all"),
        default="bot",
        help="bot: Telegram bot only; web: MiniApp HTTP only; all: both",
    )
    args = parser.parse_args(argv)

    if args.mode == "web":
        run_web()
    elif args.mode == "all":
        run_all()
    else:
        run_bot()


if __name__ == "__main__":
    main()
