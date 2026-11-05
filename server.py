"""Loopback-only dashboard. Credentials are loaded on the server."""

import asyncio
import os
import fcntl
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).with_name(".env"))
# Hold one process per checkout so budget reservations cannot race.
_runtime_lock = open(Path(__file__).with_name(".server.lock"), "a")
try:
    fcntl.flock(_runtime_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise RuntimeError("Another server uses this checkout. Stop it before starting another.")
# harness reads JEV_BUDGET_USD at import, so it must load after .env.
from harness import Harness, ROOT  # noqa: E402

h = Harness()


@asynccontextmanager
async def lifespan(app):
    yield
    h.shutdown()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def local_only(request: Request, call_next):
    if request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
        return Response("Local host required", status_code=403)
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return Response("Same local origin required", status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return Response("JSON required", status_code=415)
        if len(await request.body()) > 48000:
            return Response("Request too large", status_code=413)
    return await call_next(request)


@app.get("/")
def index():
    return HTMLResponse((ROOT / "index.html").read_text())


@app.get("/api/state")
def state():
    return h.view()


@app.get("/frame.jpg")
def frame():
    return Response(h.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/stream")
def stream(request: Request):
    async def frames():
        while not h.stop_event.is_set():
            if await request.is_disconnected():
                break
            if h.jpeg:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + h.jpeg + b"\r\n"
            await asyncio.sleep(0.06)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/api/{command}")
async def command(command: str, request: Request):
    if command not in (
        "start",
        "stop",
        "pause",
        "resume",
        "orders",
        "checkpoint",
        "load",
        "probe",
    ):
        raise HTTPException(404)
    try:
        args = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(args, dict):
        raise HTTPException(400, "Expected a JSON object")
    if command in ("start", "load", "probe"):
        provider = args.get("provider", h.config["provider_key"])
        if provider not in ("typesafe", "gateway"):
            raise HTTPException(400, "Unknown provider")
        env = "TYPESAFE_API_KEY" if provider == "typesafe" else "AI_GATEWAY_API_KEY"
        if not os.getenv(env):
            raise HTTPException(409, f"Add {env} to .env and restart the server")
        if command == "probe":
            args = {
                "provider": provider,
                "label": "Dashboard connection check",
                "state": {"signal": "green"},
                "questions": {
                    "action": {
                        "type": "choice",
                        "instructions": "Choose go when the signal is green.",
                        "criteria": {
                            "go": "The signal is green.",
                            "wait": "The signal is not green.",
                        },
                    }
                },
            }
    if command == "start":
        if (
            not isinstance(args.get("seconds", 90), (int, float))
            or not 5 <= args.get("seconds", 90) <= 600
        ):
            raise HTTPException(400, "Seconds must be between 5 and 600")
        if args.get("map", "MAP01") not in ("MAP01", "MAP02"):
            raise HTTPException(400, "Choose MAP01 or MAP02")
        if not isinstance(args.get("revision", ""), str):
            raise HTTPException(400, "Invalid revision")
    if command == "orders":
        if not isinstance(args.get("text"), str):
            raise HTTPException(400, "Order text is required")
    h.commands.put((command, args))
    return {"queued": command}
