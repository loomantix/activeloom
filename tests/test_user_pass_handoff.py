"""Session mode: the run controller renders the handoff for a pass the user launches."""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

HEAD = "a" * 40
BASE = "b" * 40
RUN_ID = "d" * 64
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("offline_github")


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = ROOT / ".claude/skills/critique/scripts/local-review-handoff.py"
    spec = importlib.util.spec_from_file_location("user_handoff_controller", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SILENT_SCOPE", None)
    return module


def run_record(tier: str = "deep", sequence: list[str] | None = None) -> dict[str, Any]:
    return {
        "comment_id": 20,
        "run_id": RUN_ID,
        "base": BASE,
        "start_head": HEAD,
        "tier": tier,
        "max_rounds": 4 if tier == "deep" else 2,
        "sequence": sequence or ["claude", "codex", "gemini"],
        "plan_mode": "cycle",
    }


def clean_pass(index: int, engine: str, number: int = 1) -> dict[str, Any]:
    marker = (
        f"<!-- local-review-pass:v3 engine={engine} round={number} base={BASE} "
        f"head={HEAD} result-sha256={'c' * 64} -->"
    )
    return {"id": 21 + index, "body": marker + "\nCompleted review result."}


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    for root in (".claude", ".codex", ".agents"):
        pin = tmp_path / root / "skills/critique/scripts/review-ledger.version"
        pin.parent.mkdir(parents=True)
        pin.write_text("1.18.0\n")
    return tmp_path


def wire(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    *,
    record: dict[str, Any] | None = None,
    ended: bool = False,
) -> None:
    monkeypatch.setattr(controller, "_issue_comments", lambda *_: rows)
    monkeypatch.setattr(
        controller, "_run_records", lambda _: [record or run_record()]
    )
    monkeypatch.setattr(
        controller, "_run_end", lambda *_: {"outcome": "exhausted"} if ended else None
    )
    monkeypatch.setattr(controller, "_verify_head", lambda *_: None)


def args(path: Path, **fields: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "repo": "example/repo",
        "pr": 7,
        "head": HEAD,
        "engine": None,
        "worktree": str(path),
        "model": "example-model",
        "effort": "high",
        "launch_arg": None,
        "env": None,
        "context_file": None,
        "bound_entry_file": None,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def render(
    controller: ModuleType, capsys: pytest.CaptureFixture[str], namespace: SimpleNamespace
) -> str:
    controller._user_handoff(namespace)
    return capsys.readouterr().out


def launch_line(block: str) -> list[str]:
    line = next(item.strip() for item in block.splitlines() if " && " in item)
    cd, _, command = line.partition(" && ")
    return [*shlex.split(cd), "&&", *shlex.split(command)]


@pytest.mark.parametrize(
    ("rows", "engine", "expected"),
    [
        (
            [],
            "claude",
            [
                "env", "ANTHROPIC_MODEL=example-model", "CLAUDE_CODE_EFFORT_LEVEL=high",
                "claude", "--model", "example-model", "--effort", "high",
            ],
        ),
        (
            [clean_pass(0, "claude")],
            "codex",
            ["codex", "-m", "example-model", "-c", 'model_reasoning_effort="high"'],
        ),
        (
            [clean_pass(0, "claude"), clean_pass(1, "codex")],
            "gemini",
            ["agy", "--model", "example-model", "--effort", "high"],
        ),
    ],
)
def test_handoff_renders_each_engines_launch_preflight_and_skill(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
    rows: list[dict[str, Any]],
    engine: str,
    expected: list[str],
) -> None:
    wire(controller, monkeypatch, rows)
    block = render(
        controller,
        capsys,
        args(worktree, launch_arg=["--dangerously-skip-permissions"]),
    )
    assert launch_line(block) == [
        "cd", str(worktree), "&&", *expected, "--dangerously-skip-permissions",
    ]
    root = {"claude": ".claude", "codex": ".codex", "gemini": ".agents"}[engine]
    assert f'test "$(git rev-parse HEAD)" = {HEAD}' in block
    assert (
        f'test "$(node {root}/skills/critique/scripts/review-ledger.js --version)" = 1.18.0'
        in block
    )
    invocation = (
        "Use .codex/skills/deepcritique/SKILL.md for 7"
        if engine == "codex"
        else "/deepcritique 7"
    )
    assert invocation in block
    assert f"run {RUN_ID}, round 1, pinned base {BASE}, exact head {HEAD}" in block
    assert f"USER PASS: example/repo#7 {engine} r1" in block
    assert "refactorpass" not in block


def test_lean_first_pass_names_refactorpass_and_inherit_omits_the_model(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
) -> None:
    wire(controller, monkeypatch, [], record=run_record("lean", ["codex", "claude"]))
    block = render(controller, capsys, args(worktree, model="inherit", effort="medium"))
    assert launch_line(block)[3:] == ["codex", "-c", 'model_reasoning_effort="medium"']
    assert "Use .codex/skills/refactorpass/SKILL.md for 7" in block
    assert "Use .codex/skills/critique/SKILL.md for 7" in block


def test_bound_handoff_supplies_only_the_assigned_entry_instructions(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    wire(controller, monkeypatch, [], record=run_record("lean", ["codex", "claude"]))
    monkeypatch.setattr(controller, "SILENT_SCOPE", ("example/repo", 7))
    entry_file = tmp_path / "entry"
    entry = "\n".join(f"python3 -IB /tools/entry.py enter --skill {s}" for s in ("refactorpass", "critique"))
    entry_file.write_text(entry)
    block = render(controller, capsys, args(tmp_path, bound_entry_file=str(entry_file)))
    assert entry in block and block.index(entry) < block.index("Review pass for")
    assert ".codex/skills" not in block
    assert "pass `silent` to the skill" not in block
    assert "This PR is silent: keep the ledger local per the protocol's Silent mode." in block
    assert f"run {RUN_ID}, round 1, pinned base {BASE}, exact head {HEAD}" in block


def test_pane_env_entries_reach_the_launch_line(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
) -> None:
    wire(controller, monkeypatch, [])
    block = render(
        controller,
        capsys,
        args(worktree, env=["LOOM_REVIEW_TELEMETRY_STORE=issue", "EXTRA=a b"]),
    )
    assert launch_line(block)[3:] == [
        "env", "LOOM_REVIEW_TELEMETRY_STORE=issue", "EXTRA=a b",
        "ANTHROPIC_MODEL=example-model", "CLAUDE_CODE_EFFORT_LEVEL=high",
        "claude", "--model", "example-model", "--effort", "high",
    ]


def test_claude_pins_effort_in_the_environment_even_with_an_inherited_model(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
) -> None:
    # Claude gives CLAUDE_CODE_EFFORT_LEVEL precedence over --effort, so a value
    # inherited from the user's terminal must not survive the launch line.
    wire(controller, monkeypatch, [])
    block = render(controller, capsys, args(worktree, model="inherit", effort="medium"))
    assert launch_line(block)[3:] == [
        "env", "CLAUDE_CODE_EFFORT_LEVEL=medium", "claude", "--effort", "medium",
    ]


@pytest.mark.parametrize("entry", ["NOEQUALS", "1BAD=x", "KEY=line\nbreak", "-x=y"])
def test_malformed_pane_env_entry_is_refused(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
    entry: str,
) -> None:
    wire(controller, monkeypatch, [])
    with pytest.raises(controller.HandoffError, match="not KEY=VALUE"):
        controller._user_handoff(args(worktree, env=[entry]))
    assert not capsys.readouterr().out


def test_gemini_requires_an_explicit_model(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
) -> None:
    wire(controller, monkeypatch, [], record=run_record(sequence=["gemini", "claude"]))
    with pytest.raises(controller.HandoffError, match="explicit pinned model"):
        controller._user_handoff(args(worktree, model="inherit"))
    assert not capsys.readouterr().out


def test_silent_pr_and_context_reach_the_prompt(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
    tmp_path: Path,
) -> None:
    wire(controller, monkeypatch, [])
    monkeypatch.setattr(controller, "SILENT_SCOPE", ("example/repo", 7))
    context = tmp_path / "context.txt"
    context.write_text("Carried findings: https://example.invalid/thread/1\n")
    block = render(controller, capsys, args(worktree, context_file=str(context)))
    assert "/deepcritique 7 silent" in block
    assert "This PR is silent" in block
    assert "Carried findings: https://example.invalid/thread/1" in block

    wire(controller, monkeypatch, [], record=run_record("lean", ["codex", "claude"]))
    lean_block = render(controller, capsys, args(worktree))
    assert "Use .codex/skills/refactorpass/SKILL.md for 7 silent" in lean_block
    assert "Use .codex/skills/critique/SKILL.md for 7 silent" in lean_block


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("no-run", "no authenticated local-review run"),
        ("ended", "run has ended"),
        ("converged", "no pass is due: the run reports converged"),
        ("exhausted", "no pass is due: the run reports exhausted"),
        ("wrong-engine", "belongs to claude, not gemini"),
        ("head-moved", "head moved"),
        ("relative", "absolute path"),
        ("no-pin", "no ledger version pin"),
    ],
)
def test_handoff_refuses_when_no_pass_is_due_or_the_run_is_not_active(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
    tmp_path: Path,
    setup: str,
    message: str,
) -> None:
    rows: list[dict[str, Any]] = []
    record = run_record()
    fields: dict[str, Any] = {}
    if setup == "converged":
        rows = [
            clean_pass(0, "claude"), clean_pass(1, "codex"), clean_pass(2, "gemini"),
        ]
    if setup == "exhausted":
        record = run_record("lean", ["claude", "codex"])
        rows = [
            {"id": 21 + index, "body": body}
            for index, body in enumerate(
                f"<!-- local-review-complete:v3 engine={engine} round={number} "
                f"base={BASE} before={HEAD} head={HEAD} classification=material "
                f"fingerprints=x result-sha256={'c' * 64} -->\nResult."
                for number in (1, 2)
                for engine in ("claude", "codex")
            )
        ]
    wire(controller, monkeypatch, rows, record=record, ended=setup == "ended")
    if setup == "no-run":
        monkeypatch.setattr(controller, "_run_records", lambda _: [])
    if setup == "wrong-engine":
        fields["engine"] = "gemini"
    if setup == "head-moved":
        monkeypatch.setattr(
            controller, "_verify_head", lambda *_: controller._fail("head moved")
        )
    if setup == "relative":
        fields["worktree"] = "relative/path"
    if setup == "no-pin":
        fields["worktree"] = str(tmp_path / "empty")
    with pytest.raises(controller.HandoffError, match=message):
        controller._user_handoff(args(worktree, **fields))
    assert not capsys.readouterr().out


def test_handoff_is_read_only(
    controller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worktree: Path,
) -> None:
    wire(controller, monkeypatch, [])

    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("user-handoff must not write")

    monkeypatch.setattr(controller, "_post_issue_comment", refuse)
    render(controller, capsys, args(worktree))


def test_cli_parses_flag_shaped_launch_arguments(
    controller: ModuleType,
) -> None:
    namespace = controller._parser().parse_args(
        [
            "user-handoff", "--repo", "example/repo", "--pr", "7", "--head", HEAD,
            "--worktree", "/tmp/x", "--model", "m", "--effort", "high",
            "--launch-arg=--settings", "--launch-arg=/tmp/settings.json",
            "--env=LOOM_REVIEW_TELEMETRY_STORE=issue",
        ]
    )
    assert namespace.handler is controller._user_handoff
    assert namespace.launch_arg == ["--settings", "/tmp/settings.json"]
    assert namespace.env == ["LOOM_REVIEW_TELEMETRY_STORE=issue"]
