#!/usr/bin/env python3
"""Register the Brave native-messaging host for the bundled extension ID."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path


HOST_NAME = "com.apm.website_tracker"
NATIVE_HOST_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    # Brave redirects Chromium's per-user native-host lookup to this location
    # for compatibility with native apps that register Chrome hosts.
    / "Google"
    / "Chrome"
    / "NativeMessagingHosts"
)


def extension_id_from_manifest(manifest_path: Path) -> str:
    """Compute Chromium's stable extension ID from manifest.json's public key."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    public_key = manifest.get("key")
    if not isinstance(public_key, str):
        raise ValueError("extension manifest must contain a base64 public key")
    digest = hashlib.sha256(base64.b64decode(public_key)).digest()[:16]
    return "".join(chr(ord("a") + nibble) for byte in digest for nibble in (byte >> 4, byte & 0x0F))


def install(output_path: Path) -> Path:
    """Write Brave's per-user native-messaging manifest atomically."""

    if sys.platform != "darwin":
        raise RuntimeError("this installer currently supports macOS Brave only")

    extension_dir = Path(__file__).resolve().parent
    extension_id = extension_id_from_manifest(extension_dir / "manifest.json")
    output_path = output_path.expanduser().resolve()
    host_script = extension_dir / "native_host.py"
    launcher_path = NATIVE_HOST_DIR / "apm_website_tracker_host"
    host_manifest = {
        "name": HOST_NAME,
        "description": "Write Brave active website sessions to the APM CSV.",
        "path": str(launcher_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }

    NATIVE_HOST_DIR.mkdir(parents=True, exist_ok=True)
    # Native-host manifests do not support command arguments. Generate a
    # tiny executable launcher so users can still choose a CSV destination.
    launcher = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(host_script))} "
        f"--output {shlex.quote(str(output_path))}\n"
    )
    launcher_path.write_text(launcher, encoding="utf-8")
    launcher_path.chmod(0o700)

    destination = NATIVE_HOST_DIR / f"{HOST_NAME}.json"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=NATIVE_HOST_DIR,
        prefix=f".{HOST_NAME}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temp_path = Path(temporary.name)
        json.dump(host_manifest, temporary, indent=2)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
    os.replace(temp_path, destination)
    return destination


def main() -> int:
    default_output = Path(__file__).resolve().parent.parent / "website_focus.csv"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default_output)
    args = parser.parse_args()

    manifest_path = install(args.output)
    extension_id = extension_id_from_manifest(Path(__file__).with_name("manifest.json"))
    print(f"Installed Brave native host: {manifest_path}")
    print(f"Load the unpacked extension from: {Path(__file__).resolve().parent}")
    print(f"Extension ID: {extension_id}")
    print(f"Website sessions will be written to: {args.output.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
