import hashlib
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client import ServerRejected, TransferError, download_file, download_with_retries
from content_server import Heartbeat, make_server


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="smart-cdn-tests-")
        cls.root = Path(cls.temp.name)
        cls.content = cls.root / "content"
        cls.content.mkdir()
        (cls.content / "sample.txt").write_bytes(b"replicated sample\n")
        (cls.content / "empty.bin").write_bytes(b"")
        cls.large_bytes = bytes(range(256)) * 20_480
        (cls.content / "large.bin").write_bytes(cls.large_bytes)
        cls.server = make_server("127.0.0.1", 0, cls.content, "test")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def setUp(self):
        self.output = self.root / ("out-" + self.id().split(".")[-1])
        self.output.mkdir(exist_ok=True)

    def test_small_file(self):
        path = download_file("sample.txt", "127.0.0.1", self.port, self.output)
        self.assertEqual(path.read_bytes(), b"replicated sample\n")

    def test_client_command_line(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "client.py"),
                "sample.txt",
                "--host", "127.0.0.1",
                "--port", str(self.port),
                "--download-dir", str(self.output),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("throughput:", result.stdout)
        self.assertEqual((self.output / "sample.txt").read_bytes(), b"replicated sample\n")

    def test_server_command_line_help(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "content_server.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--directory", result.stdout)

    def test_empty_file(self):
        path = download_file("empty.bin", "127.0.0.1", self.port, self.output)
        self.assertEqual(path.read_bytes(), b"")

    def test_large_file_hash(self):
        path = download_file("large.bin", "127.0.0.1", self.port, self.output)
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).digest(),
            hashlib.sha256(self.large_bytes).digest(),
        )

    def test_missing_file_reports_error(self):
        with self.assertRaisesRegex(TransferError, "file not found"):
            download_file("missing.txt", "127.0.0.1", self.port, self.output)

    def test_path_traversal_is_rejected_before_connecting(self):
        with self.assertRaises(ValueError):
            download_file("../sample.txt", "127.0.0.1", self.port, self.output)
        with self.assertRaises(ValueError):
            download_file(r"..\sample.txt", "127.0.0.1", self.port, self.output)

    def test_server_rejects_invalid_and_overlong_names(self):
        for request, expected in (
            (b"../sample.txt\n", b"ERROR: invalid filename\n"),
            (b"x" * 256 + b"\n", b"ERROR: filename too long\n"),
        ):
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
                sock.sendall(request)
                response = bytearray()
                while not response.endswith(b"\n"):
                    part = sock.recv(1)
                    self.assertTrue(part)
                    response.extend(part)
                self.assertEqual(bytes(response), expected)

    def test_six_concurrent_clients(self):
        def one_client(index):
            directory = self.root / f"parallel-{index}"
            result = download_file("sample.txt", "127.0.0.1", self.port, directory)
            return result.read_bytes()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(one_client, range(6)))
        self.assertEqual(results, [b"replicated sample\n"] * 6)

    def test_interrupted_transfer_removes_temporary_file(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def cut_connection():
            connection, _ = listener.accept()
            with connection:
                request = bytearray()
                while not request.endswith(b"\n"):
                    chunk = connection.recv(1)
                    if not chunk:
                        return
                    request.extend(chunk)
                connection.sendall(b"OK\n" + struct.pack("!Q", 100) + b"short-data")
            listener.close()

        worker = threading.Thread(target=cut_connection, daemon=True)
        worker.start()
        with self.assertRaisesRegex(TransferError, "incomplete transfer"):
            download_file("partial.bin", "127.0.0.1", port, self.output)
        worker.join(timeout=2)
        self.assertFalse((self.output / "partial.bin").exists())
        self.assertEqual(list(self.output.glob("*.part")), [])

    def test_health_probe(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            sock.sendall(b"__health__\n")
            self.assertEqual(sock.recv(3), b"OK\n")

    def test_retry_reconnects_after_a_cut_transfer(self):
        """First connection dies mid-file (backend crash); the retry succeeds."""
        payload = b"x" * 1000
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        port = listener.getsockname()[1]

        def serve_twice():
            for body in (payload[:10], payload):
                connection, _ = listener.accept()
                with connection:
                    while not connection.recv(1) == b"\n":
                        pass
                    connection.sendall(b"OK\n" + struct.pack("!Q", len(payload)) + body)
            listener.close()

        worker = threading.Thread(target=serve_twice, daemon=True)
        worker.start()
        path, attempts = download_with_retries(
            "retry.bin", "127.0.0.1", port, self.output, retries=2, retry_delay=0.05)
        worker.join(timeout=2)
        self.assertEqual(attempts, 2)
        self.assertEqual(path.read_bytes(), payload)

    def test_retry_gives_up_and_does_not_retry_server_errors(self):
        with self.assertRaises(ServerRejected):
            download_with_retries("missing.txt", "127.0.0.1", self.port, self.output,
                                  retries=3, retry_delay=0.05)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        with self.assertRaisesRegex(TransferError, "after 2 attempts"):
            download_with_retries("sample.txt", "127.0.0.1", port, self.output,
                                  timeout=1, retries=1, retry_delay=0.05)

    def test_heartbeat_reports_liveness_load_and_shutdown(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        heartbeat = Heartbeat(self.server, receiver.getsockname(), interval=0.05)
        heartbeat.start()
        try:
            self.assertEqual(receiver.recv(64), b"HB test 0")
            self.server.transfer_started()
            deadline = time.monotonic() + 2
            while receiver.recv(64) != b"HB test 1":
                self.assertLess(time.monotonic(), deadline)
            self.server.transfer_finished()
        finally:
            heartbeat.stop()
        last = b""
        try:
            while True:
                last = receiver.recv(64)
        except socket.timeout:
            pass
        receiver.close()
        self.assertEqual(last, b"BYE test")
        self.assertEqual(self.server.active_transfers(), 0)


if __name__ == "__main__":
    unittest.main()
