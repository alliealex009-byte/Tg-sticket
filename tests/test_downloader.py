import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bot.services.downloader import DownloadError, Downloader
from conftest import run
from helpers import png_bytes

HITS = {"n": 0}
PNG = png_bytes((64, 64))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS["n"] += 1
        if self.path == "/img.png":
            return self._send(200, "image/png", PNG)
        if self.path == "/big":
            return self._send(200, "image/png", b"x" * 300_000)
        if self.path == "/text":
            return self._send(200, "text/html", b"<html></html>")
        if self.path == "/fake.png":
            return self._send(200, "image/png", b"definitely not a png")
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/img.png")
            self.end_headers()
            return
        if self.path == "/redir-private":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/img.png")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(2)
            return self._send(200, "image/png", PNG)
        self._send(404, "text/plain", b"nope")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def local(**kw):
    return Downloader(allow_private=True, allowed_ports=None, **kw)


def test_downloads_and_validates_image(server):
    data = run(local().fetch_image(server + "/img.png"))
    assert data == PNG


def test_follows_safe_redirect(server):
    assert run(local().fetch_image(server + "/redir")) == PNG


def test_ssrf_blocked_by_default(server):
    with pytest.raises(DownloadError, match="Blocked"):
        run(Downloader(allowed_ports=None).fetch_image(server + "/img.png"))


def test_redirect_to_private_ip_is_blocked(server):
    port = server.rsplit(":", 1)[1]
    dl = Downloader(allowed_ports=None, resolver=lambda host, p: ["127.0.0.1"])  # pretend host is public
    with pytest.raises(DownloadError, match="Blocked"):
        run(dl.fetch_image(f"http://public.example.test:{port}/redir-private"))


def test_rejects_wrong_type_size_and_fake_image(server):
    with pytest.raises(DownloadError, match="content type"):
        run(local().fetch_image(server + "/text"))
    with pytest.raises(DownloadError, match="large"):
        run(local(max_bytes=1000).fetch_image(server + "/big"))
    with pytest.raises(DownloadError):
        run(local().fetch_image(server + "/fake.png"))        # valid MIME but not decodable
    with pytest.raises(DownloadError, match="404"):
        run(local().fetch_image(server + "/missing"))


def test_timeout(server):
    with pytest.raises(DownloadError):
        run(local(timeout=0.5, total_timeout=1).fetch_image(server + "/slow"))


def test_cache_prevents_duplicate_downloads(server, tmp_path):
    HITS["n"] = 0
    dl = local(cache_dir=tmp_path / "c")
    run(dl.fetch_image(server + "/img.png"))
    run(dl.fetch_image(server + "/img.png"))
    assert HITS["n"] == 1


def test_concurrent_identical_requests_are_coalesced(server):
    HITS["n"] = 0
    dl = local()

    async def go():
        return await asyncio.gather(*[dl.fetch_image(server + "/img.png") for _ in range(6)])

    results = run(go())
    assert all(r == PNG for r in results) and HITS["n"] == 1


def test_purge_cache(tmp_path):
    dl = Downloader(cache_dir=tmp_path, cache_ttl=0)
    (tmp_path / "old.img").write_bytes(b"x")
    time.sleep(0.01)
    assert dl.purge_cache() == 1
