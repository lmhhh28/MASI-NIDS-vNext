#!/usr/bin/env python3
"""Generate PgBouncer SCRAM auth_file entries from mode-0600 secret files."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
from pathlib import Path


def read_secret(path: Path) -> bytes:
    stat = path.lstat()
    if not path.is_file() or path.is_symlink() or stat.st_mode & 0o077:
        raise ValueError(f"unsafe secret file: {path}")
    data = path.read_bytes()
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data or b"\n" in data or len(data) > 1024:
        raise ValueError(f"invalid secret value: {path}")
    return data


def verifier(password: bytes) -> str:
    iterations = 4096
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return "SCRAM-SHA-256${}:{}${}:{}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(stored_key).decode("ascii"),
        base64.b64encode(server_key).decode("ascii"),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--verifier-directory", required=True, type=Path)
    args = parser.parse_args()
    users = {
        "masi_app_login": ("app-password", "app-scram"),
        "masi_migration_login": ("migration-password", "migration-scram"),
        "masi_monitoring_login": ("monitoring-password", "monitoring-scram"),
    }
    generated = {
        user: verifier(read_secret(args.secret_directory / password_file))
        for user, (password_file, _) in users.items()
    }
    lines = [f'"{user}" "{generated[user]}"' for user in users]
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("refusing to overwrite PgBouncer auth file")
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    for user, (_, verifier_file) in users.items():
        path = args.verifier_directory / verifier_file
        if path.exists() or path.is_symlink():
            raise ValueError(f"refusing to overwrite verifier file: {path}")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(generated[user] + "\n")


if __name__ == "__main__":
    main()
