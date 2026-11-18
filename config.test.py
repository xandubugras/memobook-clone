"""Error handling for a malformed .state/azure.json. Self-contained.

Copies the server into a temporary directory, starts it with a broken
Azure config (which makes server.py serve without a local model), and
checks that requests fail with a clear 503 instead of a dropped
connection or a raw JSON parser message. The config is read on every
request, so the test can also repair the file and watch the same
server recover.
"""
import http.client
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VALID = json.dumps({
    "chat_endpoint": "https://example-resource.openai.azure.com",
    "chat_model": "test-chat",
    "voice_endpoint": "https://example-resource.openai.azure.com",
    "voice_model": "test-voice",
})


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class MalformedConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.host = f"127.0.0.1:{cls.port}"
        cls.tmp = Path(tempfile.mkdtemp(prefix="memo-config-"))
        for name in ("server.py", "cloud.py"):
            shutil.copy(ROOT / name, cls.tmp / name)
        shutil.copytree(ROOT / "public", cls.tmp / "public")
        cls.config = cls.tmp / ".state" / "azure.json"
        cls.config.parent.mkdir()
        cls.config.write_text("{invalid json")
        import os

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
                status, _ = cls.request("GET", "/app")
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
    def request(cls, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=10)
        headers = {"Host": cls.host}
        if body is not None:
            headers["Origin"] = f"http://{cls.host}"
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, raw

    def assert_config_error(self, status, raw):
        self.assertEqual(status, 503)
        error = json.loads(raw)["error"]
        self.assertIn("azure.json", error)
        self.assertNotIn("Expecting property name", error)

    def test_provider_returns_503_not_a_dropped_connection(self):
        self.assert_config_error(*self.request("GET", "/api/provider"))

    def test_tutor_returns_503_with_the_config_error(self):
        self.assert_config_error(
            *self.request("POST", "/api/chat", {"turns": ["hi"]})
        )

    def test_voice_returns_503_with_the_config_error(self):
        self.assert_config_error(
            *self.request("POST", "/api/voice", {"line": "hello"})
        )

    def test_static_studio_still_serves(self):
        status, _ = self.request("GET", "/app")
        self.assertEqual(status, 200)

    def test_non_object_config_is_also_invalid(self):
        self.config.write_text("[1, 2, 3]")
        self.assert_config_error(*self.request("GET", "/api/provider"))

    def test_fixing_the_file_recovers_without_a_restart(self):
        self.config.write_text(VALID)
        status, raw = self.request("GET", "/api/provider")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["chat_model"], "test-chat")


if __name__ == "__main__":
    unittest.main()
