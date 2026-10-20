"""Contract for the promotion recipe helper (`promote/scripts/promote-recipe.py`)."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "prompts/skills/promote/scripts"
HERD = ROOT / "prompts/skills/hreview/scripts/herd-config.py"
FILES = ("promote-recipe.py", "promote-recipe.schema.json")


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recipe_mod = load(SOURCE / "promote-recipe.py", "promote_recipe")
herd = load(HERD, "herd_config_for_promote")

FULL: dict[str, Any] = {
    "schema_version": 1,
    "integration_branch": "develop",
    "production_branch": "main",
    "guide": "docs/release/promotion.md",
    "rollback": "docs/runbooks/rollback.md",
    "blocker_issues": {"labels": ["type: bug"]},
    "pull_request": {
        "merge_method": "merge",
        "labels": ["release"],
        "close_stale": True,
        "surface": [{"name": "migrations", "globs": ["db/migrations/**"]}],
        "after_merge": ["The integration branch resets itself after the merge; never reset it by hand."],
    },
    "steps": [
        {"id": "install", "kind": "command", "phase": "preflight", "run": ["npm", "ci"], "fix": ["npm", "install"]},
        {"id": "lint", "kind": "command", "phase": "preflight", "run": ["npm", "run", "lint"]},
        {"id": "gate", "kind": "command", "phase": "preflight", "lane": "gate", "run": ["scripts/gate"], "on_fail": "hold"},
        {"id": "deps", "kind": "task", "phase": "preflight", "brief": "docs/release/deps.md", "writes": True},
        {"id": "integration-deploy", "kind": "watch_workflow", "phase": "preflight", "workflow": "deploy.yml"},
        {"id": "risk", "kind": "risk_review", "phase": "pre_merge", "focus": ["Queue consumers"]},
        {"id": "ci", "kind": "watch_checks", "phase": "pre_merge"},
        {"id": "deploy-app", "kind": "watch_workflow", "phase": "post_merge", "workflow": "deploy.yml"},
        {"id": "deploy-infra", "kind": "watch_workflow", "phase": "post_merge", "workflow": "infra.yml", "if_absent": "skip"},
        {"id": "smoke", "kind": "command", "phase": "post_merge", "lane": "verify", "run": ["scripts/smoke", "production"]},
    ],
}


def recipe(**changes: Any) -> dict[str, Any]:
    value = copy.deepcopy(FULL)
    value.update(changes)
    return value


def errors_of(value: Any) -> list[str]:
    with pytest.raises(recipe_mod.RecipeError) as caught:
        recipe_mod.validate_recipe(value)
    errors: list[str] = caught.value.errors
    return errors


# --- shipped copies --------------------------------------------------------------


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_every_root_ships_the_helper_and_schema(root: str) -> None:
    for name in FILES:
        shipped = ROOT / root / "skills/promote/scripts" / name
        assert shipped.read_bytes() == (SOURCE / name).read_bytes(), shipped
    assert (ROOT / root / "skills/promote/SKILL.md").is_file()


def test_schema_enums_match_the_helper() -> None:
    schema = json.loads((SOURCE / "promote-recipe.schema.json").read_text())
    assert tuple(schema["properties"]) == recipe_mod.TOP_KEYS
    assert tuple(schema["properties"]["pull_request"]["properties"]) == recipe_mod.PR_KEYS
    assert schema["$defs"]["phase"]["enum"] == list(recipe_mod.PHASES)
    assert schema["$defs"]["engine"]["enum"] == list(recipe_mod.ENGINES)
    for kind in recipe_mod.KINDS:
        props = tuple(schema["$defs"][kind]["properties"])
        assert set(props) == set(recipe_mod.COMMON_STEP_KEYS + recipe_mod.STEP_KEYS[kind]), kind


def test_schema_accepts_the_full_recipe() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SOURCE / "promote-recipe.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(FULL)


# --- validation ---------------------------------------------------------------------


def test_full_recipe_applies_defaults() -> None:
    effective = recipe_mod.validate_recipe(FULL)
    steps = {step["id"]: step for step in effective["steps"]}
    assert steps["install"]["lane"] == "main"
    assert steps["install"]["on_fail"] == "block"
    assert steps["install"]["timeout_minutes"] == 30
    assert steps["deps"]["lane"] == "deps"
    assert steps["risk"]["lane"] == "risk" and steps["risk"]["engine"] is None
    assert steps["ci"] == {**steps["ci"], "checks": "required", "rerun_flakes": 1, "timeout_minutes": 60}
    assert steps["deploy-app"]["if_absent"] == "fail"
    assert effective["pull_request"]["title"] == recipe_mod.DEFAULT_TITLE


def test_minimal_recipe() -> None:
    effective = recipe_mod.validate_recipe(
        {"schema_version": 1, "integration_branch": "develop", "production_branch": "main",
         "steps": [{"id": "ci", "kind": "watch_checks", "phase": "pre_merge"}]}
    )
    assert effective["guide"] is None and effective["blocker_issues"] is None
    assert effective["pull_request"]["surface"] == [] and effective["pull_request"]["merge_method"] is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": 2}, "schema_version: must be 1"),
        ({"production_branch": "develop"}, "must differ from integration_branch"),
        ({"integration_branch": "a..b"}, "integration_branch: required; must be a branch name"),
        ({"steps": []}, "steps: required; must be a non-empty array"),
        ({"rollback": "../runbook.md"}, "without '..'"),
        ({"extra": 1}, "top level: unknown key 'extra'"),
        ({"pull_request": {"title": "Release {version}"}}, "unknown field {version}"),
        ({"pull_request": {"merge_method": "fast-forward"}}, "merge_method: must be one of"),
        ({"pull_request": {"surface": [{"name": "migrations", "globs": []}]}}, "must name at least one glob"),
    ],
)
def test_top_level_errors(change: dict[str, Any], message: str) -> None:
    assert any(message in error for error in errors_of(recipe(**change))), errors_of(recipe(**change))


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"id": "x", "kind": "shell", "phase": "preflight"}, "kind: must be one of"),
        ({"id": "X", "kind": "command", "phase": "preflight", "run": ["true"]}, "id: required; must be a lower-case slug"),
        ({"id": "x", "kind": "command", "phase": "preflight", "run": ["npm run lint"]}, "looks like a shell string"),
        ({"id": "x", "kind": "command", "phase": "preflight"}, "run: required"),
        ({"id": "x", "kind": "command", "phase": "preflight", "run": ["t"], "on_fail": "ignore"}, "on_fail: must be one of"),
        ({"id": "x", "kind": "command", "phase": "post_merge", "run": ["t"], "fix": ["f"]}, "cannot carry a fix"),
        ({"id": "x", "kind": "command", "phase": "preflight", "run": ["t"], "timeout_minutes": 0}, "from 1 to 240"),
        ({"id": "x", "kind": "command", "phase": "release", "run": ["t"]}, "phase: must be one of"),
        ({"id": "x", "kind": "risk_review", "phase": "preflight"}, "runs only in pre_merge"),
        ({"id": "x", "kind": "watch_checks", "phase": "post_merge"}, "runs only in pre_merge"),
        ({"id": "x", "kind": "risk_review", "phase": "pre_merge", "engine": "gpt"}, "unknown engine 'gpt'"),
        ({"id": "x", "kind": "watch_workflow", "phase": "post_merge"}, "workflow: required"),
        ({"id": "x", "kind": "task", "phase": "preflight", "brief": "/etc/passwd"}, "relative POSIX path"),
        ({"id": "x", "kind": "task", "phase": "preflight", "brief": "a.md", "run": ["t"]}, "unknown key 'run'"),
    ],
)
def test_step_errors(step: dict[str, Any], message: str) -> None:
    errors = errors_of(recipe(steps=[step]))
    assert any(message in error for error in errors), errors


def test_duplicate_ids_and_singletons() -> None:
    steps = [
        {"id": "ci", "kind": "watch_checks", "phase": "pre_merge"},
        {"id": "ci", "kind": "watch_checks", "phase": "pre_merge", "lane": "ci2"},
    ]
    errors = errors_of(recipe(steps=steps))
    assert any("duplicate 'ci'" in error for error in errors)
    assert any("at most one watch_checks" in error for error in errors)


def test_a_lane_is_one_phase_and_one_executor() -> None:
    spans = [
        {"id": "a", "kind": "command", "phase": "preflight", "run": ["t"]},
        {"id": "b", "kind": "command", "phase": "post_merge", "run": ["t"]},
    ]
    assert any("spans phases" in error for error in errors_of(recipe(steps=spans)))
    mixed = [
        {"id": "a", "kind": "command", "phase": "preflight", "run": ["t"]},
        {"id": "b", "kind": "task", "phase": "preflight", "brief": "x.md", "lane": "main"},
    ]
    assert any("mixes shell steps" in error for error in errors_of(recipe(steps=mixed)))
    two_tasks = [
        {"id": "a", "kind": "task", "phase": "preflight", "brief": "x.md", "lane": "t"},
        {"id": "b", "kind": "task", "phase": "preflight", "brief": "y.md", "lane": "t"},
    ]
    assert any("more than one step" in error for error in errors_of(recipe(steps=two_tasks)))


def test_title_renders_fields() -> None:
    assert recipe_mod.render_title(recipe_mod.validate_recipe(FULL), "2026-01-02") == "Promote develop → main — 2026-01-02"


# --- legacy herd config -------------------------------------------------------------


def test_recipe_from_legacy_herd_promotion_keys() -> None:
    config = herd.validate_repo(
        {
            "schema_version": 1,
            "integration_branch": "develop",
            "promotion": {
                "production_branch": "main",
                "risk_engine": "codex",
                "preflight": [["make", "check"], ["make", "audit"]],
                "deploy_workflows": ["Deploy App", "deploy-app.yml"],
                "post_merge": [["scripts/smoke"]],
            },
        }
    )
    assert config["promotion"]["skill"] == "promote"
    effective = recipe_mod.from_herd({"config": config})
    by_id = {step["id"]: step for step in effective["steps"]}
    assert [s["id"] for s in effective["steps"] if s["phase"] == "preflight"] == [
        "preflight-1", "preflight-2", "pre-deploy-app", "pre-deploy-app-2"
    ]
    assert by_id["preflight-1"]["lane"] == by_id["preflight-2"]["lane"] == "preflight"
    assert by_id["risk"]["engine"] == "codex"
    assert by_id["post-deploy-app"]["workflow"] == "Deploy App"
    assert by_id["post-merge-1"]["lane"] == "verify"
    assert effective["integration_branch"] == "develop" and effective["production_branch"] == "main"


def test_from_herd_needs_promotion() -> None:
    with pytest.raises(recipe_mod.UsageError):
        recipe_mod.from_herd({"config": herd.validate_repo({"schema_version": 1})})


def test_explicit_skill_still_overrides_the_default() -> None:
    config = herd.validate_repo({"schema_version": 1, "promotion": {"skill": "release", "production_branch": "main"}})
    assert config["promotion"]["skill"] == "release"


# --- layout ---------------------------------------------------------------------------


def lanes_of(layout: dict[str, Any], phase: str) -> list[list[str]]:
    entry = next(p for p in layout["phases"] if p["phase"] == phase)
    return [[lane["lane"] for lane in pane["lanes"]] for pane in entry["panes"]]


def test_layout_of_the_full_recipe() -> None:
    layout = recipe_mod.layout(recipe_mod.validate_recipe(FULL))
    assert lanes_of(layout, "preflight") == [["main"], ["gate"], ["deps"], ["integration-deploy"]]
    assert lanes_of(layout, "pre_merge") == [["risk"], ["ci"]]
    assert lanes_of(layout, "post_merge") == [["deploy-app"], ["deploy-infra"], ["verify"]]
    assert layout["worker_slots"] == 5
    pre = next(p for p in layout["phases"] if p["phase"] == "preflight")
    assert pre["fix_slots"] == 1
    executors = {lane["lane"]: lane["executor"] for pane in pre["panes"] for lane in pane["lanes"]}
    assert executors == {"main": "shell", "gate": "shell", "deps": "session", "integration-deploy": "shell"}


def test_layout_omits_empty_phases() -> None:
    layout = recipe_mod.layout(recipe_mod.validate_recipe(recipe(steps=[{"id": "ci", "kind": "watch_checks", "phase": "pre_merge"}])))
    assert [p["phase"] for p in layout["phases"]] == ["pre_merge"]
    assert layout["worker_slots"] == 2


def _many(phase: str, shells: int, watchers: int) -> list[dict[str, Any]]:
    steps = [{"id": f"c{i}", "kind": "command", "phase": phase, "lane": f"c{i}", "run": ["t"]} for i in range(shells)]
    steps += [{"id": f"w{i}", "kind": "watch_workflow", "phase": phase, "workflow": f"w{i}.yml"} for i in range(watchers)]
    return steps


def test_short_lanes_fold_before_watchers() -> None:
    layout = recipe_mod.layout(recipe_mod.validate_recipe(recipe(steps=_many("preflight", 4, 2))))
    # Four panes at most in a phase that can raise a blocker; the watchers keep theirs.
    assert lanes_of(layout, "preflight") == [["c0"], ["c1", "c2", "c3"], ["w0"], ["w1"]]


def test_watchers_fold_only_when_alone() -> None:
    layout = recipe_mod.layout(recipe_mod.validate_recipe(recipe(steps=_many("post_merge", 0, 8))))
    panes = lanes_of(layout, "post_merge")
    assert len(panes) == 5
    assert [lane for pane in panes for lane in pane] == [f"w{i}" for i in range(8)]


@pytest.mark.parametrize("shells", range(0, 9))
@pytest.mark.parametrize("watchers", range(0, 9))
def test_never_more_than_six_panes(shells: int, watchers: int) -> None:
    if shells + watchers == 0:
        return
    for phase in ("preflight", "post_merge"):
        steps = _many(phase, shells, watchers)
        effective = recipe_mod.validate_recipe(recipe(steps=steps))
        first = recipe_mod.layout(effective)
        assert first == recipe_mod.layout(copy.deepcopy(effective)), "layout must be deterministic"
        assert 1 + first["worker_slots"] <= recipe_mod.MAX_PANES
        for entry in first["phases"]:
            panes = entry["panes"]
            assert len(panes) + entry["fix_slots"] <= recipe_mod.WORKER_SLOTS
            if entry["phase"] in recipe_mod.FIX_PHASES:
                assert entry["fix_slots"] >= 1
            # Every lane is placed exactly once, in recipe order.
            placed = [lane["lane"] for pane in panes for lane in pane["lanes"]]
            assert sorted(placed) == sorted({s["lane"] for s in effective["steps"]})
            assert [p["slot"] for p in panes] == list(range(1, len(panes) + 1))


# --- running a lane -------------------------------------------------------------------


SHA = "a" * 40


def test_run_lane_stops_at_the_first_failure(tmp_path: Path) -> None:
    steps = [
        {"id": "ok", "kind": "command", "phase": "preflight", "run": ["true"]},
        {"id": "soft", "kind": "command", "phase": "preflight", "run": ["false"], "on_fail": "warn"},
        {"id": "hard", "kind": "command", "phase": "preflight", "run": ["false"], "fix": ["true"]},
        {"id": "after", "kind": "command", "phase": "preflight", "run": ["true"]},
    ]
    result = recipe_mod.run_lane(recipe_mod.validate_recipe(recipe(steps=steps)), "preflight", "main", tmp_path, SHA, None)
    assert [(s["step"], s["outcome"]) for s in result["steps"]] == [
        ("ok", "ok"), ("soft", "warn"), ("hard", "failed"), ("after", "not-run")
    ]
    assert result["outcome"] == "failed"
    assert result["steps"][2]["fix"] == ["true"]


def test_run_cli_prints_the_sentinel_and_writes_the_result(tmp_path: Path) -> None:
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe(steps=[
        {"id": "hold", "kind": "command", "phase": "preflight", "run": ["false"], "on_fail": "hold"},
    ])))
    out = tmp_path / "result.json"
    done = subprocess.run(
        ["python3", "-I", str(SOURCE / "promote-recipe.py"), "run", "--recipe", str(path), "--phase", "preflight",
         "--lane", "main", "--cwd", str(tmp_path), "--sha", SHA, "--out", str(out)],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 3
    assert done.stdout.rstrip().splitlines()[-1] == "PROMOTE-LANE preflight/main hold"
    assert json.loads(out.read_text())["outcome"] == "hold"


def test_run_refuses_session_lanes_and_short_shas(tmp_path: Path) -> None:
    effective = recipe_mod.validate_recipe(FULL)
    with pytest.raises(recipe_mod.UsageError, match="session"):
        recipe_mod.run_lane(effective, "preflight", "deps", tmp_path, SHA, None)
    with pytest.raises(recipe_mod.UsageError, match="--pr"):
        recipe_mod.run_lane(effective, "pre_merge", "ci", tmp_path, SHA, None)
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(FULL))
    done = subprocess.run(
        ["python3", "-I", str(SOURCE / "promote-recipe.py"), "run", "--recipe", str(path), "--phase", "preflight",
         "--lane", "main", "--cwd", str(tmp_path), "--sha", "abc"],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 2


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def workflow_step(**extra: Any) -> dict[str, Any]:
    step = {"id": "w", "kind": "watch_workflow", "phase": "post_merge", "workflow": "deploy.yml", **extra}
    validated: dict[str, Any] = recipe_mod.validate_recipe(recipe(steps=[step]))["steps"][0]
    return validated


def test_watch_workflow_waits_for_the_newest_run() -> None:
    clock = FakeClock()
    answers = iter([
        [],
        [{"databaseId": 1, "status": "in_progress", "createdAt": "2026-01-01T00:00:00Z"}],
        [
            {"databaseId": 1, "status": "completed", "conclusion": "cancelled", "createdAt": "2026-01-01T00:00:00Z"},
            {"databaseId": 2, "status": "completed", "conclusion": "success", "createdAt": "2026-01-01T00:01:00Z"},
        ],
    ])

    def runner(argv: list[str], cwd: Path, timeout: int | None) -> tuple[int, str]:
        assert argv[:3] == ["gh", "run", "list"] and SHA in argv
        return 0, json.dumps(next(answers))

    result = recipe_mod.watch_workflow(workflow_step(), SHA, Path("."), runner, clock.sleep, clock)
    assert result["outcome"] == "ok" and result["run"] == 2


@pytest.mark.parametrize(("if_absent", "outcome"), [("fail", "absent"), ("skip", "skipped")])
def test_watch_workflow_absent(if_absent: str, outcome: str) -> None:
    clock = FakeClock()
    result = recipe_mod.watch_workflow(
        workflow_step(if_absent=if_absent), SHA, Path("."), lambda *_: (0, "[]"), clock.sleep, clock
    )
    assert result["outcome"] == outcome


def test_watch_checks_reports_failures_and_head_moves() -> None:
    step = recipe_mod.validate_recipe(FULL)["steps"][6]
    assert step["kind"] == "watch_checks"
    clock = FakeClock()
    checks = iter([
        (8, [{"name": "build", "bucket": "pending"}, {"name": "lint", "bucket": "pass"}]),
        (1, [{"name": "build", "bucket": "fail"}, {"name": "lint", "bucket": "pass"}]),
    ])

    def runner(argv: list[str], cwd: Path, timeout: int | None) -> tuple[int, str]:
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"headRefOid": SHA})
        assert "--required" in argv
        code, body = next(checks)
        return code, json.dumps(body)

    result = recipe_mod.watch_checks(step, 7, SHA, Path("."), runner, clock.sleep, clock)
    assert result["outcome"] == "failed" and result["failing"] == ["build"]

    moved = recipe_mod.watch_checks(
        step, 7, SHA, Path("."), lambda *_: (0, json.dumps({"headRefOid": "b" * 40})), clock.sleep, clock
    )
    assert moved["outcome"] == "failed" and "expected" in moved["detail"]


# --- reading from the integration branch ----------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    git(work, "checkout", "-q", "-b", "develop")
    (work / "README").write_text("x\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "init")
    git(work, "push", "-q", "origin", "develop")
    return origin, work


def show(work: Path, *extra: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-I", str(SOURCE / "promote-recipe.py"), "show", "--repo-dir", str(work),
         "--default-branch", "develop", *extra],
        capture_output=True, text=True, check=False, input=stdin,
    )


def test_show_reads_the_remote_branch_not_the_working_tree(remote: tuple[Path, Path]) -> None:
    _, work = remote
    assert show(work).returncode == 4
    (work / ".activeloom-promote.json").write_text(json.dumps(FULL))
    assert show(work).returncode == 4, "an uncommitted recipe must not count"
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "recipe")
    git(work, "push", "-q", "origin", "develop")
    done = show(work)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["source"]["kind"] == "file"


def test_show_falls_back_to_the_herd_config_and_checks_agreement(remote: tuple[Path, Path]) -> None:
    _, work = remote
    herd_out = {"source": {"ref": "origin/develop", "commit": "c"}, "config": herd.validate_repo(
        {"schema_version": 1, "integration_branch": "develop",
         "promotion": {"production_branch": "release", "preflight": [["make", "check"]]}}
    )}
    done = show(work, "--herd", "-", stdin=json.dumps(herd_out))
    assert done.returncode == 0, done.stderr
    shown = json.loads(done.stdout)
    assert shown["source"]["kind"] == "herd" and shown["recipe"]["production_branch"] == "release"
    (work / ".activeloom-promote.json").write_text(json.dumps(FULL))
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "recipe")
    git(work, "push", "-q", "origin", "develop")
    done = show(work, "--herd", "-", stdin=json.dumps(herd_out))
    assert done.returncode == 1 and "disagree" in done.stderr


def test_documented_example_validates_and_lays_out_as_described() -> None:
    guide = (ROOT / "docs/herd-orchestration.md").read_text()
    section = guide.split("#### The promotion recipe\n", 1)[1]
    example = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])
    layout = recipe_mod.layout(recipe_mod.validate_recipe(example))
    assert [(p["phase"], len(p["panes"])) for p in layout["phases"]] == [
        ("preflight", 4), ("pre_merge", 2), ("post_merge", 2)
    ]
    assert "four preflight slots, two pre-merge slots and two post-merge slots" in section
