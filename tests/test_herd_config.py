"""Contract for the herd orchestration config helper (`hreview/scripts/herd-config.py`)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "prompts/skills/hreview/scripts"
FIXTURES = Path(__file__).parent / "fixtures/herd-config"
FILES = ("herd-config.py", "herd-config.schema.json", "herd-user.schema.json")

# The schema is for editors; the helper is the gate. These fixtures are
# rejected only by checks a JSON schema cannot express.
HELPER_ONLY = {"shell-string.json", "escaping-glob.json"}

INVALID = {
    "schema-version.json": "schema_version: must be 1",
    "unknown-section.json": "unknown key 'shipping'",
    "shell-string.json": "looks like a shell string",
    "flat-argv.json": "worktree.bootstrap[0]: must be an array",
    "label-missing.json": "ship.label: required",
    "unknown-engine.json": "unknown engine 'gpt'",
    "escaping-glob.json": "without '..'",
    "promotion-branch-missing.json": "promotion.production_branch: required",
    "glance-not-boolean.json": "glance.auto: must be true or false",
    "bad-lane.json": "production_lanes[0].name: must be a lower-case slug",
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("herd_config", SOURCE / "herd-config.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


herd = load()


def fixture(kind: str, name: str) -> Any:
    return json.loads((FIXTURES / kind / name).read_text())


# --- shipped copies --------------------------------------------------------------


@pytest.mark.parametrize("root", [".claude", ".codex", ".agents"])
def test_every_root_ships_the_helper_and_schemas(root: str) -> None:
    for name in FILES:
        copy = ROOT / root / "skills/hreview/scripts" / name
        assert copy.read_bytes() == (SOURCE / name).read_bytes(), copy


def test_schema_sections_match_the_helper() -> None:
    repo_schema = json.loads((SOURCE / "herd-config.schema.json").read_text())
    user_schema = json.loads((SOURCE / "herd-user.schema.json").read_text())
    assert tuple(repo_schema["properties"]) == herd.REPO_SECTIONS
    assert tuple(user_schema["properties"]) == herd.USER_SECTIONS
    assert repo_schema["$defs"]["engine"]["enum"] == list(herd.ENGINES)
    assert repo_schema["properties"]["ship"]["properties"]["on_ship"]["enum"] == list(herd.ON_SHIP)
    assert repo_schema["properties"]["ship"]["properties"]["checks"]["enum"] == list(herd.CHECKS)


# --- repository config validation -------------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in (FIXTURES / "valid").glob("*.json")))
def test_valid_fixtures_pass(name: str) -> None:
    herd.validate_repo(fixture("valid", name))


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_fixtures_fail_with_a_named_error(name: str) -> None:
    with pytest.raises(herd.ConfigError) as caught:
        herd.validate_repo(fixture("invalid", name))
    assert any(INVALID[name] in error for error in caught.value.errors), caught.value.errors


def test_every_invalid_fixture_has_an_expectation() -> None:
    assert {p.name for p in (FIXTURES / "invalid").glob("*.json")} == set(INVALID)


def test_schema_agrees_with_the_helper_on_fixtures() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SOURCE / "herd-config.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    for path in sorted((FIXTURES / "valid").glob("*.json")):
        assert not list(validator.iter_errors(json.loads(path.read_text()))), path.name
    for path in sorted((FIXTURES / "invalid").glob("*.json")):
        errors = list(validator.iter_errors(json.loads(path.read_text())))
        assert bool(errors) == (path.name not in HELPER_ONLY), path.name


def test_absent_sections_take_conservative_defaults() -> None:
    effective = herd.validate_repo({"schema_version": 1})
    assert effective["integration_branch"] is None
    assert effective["worktree"] == {"bootstrap": []}
    assert effective["gate_fix"]["enabled"] is False
    assert effective["glance"] == {"auto": False}
    assert effective["ship"]["enabled"] is False
    assert effective["ship"]["on_ship"] == "none"
    assert effective["builds"] == {"engines": None}
    assert effective["grill"] == {
        "engines": ["claude", "codex", "gemini"],
        "hold_label": "agent-bail: grill-review",
        "verify_first_label": None,
        "persona": None,
    }
    assert effective["production_lanes"] == []
    assert effective["promotion"]["enabled"] is False


def test_present_ship_section_defaults_to_the_stricter_check() -> None:
    ship = herd.validate_repo({"schema_version": 1, "ship": {}})["ship"]
    assert ship == {
        "enabled": True,
        "on_ship": "none",
        "label": None,
        "checks": "all-runs",
        "post_ship_checks": [],
    }


def test_production_branch_must_differ_from_integration_branch() -> None:
    config = {
        "schema_version": 1,
        "integration_branch": "main",
        "promotion": {"skill": "promote", "production_branch": "main"},
    }
    with pytest.raises(herd.ConfigError, match="must differ"):
        herd.validate_repo(config)


def test_every_error_is_reported_at_once() -> None:
    with pytest.raises(herd.ConfigError) as caught:
        herd.validate_repo({"schema_version": 1, "glance": {"auto": 1}, "builds": {"engines": []}})
    assert len(caught.value.errors) == 2


@pytest.mark.parametrize("field", ["name", "engine", "skill"])
def test_null_required_production_lane_field_is_rejected(field: str) -> None:
    lane = {"name": "docs-video", "engine": "codex", "skill": "record", "brief": "docs/brief.md", field: None}
    with pytest.raises(herd.ConfigError, match=rf"production_lanes\[0\].{field}:"):
        herd.validate_repo({"schema_version": 1, "production_lanes": [lane]})


# --- gate-fix classification ----------------------------------------------------------


def gate(**overrides: Any) -> dict[str, Any]:
    config = {"schema_version": 1, "gate_fix": fixture("valid", "full.json")["gate_fix"] | overrides}
    result: dict[str, Any] = herd.validate_repo(config)["gate_fix"]
    return result


def test_tooling_and_test_only_fix_is_eligible() -> None:
    result = herd.classify_paths(gate(), ["scripts/deploy.sh", "apps/web/src/a.test.ts"])
    assert result["eligible"], result
    assert [f["class"] for f in result["files"]] == ["tooling", "test"]


def test_off_limits_wins_over_every_other_class() -> None:
    result = herd.classify_paths(gate(), ["scripts/package.json"])
    assert result["files"][0]["class"] == "off_limits"
    assert not result["eligible"]


def test_product_and_unclassified_paths_fail_closed() -> None:
    result = herd.classify_paths(gate(), ["apps/web/src/a.ts", "README.md"])
    assert not result["eligible"]
    assert any("product code" in reason for reason in result["reasons"])
    assert any("no tooling or test glob" in reason for reason in result["reasons"])


def test_unconfigured_gate_fix_is_never_eligible() -> None:
    disabled = herd.validate_repo({"schema_version": 1})["gate_fix"]
    result = herd.classify_paths(disabled, ["scripts/deploy.sh"])
    assert not result["eligible"]
    assert result["reasons"][0] == "gate_fix is not configured for this repository"


def test_no_built_in_globs() -> None:
    result = herd.classify_paths(gate(off_limits_globs=[], product_globs=[], test_globs=[]), ["infra/main.tf"])
    assert result["files"][0]["class"] == "other"


@pytest.mark.parametrize(
    ("glob", "path", "matches"),
    [
        ("scripts/**", "scripts/a/b.sh", True),
        ("scripts/**", "scripts", False),
        ("**/fixtures/**", "fixtures/a.json", True),
        ("**/fixtures/**", "x/y/fixtures/a.json", True),
        ("packages/*/src/**", "packages/a/src/b.ts", True),
        ("packages/*/src/**", "packages/a/b/src/c.ts", False),
        ("*.md", "docs/a.md", False),
        ("file?.txt", "file1.txt", True),
    ],
)
def test_glob_semantics(glob: str, path: str, matches: bool) -> None:
    assert bool(herd.glob_to_regex(glob).match(path)) is matches


# --- reading from the integration branch at the remote ---------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout.strip()


def commit_config(work: Path, branch: str, config: Any | None, message: str) -> None:
    git(work, "checkout", "-q", "-B", branch)
    target = work / herd.REPO_FILE
    if config is None:
        target.unlink(missing_ok=True)
    else:
        target.write_text(json.dumps(config))
    (work / "touch").write_text(message)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    git(work, "push", "-q", "-f", "origin", branch)


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    git(work, "remote", "add", "origin", str(origin))
    return origin, work


def test_absent_file_uses_the_default_branch(repos: tuple[Path, Path]) -> None:
    _, work = repos
    commit_config(work, "main", None, "init")
    loaded = herd.read_repo(work, "origin", None, fetch=True)
    assert loaded["source"]["present"] is False
    assert loaded["source"]["ref"] == "origin/main"
    assert loaded["config"]["integration_branch"] == "main"
    assert loaded["config"]["ship"]["enabled"] is False


def test_config_on_the_integration_branch_is_authoritative(repos: tuple[Path, Path]) -> None:
    _, work = repos
    pointer = {"schema_version": 1, "integration_branch": "staging"}
    commit_config(work, "main", pointer, "main")
    commit_config(work, "staging", pointer | {"glance": {"auto": True}}, "staging")
    loaded = herd.read_repo(work, "origin", None, fetch=True)
    assert loaded["source"]["ref"] == "origin/staging"
    assert loaded["source"]["commit"] == git(work, "rev-parse", "origin/staging")
    assert loaded["config"]["glance"]["auto"] is True


def test_pull_request_head_cannot_loosen_its_own_rules(repos: tuple[Path, Path]) -> None:
    _, work = repos
    commit_config(work, "main", {"schema_version": 1}, "main")
    commit_config(work, "feature", {"schema_version": 1, "ship": {"checks": "required"}}, "pr")
    loaded = herd.read_repo(work, "origin", "main", fetch=True)
    assert loaded["config"]["ship"]["enabled"] is False


def test_integration_branch_without_the_file_fails_closed(repos: tuple[Path, Path]) -> None:
    _, work = repos
    commit_config(work, "main", {"schema_version": 1, "integration_branch": "staging"}, "main")
    commit_config(work, "staging", None, "staging")
    with pytest.raises(herd.ConfigError, match="must carry the configuration"):
        herd.read_repo(work, "origin", "main", fetch=True)


def test_branches_that_disagree_fail_closed(repos: tuple[Path, Path]) -> None:
    _, work = repos
    commit_config(work, "main", {"schema_version": 1, "integration_branch": "staging"}, "main")
    commit_config(work, "staging", {"schema_version": 1, "integration_branch": "next"}, "staging")
    with pytest.raises(herd.ConfigError, match="names integration branch 'next'"):
        herd.read_repo(work, "origin", "main", fetch=True)


def test_production_branch_matching_default_integration_branch_fails_closed(repos: tuple[Path, Path]) -> None:
    _, work = repos
    commit_config(
        work,
        "main",
        {"schema_version": 1, "promotion": {"skill": "promote", "production_branch": "main"}},
        "main",
    )
    with pytest.raises(herd.ConfigError, match="must differ from integration_branch"):
        herd.read_repo(work, "origin", None, fetch=True)


def test_invalid_remote_config_is_a_config_error(repos: tuple[Path, Path]) -> None:
    _, work = repos
    commit_config(work, "main", {"schema_version": 1, "glance": {"auto": "yes"}}, "main")
    with pytest.raises(herd.ConfigError):
        herd.read_repo(work, "origin", "main", fetch=True)


def test_gate_fix_paths_reads_config_from_the_remote(repos: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _, work = repos
    commit_config(work, "main", {"schema_version": 1, "gate_fix": {"tooling_globs": ["scripts/**"]}}, "main")
    base = git(work, "rev-parse", "HEAD")
    (work / "scripts").mkdir()
    (work / "scripts/fix.sh").write_text("true\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "fix")
    code = herd.main(["gate-fix-paths", "--repo-dir", str(work), "--base", base, "--head", "HEAD"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["eligible"] is True


# --- per-user config ------------------------------------------------------------------


def test_absent_user_config_uses_defaults(tmp_path: Path) -> None:
    loaded = herd.read_user(tmp_path / "missing.json")
    assert loaded["source"]["present"] is False
    config = loaded["config"]
    assert config["push_notifications"] is True
    assert config["worktree_root"] == str(Path("~/wt").expanduser())
    assert config["engines"]["codex"] == {"launch_args": [], "permission_off_args": []}


def test_duplicate_launch_flag_is_rejected() -> None:
    config = {
        "schema_version": 1,
        "engines": {"codex": {"launch_args": ["--yolo"], "permission_off_args": ["--yolo"]}},
    }
    with pytest.raises(herd.ConfigError, match="flag passed more than once: --yolo"):
        herd.validate_user(config)


def test_user_args_omit_permission_flags_for_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "herd.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engines": {"claude": {"launch_args": ["--verbose"], "permission_off_args": ["--skip"]}},
            }
        )
    )
    assert herd.main(["user-args", "--engine", "claude", "--path", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == ["--verbose", "--skip"]
    assert herd.main(["user-args", "--engine", "claude", "--read-only", "--path", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == ["--verbose"]


def test_unknown_user_engine_is_rejected() -> None:
    with pytest.raises(herd.ConfigError, match="unknown key 'agy'"):
        herd.validate_user({"schema_version": 1, "engines": {"agy": {}}})


def permissions(tmp_path: Path, capsys: pytest.CaptureFixture[str], engines: dict[str, Any], *names: str) -> Any:
    path = tmp_path / "herd.json"
    path.write_text(json.dumps({"schema_version": 1, "engines": engines}))
    argv = ["permissions", "--path", str(path)]
    for name in names:
        argv += ["--engine", name]
    assert herd.main(argv) == 0
    return json.loads(capsys.readouterr().out)["engines"]


def test_engine_without_permission_flags_stops_at_prompts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = permissions(tmp_path, capsys, {}, "codex")
    assert report == {
        "codex": {"stops_at_prompts": True, "turned_off": [], "unrecognized": [], "turned_off_in_read_only": []}
    }


def test_permission_flags_name_what_they_turn_off(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    engines = {
        "claude": {"permission_off_args": ["--permission-mode=bypassPermissions"]},
        "codex": {"permission_off_args": ["-a", "never", "--sandbox", "danger-full-access"]},
    }
    report = permissions(tmp_path, capsys, engines, "claude", "codex")
    assert report["claude"]["stops_at_prompts"] is False
    assert report["claude"]["turned_off"] == [
        {"arg": "--permission-mode=bypassPermissions", "turns_off": "every permission check"}
    ]
    assert report["codex"]["turned_off"] == [
        {"arg": "-a never", "turns_off": "approval prompts"},
        {"arg": "--sandbox danger-full-access", "turns_off": "the sandbox"},
    ]
    assert report["codex"]["unrecognized"] == []


def test_safe_value_of_a_known_flag_is_not_a_risk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    engines = {"codex": {"permission_off_args": ["-s", "workspace-write", "--search"]}}
    report = permissions(tmp_path, capsys, engines, "codex")["codex"]
    assert report["turned_off"] == []
    assert report["unrecognized"] == ["-s workspace-write", "--search"]


def test_sandbox_only_flag_does_not_disable_approval_prompts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engines = {"codex": {"permission_off_args": ["-s", "danger-full-access"]}}
    report = permissions(tmp_path, capsys, engines, "codex")["codex"]
    assert report["stops_at_prompts"] is True


def test_approval_flag_in_launch_args_applies_to_editing_sessions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engines = {"codex": {"launch_args": ["-a", "never"]}}
    report = permissions(tmp_path, capsys, engines, "codex")["codex"]
    assert report["stops_at_prompts"] is False


def test_agy_permission_flag_disables_approval_prompts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    engines = {"gemini": {"permission_off_args": ["--dangerously-skip-permissions"]}}
    report = permissions(tmp_path, capsys, engines, "gemini")["gemini"]
    assert report["stops_at_prompts"] is False
    assert report["unrecognized"] == []


def test_permission_flag_in_launch_args_reaches_read_only_sessions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engines = {"claude": {"launch_args": ["--dangerously-skip-permissions"]}}
    report = permissions(tmp_path, capsys, engines, "claude")["claude"]
    assert report["stops_at_prompts"] is False
    assert report["turned_off_in_read_only"] == [
        {"arg": "--dangerously-skip-permissions", "turns_off": "every permission check"}
    ]


# --- state paths and locks ----------------------------------------------------------------


def test_state_paths_are_flat_per_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    paths = herd.state_paths("acme/widgets", "hreview", "pr-12")
    assert paths["state"] == str(tmp_path / "cache/activeloom-herd/acme__widgets/hreview/pr-12")
    assert paths["scratch"] == str(tmp_path / "tmp/activeloom-herd/acme__widgets/hreview/pr-12")


@pytest.mark.parametrize("repo", ["acme", "acme/../x", "../acme/w", "acme/w/x"])
def test_malformed_repository_is_refused(repo: str) -> None:
    with pytest.raises(herd.UsageError):
        herd.flat_repo(repo)


def test_lock_is_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    args = ["--repo", "acme/widgets", "--skill", "hgrill", "--key", "issue-7"]
    assert herd.main(["lock", *args]) == 0
    assert herd.main(["lock", *args]) == 3
    assert herd.main(["unlock", *args]) == 0
    assert herd.main(["lock", *args]) == 0


def test_paths_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    assert herd.main(["paths", "--repo", "acme/widgets", "--skill", "horch"]) == 0
    state = tmp_path / "cache/activeloom-herd/acme__widgets/horch"
    assert state.stat().st_mode & 0o777 == 0o700
