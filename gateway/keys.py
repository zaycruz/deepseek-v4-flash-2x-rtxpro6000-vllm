#!/usr/bin/env python3
"""Team API-key registry and administration for the DS4 gateway."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "member name must be 1-64 characters using letters, numbers, dot, dash, or underscore"
        )
    return name


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _read_registry(path: Path, *, allow_missing: bool = False) -> dict[str, object]:
    if not path.exists():
        if allow_missing:
            return {"version": 1, "keys": []}
        raise ValueError(f"API key registry does not exist: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(f"API key registry must not be group/world accessible: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("keys"), list):
        raise ValueError("unsupported API key registry format")
    for entry in payload["keys"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid API key registry entry")
        if not all(
            isinstance(entry.get(field), str)
            for field in ("id", "name", "sha256", "created_at")
        ):
            raise ValueError("invalid API key registry entry")
        _validate_name(entry["name"])
        if len(entry["sha256"]) != 64:
            raise ValueError("invalid API key digest")
    return payload


def _write_registry(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".keys-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def _locked_registry(path: Path) -> Iterator[dict[str, object]]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        payload = _read_registry(path, allow_missing=True)
        yield payload
        _write_registry(path, payload)


class KeyRegistry:
    def __init__(self, path: Path):
        self.path = path

    def authenticate(self, authorization: str) -> str | None:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            return None
        supplied_digest = _digest(authorization[len(prefix) :])
        matched_name: str | None = None
        for entry in _read_registry(self.path)["keys"]:
            if hmac.compare_digest(supplied_digest, entry["sha256"]):
                matched_name = entry["name"]
        return matched_name


def add_key(path: Path, name: str) -> str:
    name = _validate_name(name)
    key_id = secrets.token_hex(6)
    key = f"ds4_sk_{key_id}_{secrets.token_urlsafe(32)}"
    _add_entry(path, name, key_id, key)
    return key


def import_key(path: Path, name: str, key: str) -> None:
    name = _validate_name(name)
    key = key.strip()
    if len(key) < 16:
        raise ValueError("API key must be at least 16 characters")
    _add_entry(path, name, f"imported-{secrets.token_hex(4)}", key)


def _add_entry(path: Path, name: str, key_id: str, key: str) -> None:
    with _locked_registry(path) as payload:
        keys = payload["keys"]
        if any(entry["name"] == name for entry in keys):
            raise ValueError(f"member already exists: {name}")
        keys.append(
            {
                "id": key_id,
                "name": name,
                "sha256": _digest(key),
                "created_at": datetime.now(UTC).isoformat(),
            }
        )


def list_keys(path: Path) -> list[dict[str, str]]:
    return [
        {field: entry[field] for field in ("id", "name", "created_at")}
        for entry in _read_registry(path)["keys"]
    ]


def revoke_key(path: Path, selector: str) -> str:
    revoked_name: str | None = None
    with _locked_registry(path) as payload:
        retained = []
        for entry in payload["keys"]:
            if entry["name"] == selector or entry["id"] == selector:
                if revoked_name is not None:
                    raise ValueError(f"ambiguous key selector: {selector}")
                revoked_name = entry["name"]
            else:
                retained.append(entry)
        if revoked_name is None:
            raise ValueError(f"no key found for: {selector}")
        payload["keys"] = retained
    return revoked_name


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("~/.config/ds4-gateway/keys.json").expanduser(),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="issue a new member key")
    add.add_argument("name")
    revoke = commands.add_parser("revoke", help="revoke by member name or key ID")
    revoke.add_argument("selector")
    commands.add_parser("list", help="list metadata without secrets")
    imported = commands.add_parser(
        "import-file", help="import a key from a private file"
    )
    imported.add_argument("name")
    imported.add_argument("key_file", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "add":
            key = add_key(args.registry, args.name)
            print(f"member={args.name}")
            print(f"api_key={key}")
            print("Store this key now; the gateway cannot display it again.")
        elif args.command == "revoke":
            print(f"revoked={revoke_key(args.registry, args.selector)}")
        elif args.command == "list":
            for entry in list_keys(args.registry):
                print(f"{entry['name']}\t{entry['id']}\t{entry['created_at']}")
        elif args.command == "import-file":
            key_file = args.key_file.expanduser()
            mode = stat.S_IMODE(key_file.stat().st_mode)
            if mode & 0o077:
                raise ValueError(
                    f"import key file must not be group/world accessible: {key_file}"
                )
            import_key(args.registry, args.name, key_file.read_text(encoding="utf-8"))
            print(f"imported={args.name}")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
