"""Repository telemetry consent must reach every review harness."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("harness", [".claude", ".codex", ".agents"])
@pytest.mark.parametrize("invalid_config", [False, True])
@pytest.mark.parametrize(
    "override,expected", [(None, True), ("off", False), ("invalid", False)]
)
def test_repository_gate_without_shell_exports(
    tmp_path: Path,
    harness: str,
    override: str | None,
    expected: bool,
    invalid_config: bool,
) -> None:
    source = ROOT / harness / "skills/critique/scripts"
    target = tmp_path / "scripts"
    target.mkdir()
    for name in ("usage-snapshot.js", "review-telemetry-gates.js", "package.json"):
        if (source / name).exists():
            shutil.copyfile(source / name, target / name)
    (target / "review-telemetry.json").write_text(
        json.dumps(
            {
                "LOOM_REVIEW_TELEMETRY": "on",
                "LOOM_REVIEW_TELEMETRY_EXTRACT": "off",
            }
        )
    )
    if invalid_config:
        (target / "review-telemetry.json").write_text("{broken")
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("LOOM_REVIEW_TELEMETRY")
    }
    if override is not None:
        env["LOOM_REVIEW_TELEMETRY"] = override
    result = subprocess.run(
        [
            "node",
            str(target / "usage-snapshot.js"),
            "delta",
            "--out-dir",
            str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["emit"] is (expected and not invalid_config)
    assert payload["enabled"] is False
    assert payload["tokenSource"] == "unavailable"
    if invalid_config:
        assert (
            payload["error"]
            == "review telemetry configuration is invalid or unreadable"
        )


@pytest.mark.parametrize("harness", [".claude", ".codex", ".agents"])
def test_gate_reader_is_identical_across_harnesses(harness: str) -> None:
    relative = "skills/critique/scripts/review-telemetry-gates.js"
    assert (ROOT / harness / relative).read_bytes() == (
        ROOT / ".claude" / relative
    ).read_bytes()
