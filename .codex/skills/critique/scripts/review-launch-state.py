#!/usr/bin/env python3
"""Durable launcher boundary evidence; never infer a safe retry from a log."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path


def record(phase: str, reason: str | None = None) -> None:
    filename = os.environ.get("ACTIVELOOM_LAUNCH_STATE")
    if not filename:
        return
    path = Path(filename)
    if path.is_symlink():
        raise ValueError("launch state cannot be a symlink")
    if path.exists():
        previous = json.loads(path.read_text())
        if previous.get("attempt_id") != os.environ["ACTIVELOOM_ATTEMPT_ID"]:
            raise ValueError("launch state belongs to another attempt")
        if previous.get("phase") == "execution":
            raise ValueError("execution evidence cannot return to preflight")
    value = {
        "version": 1,
        "attempt_id": os.environ["ACTIVELOOM_ATTEMPT_ID"],
        "phase": phase,
        # The write precedes exec: a crash in that gap is deliberately unknown,
        # never proof that another mutating reviewer can safely be launched.
        "review_started": None if phase == "execution" else False,
        "failure_reason": reason,
    }
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".launch-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def verify_installation() -> None:
    filename = os.environ.get("ACTIVELOOM_INSTALLATION_MANIFEST")
    if not filename:
        return
    path = Path(filename)
    if path.is_symlink() or hashlib.sha256(
        path.read_bytes()
    ).hexdigest() != os.environ.get("ACTIVELOOM_INSTALLATION_SHA256"):
        raise ValueError("review installation manifest changed")
    manifest = json.loads(path.read_text())
    root = path.parent / "native"
    expected = manifest["files"]
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError(f"review installation contains a symlink: {root}")
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if not p.is_dir()}
    if actual != set(expected):
        raise ValueError(f"review installation paths changed: {root}")
    for name, sha in expected.items():
        candidate = root / name
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or hashlib.sha256(candidate.read_bytes()).hexdigest() != sha
        ):
            raise ValueError(f"review installation changed: {candidate}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "ready", "execution", "verify"))
    parser.add_argument("reason", nargs="?")
    args = parser.parse_args()
    if args.phase == "verify":
        verify_installation()
    else:
        record(args.phase, args.reason)
