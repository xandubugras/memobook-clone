"""Route checks for the local server. Self-contained: no models, no browser.

Copies the server into a temporary directory with a placeholder Azure
config (which makes server.py serve without starting a local model),
starts it on a free port, and exercises the HTTP routes directly.
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOOK_ID = "123e4567-e89b-12d3-a456-426614174000"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServerRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.host = f"127.0.0.1:{cls.port}"
        cls.tmp = Path(tempfile.mkdtemp(prefix="memo-server-"))
        for name in ("server.py", "cloud.py"):
            shutil.copy(ROOT / name, cls.tmp / name)
        shutil.copytree(ROOT / "public", cls.tmp / "public")
        state = cls.tmp / ".state"
        state.mkdir()
        (state / "azure.json").write_text(json.dumps({
            "chat_endpoint": "https://example-resource.openai.azure.com",
            "chat_model": "test-chat",
            "voice_endpoint": "https://example-resource.openai.azure.com",
            "voice_model": "test-voice",
        }))
        cls.server = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=cls.tmp,
            env={**os.environ, "MEMOBOOK_PORT": str(cls.port)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError("Server exited early:\n" + cls.server.stdout.read())
            try:
                status, _, _ = cls.request("GET", "/api/provider")
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("Server did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def request(cls, method, path, body=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        headers = {"Host": host or cls.host}
        if body is not None:
            headers["Origin"] = f"http://{cls.host}"
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), raw

    def test_plain_routes(self):
        for path in ("/", "/app", "/app.js", f"/app/books/{BOOK_ID}"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 200, path)

    def test_query_string_does_not_change_the_route(self):
        for plain, with_query in (
            ("/app", "/app?ref=share"),
            ("/app.js", "/app.js?v=abc123"),
            (f"/app/books/{BOOK_ID}", f"/app/books/{BOOK_ID}?utm_source=share"),
        ):
            plain_status, _, plain_body = self.request("GET", plain)
            query_status, _, query_body = self.request("GET", with_query)
            self.assertEqual(query_status, 200, with_query)
            self.assertEqual(query_status, plain_status, with_query)
            self.assertEqual(query_body, plain_body, with_query)

    def test_provider_with_query_string(self):
        status, _, raw = self.request("GET", "/api/provider?source=check")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["chat_model"], "test-chat")

    def test_api_post_with_query_string_reaches_validation(self):
        status, _, raw = self.request("POST", "/api/chat?x=1", {"turns": []})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"], "Invalid tutor conversation.")

    def test_unknown_routes_still_404(self):
        for path in ("/nope", "/nope?x=1"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)
        status, _, _ = self.request("POST", "/api/nope?x=1", {})
        self.assertEqual(status, 404)

    def test_wrong_host_still_rejected(self):
        status, _, _ = self.request("GET", "/app?x=1", host="example.com")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
