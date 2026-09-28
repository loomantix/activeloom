"""Require complete, disjoint pytest collections and coverage from every shard."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys


def collection(path: Path) -> list[str]:
    nodes = [line for line in path.read_text().splitlines() if line.startswith("tests/") and "::" in line]
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError(f"Empty or duplicate test collection: {path}")
    return nodes


def verify(root: Path, count: int = 4) -> int:
    expected: set[str] | None = None
    actual: Counter[str] = Counter()
    for index in range(1, count + 1):
        directory = root / f"python-shard-{index}"
        full = set(collection(directory / "full.txt"))
        if expected is not None and full != expected:
            raise ValueError(f"Inconsistent full collection in shard {index}")
        expected = full
        actual.update(collection(directory / "selected.txt"))
        for name in (".coverage", "junit.xml"):
            if (directory / name).stat().st_size == 0:
                raise ValueError(f"Empty shard artifact: {directory / name}")
    if set(actual) != expected or any(value != 1 for value in actual.values()):
        raise ValueError("Shards must cover every collected test exactly once")
    return len(actual)


if __name__ == "__main__":
    print(f"Verified {verify(Path(sys.argv[1]))} tests across four shards")
