"""Threaded TCP content server for one Smart CDN backend.

Besides serving files over TCP, the server reports its health to the SDN
controller with small UDP heartbeats sent to the service VIP:

    HB <server-id> <active-transfers>     once per interval
    BYE <server-id>                       on clean shutdown

The edge switch has no rule for these datagrams, so they reach the controller
as Packet-In messages. The controller uses them to detect a crashed server
process and to prefer the backend with the fewest active transfers.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import socketserver
import struct
import threading
from pathlib import Path

BUFFER_SIZE = 64 * 1024
MAX_FILENAME_BYTES = 255
MAX_HEADER_BYTES = 4096
HEARTBEAT_ADDRESS = "10.0.0.100:5001"
HEARTBEAT_INTERVAL = 1.0


def validate_filename(filename: str) -> bool:
    return bool(
        filename
        and filename not in {".", ".."}
        and "/" not in filename
        and "\\" not in filename
        and "\x00" not in filename
        and "\n" not in filename
        and "\r" not in filename
        and len(filename.encode("utf-8")) <= MAX_FILENAME_BYTES
    )


def make_server(host: str, port: int, directory: str | os.PathLike[str], server_id: str):
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"content directory does not exist: {root}")

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(10.0)
            header = bytearray()
            try:
                while len(header) < MAX_HEADER_BYTES:
                    byte = self.request.recv(1)
                    if not byte:
                        return
                    header.extend(byte)
                    if byte == b"\n":
                        break
                if not header.endswith(b"\n"):
                    self.request.sendall(b"ERROR: filename too long\n")
                    return

                try:
                    filename = bytes(header[:-1]).decode("utf-8").rstrip("\r")
                except UnicodeDecodeError:
                    self.request.sendall(b"ERROR: filename is not valid UTF-8\n")
                    return
                if len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
                    self.request.sendall(b"ERROR: filename too long\n")
                    return
                if not validate_filename(filename):
                    self.request.sendall(b"ERROR: invalid filename\n")
                    return
                if filename == "__health__":
                    self.request.sendall(b"OK\n")
                    return

                candidate = (root / filename).resolve()
                if candidate.parent != root or not candidate.is_file():
                    self.request.sendall(b"ERROR: file not found\n")
                    return

                file_size = candidate.stat().st_size
                self.server.transfer_started()
                try:
                    self.request.sendall(b"OK\n" + struct.pack("!Q", file_size))
                    print(f"[{server_id}] {self.client_address}: sending {filename} ({file_size} bytes)", flush=True)
                    with candidate.open("rb") as source:
                        while True:
                            block = source.read(BUFFER_SIZE)
                            if not block:
                                break
                            self.request.sendall(block)
                finally:
                    self.server.transfer_finished()
                print(f"[{server_id}] completed {filename}", flush=True)
            except (OSError, socket.timeout) as exc:
                print(f"[{server_id}] {self.client_address}: {exc}", flush=True)

    class ThreadedServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
        block_on_close = False

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._active_lock = threading.Lock()
            self._active_transfers = 0

        def transfer_started(self):
            with self._active_lock:
                self._active_transfers += 1

        def transfer_finished(self):
            with self._active_lock:
                self._active_transfers -= 1

        def active_transfers(self) -> int:
            with self._active_lock:
                return self._active_transfers

    service = ThreadedServer((host, port), Handler)
    service.server_id = server_id
    service.content_root = root
    return service


def parse_address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"expected HOST:PORT, got {text!r}")
    return host, int(port)


class Heartbeat:
    """Background UDP reporter: liveness plus the current transfer count."""

    def __init__(self, service, address: tuple[str, int], interval: float = HEARTBEAT_INTERVAL):
        self.service = service
        self.address = address
        self.interval = interval
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)

    def _send(self, message: str) -> None:
        try:
            self._sock.sendto(message.encode("ascii"), self.address)
        except OSError:
            pass  # No route yet or controller away: the next beat tries again.

    def _run(self) -> None:
        while not self._stop.is_set():
            self._send(f"HB {self.service.server_id} {self.service.active_transfers()}")
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop reporting and tell the controller this backend is leaving."""
        self._stop.set()
        self._thread.join(timeout=2)
        self._send(f"BYE {self.service.server_id}")
        self._sock.close()


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--id", default="backend")
    parser.add_argument(
        "--heartbeat", default=HEARTBEAT_ADDRESS, metavar="HOST:PORT|off",
        help="where to send UDP health reports (default: %(default)s; 'off' disables)")
    parser.add_argument("--heartbeat-interval", type=float, default=HEARTBEAT_INTERVAL)
    args = parser.parse_args(argv)
    service = make_server(args.host, args.port, args.directory, args.id)
    print(f"[{args.id}] listening on {args.host}:{args.port}; content={service.content_root}", flush=True)
    heartbeat = None
    if args.heartbeat.lower() != "off":
        heartbeat = Heartbeat(service, parse_address(args.heartbeat), args.heartbeat_interval)
        heartbeat.start()
        print(f"[{args.id}] heartbeat to {args.heartbeat} every {args.heartbeat_interval}s", flush=True)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)  # plain `kill` = clean shutdown
    try:
        service.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print(f"[{args.id}] shutting down", flush=True)
    finally:
        if heartbeat:
            heartbeat.stop()
        service.shutdown()
        service.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
