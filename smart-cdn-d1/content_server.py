"""Threaded TCP content server for one Smart CDN backend."""

from __future__ import annotations

import argparse
import os
import socket
import socketserver
import struct
from pathlib import Path

BUFFER_SIZE = 64 * 1024
MAX_FILENAME_BYTES = 255
MAX_HEADER_BYTES = 4096


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
                self.request.sendall(b"OK\n" + struct.pack("!Q", file_size))
                print(f"[{server_id}] {self.client_address}: sending {filename} ({file_size} bytes)", flush=True)
                with candidate.open("rb") as source:
                    while True:
                        block = source.read(BUFFER_SIZE)
                        if not block:
                            break
                        self.request.sendall(block)
                print(f"[{server_id}] completed {filename}", flush=True)
            except (OSError, socket.timeout) as exc:
                print(f"[{server_id}] {self.client_address}: {exc}", flush=True)

    class ThreadedServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
        block_on_close = False

    service = ThreadedServer((host, port), Handler)
    service.server_id = server_id
    service.content_root = root
    return service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--id", default="backend")
    args = parser.parse_args(argv)
    service = make_server(args.host, args.port, args.directory, args.id)
    print(f"[{args.id}] listening on {args.host}:{args.port}; content={service.content_root}", flush=True)
    try:
        service.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print(f"[{args.id}] shutting down", flush=True)
    finally:
        service.shutdown()
        service.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
