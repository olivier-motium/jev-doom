"""Local dashboard entry point."""

import argparse
import fcntl
import os
from pathlib import Path
import threading

import uvicorn
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Run the local Doom dashboard")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--replay", metavar="RUN_DIR", help="replay a recorded run without a model")
    group.add_argument("--demo", action="store_true", help="record or replay the local demo")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    lock = (ROOT / ".server.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError(
            "Another server uses this checkout. Stop it before starting another."
        ) from exc
    from jevdoom.adapters.web import create_app

    if args.demo:
        pointer = ROOT / "runs" / "demo-path.txt"
        run_dir = Path(pointer.read_text().strip()) if pointer.exists() else None
        if not run_dir or not (run_dir / "episode.lmp").is_file():
            from jevdoom.run import Run

            print("Recording a local demo (up to 60 game seconds)...", flush=True)
            run = Run()
            run.commands.put(
                (
                    "start",
                    {
                        "map": "MAP01",
                        "seed": 17,
                        "skill": 3,
                        "seconds": 60,
                        "controller": "local",
                        "revision": "demo",
                    },
                )
            )
            while (
                not run.result.get("reason")
                and not run.stop_event.is_set()
                and run.status != "engine_error"
            ):
                threading.Event().wait(0.25)
            run_dir = getattr(run, "run_dir", None)
            run.shutdown()
            if not run_dir or not (run_dir / "episode.lmp").is_file():
                raise RuntimeError(f"Demo recording failed: {run.error or 'no recording produced'}")
            pointer.write_text(str(run_dir))
        args.replay = str(run_dir)

    if args.replay:
        from jevdoom.adapters.replay import ReplaySession

        session = ReplaySession(args.replay)
    else:
        from jevdoom.run import Run

        session = Run()

    uvicorn.run(
        create_app(session),
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8767")),
        log_level="warning",
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
