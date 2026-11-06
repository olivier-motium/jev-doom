"""Check the web adapter at its ASGI boundary without starting an engine."""

import asyncio
import json
import queue
import subprocess
import sys
from types import SimpleNamespace

from jevdoom.adapters.web import create_app


def request(app, path, *, peer="127.0.0.1", host="127.0.0.1", origin=None, length=None, body=b"{}"):
    async def run():
        headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
        if origin:
            headers.append((b"origin", origin.encode()))
        if length is not None:
            headers.append((b"content-length", str(length).encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": (peer, 10000),
            "server": ("127.0.0.1", 80),
        }
        sent, received = [], 0

        async def receive():
            nonlocal received
            received += 1
            if received == 1:
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start"), received

    return asyncio.run(run())


def test_adapter_import_does_not_start_engine():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import jevdoom.adapters.web; assert 'vizdoom' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_local_boundary_and_request_limits():
    h = SimpleNamespace(config={"provider_key": "gateway"}, commands=queue.Queue())
    app = create_app(h)
    assert request(app, "/api/load")[0] == 404
    assert request(app, "/api/start", peer="10.0.0.2")[0] == 403
    assert request(app, "/api/start", host="evil.com")[0] == 403
    assert request(app, "/api/start", origin="http://evil.com")[0] == 403
    status, reads = request(app, "/api/start", length=48001)
    assert (status, reads) == (413, 0)
    bad = json.dumps({"controller": "local", "seed": "bad"}).encode()
    assert request(app, "/api/start", body=bad)[0] == 400
    good = json.dumps({"controller": "local", "map": "MAP01"}).encode()
    assert request(app, "/api/start", body=good)[0] == 200
    assert h.commands.get()[0] == "start"


def test_overlong_order_is_rejected_without_queueing(caplog):
    h = SimpleNamespace(config={"provider_key": "gateway"}, commands=queue.Queue())
    app = create_app(h)
    too_long = json.dumps({"text": "x" * 1501}).encode()
    assert request(app, "/api/orders", body=too_long)[0] == 400
    assert "length 1501 exceeds limit 1500" in caplog.text
    assert h.commands.empty()
    allowed = json.dumps({"text": "x" * 1500}).encode()
    assert request(app, "/api/orders", body=allowed)[0] == 200
    assert h.commands.get()[1]["text"] == "x" * 1500
