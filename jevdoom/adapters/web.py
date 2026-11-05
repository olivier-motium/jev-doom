"""Loopback-only web adapter with no engine startup on import."""

import asyncio
import io
import os
import math
from contextlib import asynccontextmanager
from pathlib import Path
from PIL import Image
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse

ROOT = Path(__file__).resolve().parents[2]


def encode_frame(frame):
    if frame is None:
        return b""
    if isinstance(frame, bytes):
        return frame
    buffer = io.BytesIO()
    Image.fromarray(frame).save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()


def create_app(h):
    @asynccontextmanager
    async def lifespan(app):
        yield
        h.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        peer = request.client.host if request.client else None
        if peer not in ("127.0.0.1", "::1") or request.url.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            return Response("Local host required", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return Response("Same local origin required", status_code=403)
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    declared = int(length)
                except ValueError:
                    return Response("Invalid Content-Length", status_code=400)
                if declared < 0:
                    return Response("Invalid Content-Length", status_code=400)
                if declared > 48000:
                    return Response("Request too large", status_code=413)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return Response("JSON required", status_code=415)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 48000:
                    return Response("Request too large", status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        return await call_next(request)

    @app.get("/")
    def index():
        return HTMLResponse((ROOT / "index.html").read_text())

    @app.get("/app.js")
    def javascript():
        return Response((ROOT / "app.js").read_text(), media_type="text/javascript")

    @app.get("/api/state")
    def state():
        return h.view()

    @app.get("/frame.jpg")
    def frame():
        image, tick = h.frame_snapshot()
        return Response(
            encode_frame(image),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Tick": str(tick)},
        )

    @app.get("/stream")
    def stream(request: Request):
        async def frames():
            while not h.stop_event.is_set():
                if await request.is_disconnected():
                    break
                image, tick = h.frame_snapshot()
                image = encode_frame(image)
                if image:
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nX-Tick: "
                        + str(tick).encode()
                        + b"\r\n\r\n"
                        + image
                        + b"\r\n"
                    )
                await asyncio.sleep(0.06)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.post("/api/{command}")
    async def command(command: str, request: Request):
        if h.config.get("controller") == "replay":
            raise HTTPException(409, "Commands are unavailable during replay")
        if command not in (
            "start",
            "stop",
            "pause",
            "resume",
            "orders",
            "probe",
        ):
            raise HTTPException(404)
        try:
            args = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        if not isinstance(args, dict):
            raise HTTPException(400, "Expected a JSON object")
        controller = args.get("controller", h.config.get("controller", "semantic_tactics"))
        if command == "start":
            if controller not in ("semantic_tactics", "local"):
                raise HTTPException(400, "Unknown controller")
            seconds = args.get("seconds", 90)
            if (
                type(seconds) not in (int, float)
                or not math.isfinite(seconds)
                or not 5 <= seconds <= 600
            ):
                raise HTTPException(400, "Seconds must be between 5 and 600")
            if args.get("map", "MAP01") not in ("MAP01", "MAP02"):
                raise HTTPException(400, "Choose MAP01 or MAP02")
            if not isinstance(args.get("revision", ""), str):
                raise HTTPException(400, "Invalid revision")
            if type(args.get("seed", 17)) is not int or not 0 <= args.get("seed", 17) <= 2**31 - 1:
                raise HTTPException(400, "Invalid seed")
            if type(args.get("skill", 3)) is not int or not 1 <= args.get("skill", 3) <= 5:
                raise HTTPException(400, "Invalid skill")
            if args.get("timing", "real_time") not in ("real_time", "pause_assisted"):
                raise HTTPException(400, "Invalid timing")
        if command in ("start", "probe"):
            provider = args.get("provider", h.config["provider_key"])
            if provider not in ("typesafe", "gateway"):
                raise HTTPException(400, "Unknown provider")
            if command == "probe" or controller != "local":
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
        if command == "orders":
            if not isinstance(args.get("text"), str):
                raise HTTPException(400, "Order text is required")
        h.commands.put((command, args))
        return {"queued": command}

    return app
