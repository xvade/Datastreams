#!/usr/bin/env python3
"""Native messaging host that writes active Brave website sessions to CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


CSV_HEADER = ("domain", "started_at", "stopped_at", "duration_seconds")
MAX_MESSAGE_BYTES = 1024 * 1024
DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


def format_timestamp(epoch_seconds: float) -> str:
    """Format local ISO-8601 time with the machine's UTC offset."""

    return datetime.fromtimestamp(epoch_seconds).astimezone().isoformat(
        timespec="seconds"
    )


def read_exact(stream: BinaryIO, length: int) -> bytes | None:
    """Read one full native-messaging frame component, or return at EOF."""

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if remaining == length:
                return None
            raise EOFError("native message ended before its declared length")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> dict[str, object] | None:
    """Read a length-prefixed UTF-8 JSON message from Brave."""

    header = read_exact(stream, 4)
    if header is None:
        return None
    message_length = struct.unpack("=I", header)[0]
    if message_length > MAX_MESSAGE_BYTES:
        raise ValueError(f"native message exceeds {MAX_MESSAGE_BYTES} bytes")
    payload = read_exact(stream, message_length)
    if payload is None:
        raise EOFError("native message has no payload")
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("expected a JSON object from the extension")
    return message


def write_message(stream: BinaryIO, message: dict[str, object]) -> None:
    """Write one native-messaging response to Brave's stdout pipe."""

    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("=I", len(payload)))
    stream.write(payload)
    stream.flush()


def normalize_domain(value: object) -> str | None:
    """Accept a hostname only; reject paths and other unexpected data."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("domain must be a string or null")
    domain = value.strip().lower().rstrip(".")
    if not domain or len(domain) > 253:
        return None
    if ":" in domain:
        # URL.hostname returns bracket-free IPv6 text.
        try:
            import ipaddress

            ipaddress.IPv6Address(domain)
        except ValueError as exc:
            raise ValueError("invalid hostname") from exc
        return domain
    if all(DOMAIN_LABEL.fullmatch(label) for label in domain.split(".")):
        return domain
    raise ValueError("invalid hostname")


class WebsiteSessionWriter:
    """Keep one active domain and flush each completed interval to CSV."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._stream)
        if self._stream.tell() == 0:
            self._writer.writerow(CSV_HEADER)
            self._stream.flush()
        self._domain: str | None = None
        self._started_at: float | None = None

    def observe(self, domain: object, observed_at: object) -> None:
        """Close the previous site interval and begin the new one if needed."""

        normalized_domain = normalize_domain(domain)
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)):
            raise ValueError("observed_at must be a numeric Unix time")
        timestamp = float(observed_at)
        if not math.isfinite(timestamp):
            raise ValueError("observed_at must be finite")

        if normalized_domain == self._domain:
            return
        if self._domain is not None and self._started_at is not None:
            stopped_at = max(timestamp, self._started_at)
            self._writer.writerow(
                (
                    self._domain,
                    format_timestamp(self._started_at),
                    format_timestamp(stopped_at),
                    f"{stopped_at - self._started_at:.3f}",
                )
            )
            self._stream.flush()

        self._domain = normalized_domain
        self._started_at = timestamp if normalized_domain is not None else None

    def close(self) -> None:
        """Close any active session and the output file on host shutdown."""

        try:
            if self._domain is not None and self._started_at is not None:
                self.observe(None, time.time())
        finally:
            self._stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    # Native messaging reserves stdout for length-prefixed JSON. Keep any
    # diagnostics on stderr so the browser protocol cannot be corrupted.
    writer = WebsiteSessionWriter(args.output)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    try:
        write_message(stdout, {"type": "ready"})
        while True:
            message = read_message(stdin)
            if message is None:
                break
            if message.get("type") != "site":
                continue
            try:
                writer.observe(message.get("domain"), message.get("observed_at"))
            except (ValueError, OSError, OverflowError) as exc:
                print(f"Ignoring invalid Brave website update: {exc}", file=sys.stderr)
    finally:
        writer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
