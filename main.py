"""Entry point: load env, wire settings + app, handle shutdown + health port."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
from pathlib import Path

# curl_cffi async I/O needs a selector loop on Windows (ProactorLoop lacks
# add_reader and crashes). Ported from the original Myntra-main bot.
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from myntra_scraper.app import ScraperApp
from myntra_scraper.config import Settings
from myntra_scraper.logging_setup import setup_logging


def load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def start_health_server(port: int) -> None:
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - myntra-scraper alive")

        def log_message(self, *args: object) -> None:
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True, name="health").start()
        print("Health server on PORT=%d" % port, flush=True)
    except Exception as exc:
        print("Health server failed: %s" % exc, flush=True)


async def amain() -> None:
    load_dotenv()
    bootstrap = Settings(
        bot_tokens=["x:y"], control_bot_token="x:y", admin_chat_id=1, channel_all=1,
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"))
    logger = setup_logging("myntra_scraper", bootstrap.log_file, bootstrap.log_level)
    try:
        settings = Settings.load()
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    logger = setup_logging("myntra_scraper", settings.log_file, settings.log_level)
    start_health_server(settings.port)
    logger.info("myntra-scraper v2 starting | %d bots | admin=%d",
                len(settings.bot_tokens), settings.admin_chat_id)
    app = ScraperApp(settings)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(app.run_forever())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:
            pass
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await app.shutdown()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("Bye!")


if __name__ == "__main__":
    main()
