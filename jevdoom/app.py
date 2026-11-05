"""Local dashboard entry point."""

import fcntl
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent


def main():
    load_dotenv(ROOT / ".env")
    lock = (ROOT / ".server.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError(
            "Another server uses this checkout. Stop it before starting another."
        ) from exc
    from jevdoom.run import Run
    from jevdoom.adapters.web import create_app

    uvicorn.run(
        create_app(Run()),
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8767")),
        log_level="warning",
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
