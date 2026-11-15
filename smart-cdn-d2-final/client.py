"""TCP client for the Smart CDN VIP file-transfer protocol."""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path

BUFFER_SIZE = 64 * 1024
MAX_FILENAME_BYTES = 255
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024


class TransferError(Exception):
    """A connection, protocol, or file-transfer error."""


class ServerRejected(TransferError):
    """The server answered with an error; retrying will not help."""


def validate_filename(filename: str) -> bytes:
    if not filename or filename in {".", ".."}:
        raise ValueError("filename must not be empty")
    if "/" in filename or "\\" in filename or "\x00" in filename:
        raise ValueError("enter a filename only, without a path")
    encoded = filename.encode("utf-8")
    if len(encoded) > MAX_FILENAME_BYTES:
        raise ValueError(f"filename must be {MAX_FILENAME_BYTES} bytes or fewer")
    if b"\n" in encoded or b"\r" in encoded:
        raise ValueError("filename contains a line break")
    return encoded


def _receive_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(min(BUFFER_SIZE, size - len(chunks)))
        if not part:
            raise TransferError("connection ended before the full file arrived")
        chunks.extend(part)
    return bytes(chunks)


def download_file(
    filename: str,
    host: str,
    port: int,
    download_dir: str | os.PathLike[str] = "downloads",
    timeout: float = 15.0,
) -> Path:
    encoded_name = validate_filename(filename)
    output_dir = Path(download_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename
    temporary_path: Path | None = None

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(encoded_name + b"\n")

            status = bytearray()
            while not status.endswith(b"\n"):
                if len(status) >= 256:
                    raise TransferError("server response header is too long")
                byte = sock.recv(1)
                if not byte:
                    raise TransferError("server disconnected before sending a status")
                status.extend(byte)

            if bytes(status) != b"OK\n":
                message = bytes(status).decode("utf-8", errors="replace").strip()
                raise ServerRejected(message or "server rejected the request")

            file_size = struct.unpack("!Q", _receive_exact(sock, 8))[0]
            if file_size > MAX_FILE_SIZE:
                raise TransferError(f"server declared an unsafe file size: {file_size}")

            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{filename}.",
                suffix=".part",
                dir=output_dir,
                delete=False,
            ) as output:
                temporary_path = Path(output.name)
                remaining = file_size
                while remaining:
                    block = sock.recv(min(BUFFER_SIZE, remaining))
                    if not block:
                        raise TransferError(
                            f"incomplete transfer: expected {file_size} bytes, "
                            f"received {file_size - remaining}"
                        )
                    output.write(block)
                    remaining -= len(block)

        os.replace(temporary_path, destination)
        temporary_path = None
        return destination
    except (OSError, struct.error) as exc:
        raise TransferError(str(exc)) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def download_with_retries(
    filename: str,
    host: str,
    port: int,
    download_dir: str | os.PathLike[str] = "downloads",
    timeout: float = 15.0,
    retries: int = 2,
    retry_delay: float = 1.0,
) -> tuple[Path, int]:
    """Download a file, reconnecting after a failed attempt.

    A TCP session cannot move to another backend, so after a failure the client
    opens a new connection to the VIP. The controller treats it as a new flow
    and binds it to a backend and path that are alive now. Returns the saved
    path and the number of attempts used.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return download_file(filename, host, port, download_dir, timeout), attempt
        except ServerRejected:
            raise
        except TransferError as exc:
            if attempt > retries:
                raise TransferError(f"{exc} (after {attempt} attempts)") from exc
            print(f"Attempt {attempt} failed: {exc}; reconnecting", file=sys.stderr)
            time.sleep(retry_delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filename", nargs="?", help="file to request")
    parser.add_argument("--host", default=os.environ.get("CDN_HOST", "10.0.0.100"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CDN_PORT", "5000")))
    parser.add_argument("--download-dir", default="downloads")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2,
                        help="reconnect attempts after a failed transfer (default: %(default)s)")
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--json", action="store_true",
                        help="also print one JSON result line (used by the experiment runner)")
    args = parser.parse_args(argv)
    filename = args.filename or input("Enter filename to request: ").strip()
    started = time.monotonic()
    attempts = 0
    try:
        path, attempts = download_with_retries(
            filename, args.host, args.port, args.download_dir,
            args.timeout, args.retries, args.retry_delay)
    except (TransferError, ValueError) as exc:
        print(f"Download failed: {exc}", file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "file": filename, "error": str(exc),
                              "elapsed_s": round(time.monotonic() - started, 3)}))
        return 1
    size = path.stat().st_size
    elapsed = max(time.monotonic() - started, 1e-9)
    throughput_mbps = size * 8 / elapsed / 1_000_000
    if attempts > 1:
        print(f"Recovered after {attempts} attempts")
    if args.json:
        print(json.dumps({"ok": True, "file": filename, "bytes": size,
                          "elapsed_s": round(elapsed, 3),
                          "throughput_mbps": round(throughput_mbps, 3),
                          "attempts": attempts}))
    print(f"Downloaded {path.name} ({size} bytes) to {path}")
    print(f"Elapsed: {elapsed:.3f} s; throughput: {throughput_mbps:.3f} Mbit/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
