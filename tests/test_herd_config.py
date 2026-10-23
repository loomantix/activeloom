"""Contract for the herd orchestration config helper (`hreview/scripts/herd-config.py`)."""

from __future__ import annotations

import importlib.util
import json
import re
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
    "telemetry-store.json": "telemetry.store: must be one of pr, issue",
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("herd_config", SOURCE / "herd-config.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


herd = load()


def test_getting_started_auto_glance_example_validates(tmp_path: Path) -> None:
    guide = (ROOT / "docs/getting-started.md").read_text()
    section = guide.split("### Choose auto-glance\n", 1)[1]
    example = re.search(r"```json\n(.*?)\n```", section, re.S)
    assert example
    config = json.loads(example.group(1))
    for enabled in (True, False):
        config["glance"]["auto"] = enabled
        path = tmp_path / ".activeloom-herd.json"
        path.write_text(json.dumps(config))
        result = subprocess.run(
            ["python3", "-I", str(SOURCE / "herd-config.py"), "validate", str(path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        effective = json.loads(result.stdout)
        assert effective["glance"]["auto"] is enabled
        assert effective["integration_branch"] == "main"


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
    assert effective["telemetry"] == {"store": None}


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
    argv = ["permissions", "--path", str(path), "--no-alias-check"]
    for name in names:
        argv += ["--engine", name]
    assert herd.main(argv) == 0
    return json.loads(capsys.readouterr().out)["engines"]


def test_engine_without_permission_flags_stops_at_prompts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = permissions(tmp_path, capsys, {}, "codex")
    report["codex"].pop("alias")
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
    assert report["unrecognized"] == ["--search"]


@pytest.mark.parametrize("flag", ["--dangerously-bypass-approvals-and-sandbox", "--yolo"])
def test_codex_bypass_flag_alone_ends_prompts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    report = permissions(tmp_path, capsys, {"codex": {"permission_off_args": [flag]}}, "codex")["codex"]
    assert report["stops_at_prompts"] is False
    assert report["turned_off"] == [{"arg": flag, "turns_off": "approval prompts and the sandbox"}]


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "approval_policy=never"],
        ["-c", 'approval_policy="never"'],
        ["--config=approval_policy='never'"],
    ],
)
def test_codex_config_override_is_classified_by_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    report = permissions(tmp_path, capsys, {"codex": {"permission_off_args": args}}, "codex")["codex"]
    assert report["stops_at_prompts"] is False
    assert [item["turns_off"] for item in report["turned_off"]] == ["approval prompts"]
    assert report["unrecognized"] == []


def test_unknown_codex_config_key_is_unrecognized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    engines = {"codex": {"permission_off_args": ["-c", "model_verbosity=low", "-s", "read-only"]}}
    report = permissions(tmp_path, capsys, engines, "codex")["codex"]
    assert report["unrecognized"] == ["-c model_verbosity=low"]
    assert report["turned_off"] == []


@pytest.mark.parametrize(
    ("engine", "args", "stops"),
    [
        ("codex", ["--approve-for-me"], False),
        ("codex", ["-c", "approvals_reviewer=auto_review"], False),
        ("codex", ["--full-auto"], True),
        ("claude", ["--permission-mode", "auto"], False),
        ("claude", ["--permission-mode", "acceptEdits"], True),
        ("gemini", ["--mode", "accept-edits"], True),
    ],
)
def test_narrower_modes_are_recognized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], engine: str, args: list[str], stops: bool
) -> None:
    report = permissions(tmp_path, capsys, {engine: {"permission_off_args": args}}, engine)[engine]
    assert report["stops_at_prompts"] is stops
    assert report["unrecognized"] == []
    assert len(report["turned_off"]) == 1


def test_value_flag_without_a_value_is_unrecognized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = permissions(tmp_path, capsys, {"codex": {"permission_off_args": ["-s"]}}, "codex")["codex"]
    assert report["unrecognized"] == ["-s"]
    assert report["stops_at_prompts"] is True


def test_valueless_flag_given_a_value_is_unrecognized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = permissions(tmp_path, capsys, {"codex": {"permission_off_args": ["--yolo=1"]}}, "codex")["codex"]
    assert report["unrecognized"] == ["--yolo=1"]
    assert report["stops_at_prompts"] is True


def test_repeated_engine_is_reported_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sorted(permissions(tmp_path, capsys, {}, "codex", "codex", "claude")) == ["claude", "codex"]


def test_report_names_its_scope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "herd.json"
    path.write_text(json.dumps({"schema_version": 1}))
    assert herd.main(["permissions", "--engine", "codex", "--path", str(path), "--no-alias-check"]) == 0
    scope = json.loads(capsys.readouterr().out)["scope"]
    assert scope.startswith("herd.json, plus `type <cli>`")
    assert "Claude settings are not read" in scope


def test_unknown_flag_in_launch_args_is_unrecognized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = permissions(tmp_path, capsys, {"claude": {"launch_args": ["--verbose"]}}, "claude")["claude"]
    assert report["turned_off_in_read_only"] == []
    assert report["unrecognized"] == ["--verbose"]
    assert report["stops_at_prompts"] is True


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


# --- shell aliases -----------------------------------------------------------------


def fake_shell(tmp_path: Path, output: str) -> Path:
    """A shell whose `-ic 'type <cli>'` prints `output`, as bash or zsh would."""
    script = tmp_path / "fake-shell"
    script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{output}\nEOF\n")
    script.chmod(0o755)
    return script


def alias_for(tmp_path: Path, capsys: pytest.CaptureFixture[str], engines: dict[str, Any], output: str) -> Any:
    path = tmp_path / "herd.json"
    path.write_text(json.dumps({"schema_version": 1, "engines": engines}))
    argv = ["permissions", "--path", str(path), "--engine", "codex", "--shell", str(fake_shell(tmp_path, output))]
    assert herd.main(argv) == 0
    return json.loads(capsys.readouterr().out)["engines"]["codex"]["alias"]


@pytest.mark.parametrize(
    "output",
    [
        "codex is aliased to `codex --yolo'",
        "codex is an alias for codex --dangerously-bypass-approvals-and-sandbox",
    ],
)
def test_alias_repeating_a_herd_flag_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str
) -> None:
    engines = {"codex": {"permission_off_args": ["--dangerously-bypass-approvals-and-sandbox"]}}
    alias = alias_for(tmp_path, capsys, engines, output)
    assert alias["kind"] == "alias"
    assert alias["repeats"] == ["--dangerously-bypass-approvals-and-sandbox"]
    assert alias["turned_off"][0]["turns_off"] == "approval prompts and the sandbox"


def test_alias_flag_the_herd_passes_itself_is_a_repeat(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    alias = alias_for(tmp_path, capsys, {}, "codex is aliased to `codex --model gpt-x'")
    assert alias["repeats"] == ["-m"]


def test_alias_that_turns_a_check_off_reaches_read_only_sessions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alias = alias_for(tmp_path, capsys, {}, "codex is aliased to `codex -a never'")
    assert alias["repeats"] == []
    assert alias["turned_off"] == [{"arg": "-a never", "turns_off": "approval prompts"}]


def test_repeatable_codex_config_flag_is_not_a_repeat(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    engines = {"codex": {"launch_args": ["-c", "tui.theme=dark"]}}
    alias = alias_for(tmp_path, capsys, engines, "codex is aliased to `codex -c sandbox_mode=read-only'")
    assert alias["repeats"] == []


def test_no_alias_and_function_are_told_apart(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert alias_for(tmp_path, capsys, {}, "codex is /usr/local/bin/codex")["kind"] == "none"
    assert (
        alias_for(tmp_path, capsys, {}, "codex is a function\ncodex () { command codex --yolo; }")["kind"] == "function"
    )


def test_alias_report_names_flags_but_never_the_alias_body(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = "codex is aliased to `OPENAI_API_KEY=sk-secret codex --yolo --profile=sk-flagvalue -m x'"
    alias = alias_for(tmp_path, capsys, {}, output)
    assert alias["flags"] == ["--profile", "--yolo", "-m"]
    assert "definition" not in alias
    assert "sk-" not in json.dumps(alias)


def test_alias_for_another_command_is_ignored() -> None:
    assert herd.parse_type_output("codex", "codex2 is aliased to `codex --yolo'")["kind"] is None


@pytest.mark.parametrize("output", ["", "sh: type: not found", "codex ist ein Alias von `codex --yolo'"])
def test_output_that_does_not_describe_the_cli_is_not_a_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output: str
) -> None:
    alias = alias_for(tmp_path, capsys, {}, output)
    assert alias["checked"] is False
    assert alias["kind"] is None


def test_alias_check_can_be_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    alias = permissions(tmp_path, capsys, {}, "codex")["codex"]["alias"]
    assert alias["checked"] is False


# --- Claude settings in auto mode ------------------------------------------------------

LAUNCH_FLAGS = {
    "codex": {"permission_off_args": ["--dangerously-bypass-approvals-and-sandbox"]},
    "gemini": {"permission_off_args": ["--dangerously-skip-permissions"]},
}
COMPLETE = {
    "permissions": {"allow": ["Bash(herdr agent read:*)"]},
    "autoMode": {
        "allow": [
            "$defaults",
            "Starting codex or agy sessions with `herdr agent start` using the permission-off flags configured in "
            "herd.json (--dangerously-bypass-approvals-and-sandbox for codex, --dangerously-skip-permissions for agy) "
            "is expected.",
            herd.DIALOG_RULE,
        ]
    },
}


def claude_settings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any] | None,
    *extra: str,
    engines: dict[str, Any] = LAUNCH_FLAGS,
) -> Any:
    config_dir = tmp_path / "claude"
    config_dir.mkdir(exist_ok=True)
    if settings is not None:
        (config_dir / "settings.json").write_text(json.dumps(settings))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    herd_path = tmp_path / "herd.json"
    herd_path.write_text(json.dumps({"schema_version": 1, "engines": engines}))
    argv = ["claude-settings", "--path", str(herd_path), "--draft", str(tmp_path / "draft/settings.json")]
    argv += ["--engine", "codex", "--engine", "gemini", *extra]
    assert herd.main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_settings_with_every_entry_are_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = claude_settings(tmp_path, capsys, monkeypatch, COMPLETE, "--session-mode", "auto")
    assert report["auto_mode"]["in_use"] is True
    assert report["missing"] == []
    assert report["ready"] is True
    assert report["draft"] is None
    assert report["defaults"] == "kept"


@pytest.mark.parametrize("kind", ["deny", "ask"])
@pytest.mark.parametrize("command", ["start", "read", "send-keys"])
def test_restrictive_rules_block_complete_settings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    command: str,
) -> None:
    settings = json.loads(json.dumps(COMPLETE))
    rule = f"Bash(herdr agent {command}:*)"
    settings["permissions"][kind] = [rule]
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto")
    assert report["ready"] is False
    assert report["missing"] == []
    assert report["draft"] is None
    assert report["conflicts"] == [
        {"setting": f"permissions.{kind}", "entry": rule, "blocks": f"herdr agent {command}"}
    ]
    assert json.loads((tmp_path / "claude/settings.json").read_text()) == settings


def test_missing_entries_block_and_get_a_draft(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = {"model": "opus", "permissions": {"allow": ["Bash(git status:*)"]}}
    report = claude_settings(tmp_path, capsys, monkeypatch, existing, "--session-mode", "auto")
    assert report["ready"] is False
    assert [item["setting"] for item in report["missing"]] == [
        "permissions.allow",
        "autoMode.allow",
        "autoMode.allow",
    ]
    draft_path = Path(report["draft"]["path"])
    assert draft_path.stat().st_mode & 0o777 == 0o600
    draft = json.loads(draft_path.read_text())
    assert draft["model"] == "opus"
    assert draft["permissions"]["allow"][0] == "Bash(git status:*)"
    assert draft["autoMode"]["allow"][0] == "$defaults"
    # Launches stay with the classifier: no allow rule that approves any argv.
    assert draft["permissions"]["allow"] == ["Bash(git status:*)", "Bash(herdr agent read:*)"]
    assert draft["autoMode"]["allow"][-1].startswith("While a herd skill")
    # The user's own settings are never written.
    assert json.loads((tmp_path / "claude/settings.json").read_text()) == existing
    # The draft is complete: checking it as the user's settings finds nothing missing.
    rerun = claude_settings(tmp_path, capsys, monkeypatch, draft, "--session-mode", "auto")
    assert rerun["ready"] is True


def test_copy_command_backs_up_validates_and_copies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original: dict[str, Any] = {"permissions": {"allow": []}}
    report = claude_settings(tmp_path, capsys, monkeypatch, original, "--session-mode", "auto")
    target = tmp_path / "claude/settings.json"
    command = report["draft"]["copy_command"]
    assert command.index("cp -p") < command.index("jq -e") < command.rindex(" && cp ")
    assert json.loads(target.read_text()) == original


def test_draft_keeps_a_list_that_replaced_the_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {"autoMode": {"allow": ["Pushing to my fork is fine."]}}
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto")
    assert report["defaults"] == "replaced"
    draft = json.loads(Path(report["draft"]["path"]).read_text())
    assert draft["autoMode"]["allow"][0] == "Pushing to my fork is fine."
    assert "$defaults" not in draft["autoMode"]["allow"]


def test_absent_settings_file_is_drafted_from_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = claude_settings(tmp_path, capsys, monkeypatch, None, "--session-mode", "auto")
    assert report["files"]["user"]["present"] is False
    assert report["draft"]["copy_command"].startswith("mkdir -p ")


def test_not_in_auto_mode_is_ready_but_still_lists_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = claude_settings(tmp_path, capsys, monkeypatch, {}, "--session-mode", "default")
    assert report["auto_mode"]["in_use"] is False
    assert report["ready"] is True
    assert len(report["missing"]) == 3
    assert report["draft"] is None


@pytest.mark.parametrize("args", [["--permission-mode", "auto"], ["--permission-mode=auto"]])
@pytest.mark.parametrize("location", ["launch_args", "permission_off_args"])
def test_claude_panes_in_auto_mode_need_the_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, args: list[str], location: str
) -> None:
    engines = {**LAUNCH_FLAGS, "claude": {location: args}}
    report = claude_settings(tmp_path, capsys, monkeypatch, {}, "--engine", "claude", engines=engines)
    assert report["auto_mode"]["because"] == ["herd.json starts Claude sessions in auto mode"]
    assert report["ready"] is False
    assert report["draft"] is not None


def test_default_mode_in_settings_counts_as_auto_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = claude_settings(tmp_path, capsys, monkeypatch, {"permissions": {"defaultMode": "auto"}})
    assert report["auto_mode"]["in_use"] is True


def test_launch_rule_must_name_every_configured_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = json.loads(json.dumps(COMPLETE))
    stale["autoMode"]["allow"][1] = (
        "Starting codex with `herdr agent start` and --dangerously-bypass-approvals-and-sandbox is fine."
    )
    report = claude_settings(tmp_path, capsys, monkeypatch, stale, "--session-mode", "auto")
    assert [item["setting"] for item in report["missing"]] == ["autoMode.allow"]
    assert "--dangerously-skip-permissions for agy" in report["missing"][0]["entry"]

    stale_value = json.loads(json.dumps(COMPLETE))
    stale_value["autoMode"]["allow"][1] = (
        "Starting claude sessions with `herdr agent start` using --permission-mode auto is expected."
    )
    value_report = claude_settings(
        tmp_path,
        capsys,
        monkeypatch,
        stale_value,
        "--engine",
        "claude",
        "--session-mode",
        "auto",
        engines={"claude": {"permission_off_args": ["--permission-mode", "bypassPermissions"]}},
    )
    assert [item["setting"] for item in value_report["missing"]] == ["autoMode.allow"]
    assert "--permission-mode bypassPermissions for claude" in value_report["missing"][0]["entry"]

    shared_flag = json.loads(json.dumps(COMPLETE))
    shared_flag["autoMode"]["allow"][1] = (
        "Starting agy sessions with `herdr agent start` using --dangerously-skip-permissions for agy is expected."
    )
    shared_report = claude_settings(
        tmp_path,
        capsys,
        monkeypatch,
        shared_flag,
        "--engine",
        "claude",
        "--session-mode",
        "auto",
        engines={
            "claude": {"permission_off_args": ["--dangerously-skip-permissions"]},
            "gemini": {"permission_off_args": ["--dangerously-skip-permissions"]},
        },
    )
    assert [item["setting"] for item in shared_report["missing"]] == ["autoMode.allow"]
    assert "--dangerously-skip-permissions for claude" in shared_report["missing"][0]["entry"]


def test_no_launch_rule_without_permission_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    report = claude_settings(tmp_path, capsys, monkeypatch, {}, "--session-mode", "auto", engines={})
    assert len(report["missing"]) == 2


def test_launches_are_checked_for_restrictions_without_a_launch_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = json.loads(json.dumps(COMPLETE))
    settings["permissions"]["deny"] = ["Bash(herdr agent start:*)"]
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto", engines={})
    assert report["missing"] == []
    assert report["conflicts"] == [
        {"setting": "permissions.deny", "entry": "Bash(herdr agent start:*)", "blocks": "herdr agent start"}
    ]
    assert report["ready"] is False


def test_permission_flags_in_launch_args_need_the_launch_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engines = {"codex": {"launch_args": ["-m", "gpt-x", "-s", "danger-full-access"]}}
    report = claude_settings(tmp_path, capsys, monkeypatch, COMPLETE, "--session-mode", "auto", engines=engines)
    assert [item["setting"] for item in report["missing"]] == ["autoMode.allow"]
    entry = report["missing"][0]["entry"]
    assert "-s danger-full-access for codex" in entry
    assert "gpt-x" not in entry


def test_draft_keeps_defaults_replaced_in_local_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "checkout"
    (project / ".claude").mkdir(parents=True)
    local = {"autoMode": {"allow": ["Pushing to my fork is fine."]}}
    (project / ".claude/settings.local.json").write_text(json.dumps(local))
    report = claude_settings(tmp_path, capsys, monkeypatch, {}, "--session-mode", "auto", "--project-dir", str(project))
    assert report["defaults"] == "replaced"
    draft = json.loads(Path(report["draft"]["path"]).read_text())
    assert "$defaults" not in draft["autoMode"]["allow"]


def test_draft_keeps_defaults_when_user_allow_is_not_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {"autoMode": {"allow": "not a list"}}
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto")
    assert report["defaults"] == "not set"
    draft = json.loads(Path(report["draft"]["path"]).read_text())
    assert draft["autoMode"]["allow"][0] == "$defaults"


def test_project_and_local_settings_count_except_project_auto_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "checkout"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude/settings.json").write_text(json.dumps(COMPLETE))
    (project / ".claude/settings.local.json").write_text(json.dumps({"permissions": {"deny": ["Bash(herdr:*)"]}}))
    report = claude_settings(tmp_path, capsys, monkeypatch, {}, "--session-mode", "auto", "--project-dir", str(project))
    assert {item["setting"] for item in report["missing"]} == {"autoMode.allow"}
    assert report["conflicts"][0] == {
        "setting": "permissions.deny",
        "entry": "Bash(herdr:*)",
        "blocks": "herdr agent start",
    }
    assert report["ready"] is False


@pytest.mark.parametrize(
    ("rule", "covers"),
    [
        ("Bash", True),
        ("Bash(*)", True),
        ("Bash(herdr:*)", True),
        ("Bash(herdr agent *)", True),
        ("Bash(herdr agent start:*)", True),
        ("Bash(herdr agent start)", False),
        ("Bash(*herdr*)", True),
        ("Bash(herdr * *)", True),
        ("Bash(* agent:*)", True),
        ("Bash(herdr agent*read*)", False),
        ("Bash(herdr agent start * forbidden)", False),
        ("Bash(herdr agent star *)", False),
        ("Bash(herdr agent star*)", True),
        ("Bash(herdr agent [s]tart*)", False),
        ("Bash(herdr agent read:*)", False),
        ("Bash(herd:*)", False),
        ("Read(herdr agent start)", False),
    ],
)
def test_permission_rule_coverage(rule: str, covers: bool) -> None:
    assert herd.rule_covers(rule, "herdr agent start") is covers


@pytest.mark.parametrize(
    ("rule", "restricts"),
    [
        ("Bash(herdr agent start:*)", True),
        ("Bash(herdr agent start * --yolo*)", True),
        ("Bash(herdr agent start --kind codex)", True),
        ("Bash(herdr agent st*)", True),
        ("Bash(* --dangerously-bypass-approvals-and-sandbox*)", True),
        ("Bash(herdr agent start)", False),
        ("Bash(herdr agent starts*)", False),
        ("Bash(herdr agent read:*)", False),
        ("Read(herdr agent start *)", False),
    ],
)
def test_restriction_overlap(rule: str, restricts: bool) -> None:
    assert herd.rule_restricts(rule, "herdr agent start") is restricts


@pytest.mark.parametrize("kind", ["deny", "ask"])
def test_partial_restrictions_are_reported_without_blocking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    settings = json.loads(json.dumps(COMPLETE))
    rule = "Bash(herdr agent start * --dangerously-bypass-approvals-and-sandbox*)"
    settings["permissions"][kind] = [rule]
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto")
    assert report["conflicts"] == []
    assert report["partial_conflicts"] == [
        {"setting": f"permissions.{kind}", "entry": rule, "may_block": "herdr agent start"}
    ]
    assert report["ready"] is True


def test_deny_conflict_blocks_outside_auto_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {"permissions": {"deny": ["Bash(herdr agent send-keys:*)"]}}
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "default")
    assert report["auto_mode"]["in_use"] is False
    assert report["ready"] is False
    assert report["draft"] is None


def test_ask_conflict_only_matters_in_auto_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {"permissions": {"ask": ["Bash(herdr agent send-keys:*)"]}}
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "default")
    assert len(report["conflicts"]) == 1
    assert report["ready"] is True


def test_read_only_run_needs_no_launch_entry_for_permission_off_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = json.loads(json.dumps(COMPLETE))
    settings["autoMode"]["allow"] = ["$defaults", herd.DIALOG_RULE]
    editing = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto")
    assert editing["ready"] is False
    report = claude_settings(tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto", "--read-only")
    assert report["missing"] == []
    assert report["ready"] is True


def test_read_only_run_still_names_check_off_flags_in_launch_args(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engines = {"codex": {"launch_args": ["-s", "danger-full-access"], "permission_off_args": ["-a", "never"]}}
    report = claude_settings(
        tmp_path, capsys, monkeypatch, {}, "--session-mode", "auto", "--read-only", engines=engines
    )
    launch = report["missing"][-1]["entry"]
    assert "-s danger-full-access for codex" in launch
    assert "-a never" not in launch


def test_read_only_claude_sessions_ignore_auto_mode_in_permission_off_args(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engines = {**LAUNCH_FLAGS, "claude": {"permission_off_args": ["--permission-mode", "auto"]}}
    report = claude_settings(tmp_path, capsys, monkeypatch, {}, "--engine", "claude", "--read-only", engines=engines)
    assert report["auto_mode"]["in_use"] is False


def test_invalid_settings_json_is_a_config_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text("{not json")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    herd_path = tmp_path / "herd.json"
    herd_path.write_text(json.dumps({"schema_version": 1}))
    assert herd.main(["claude-settings", "--path", str(herd_path), "--engine", "codex"]) == 1


@pytest.mark.parametrize(
    ("engine", "cli", "flag", "value"),
    [
        ("codex", "codex", "-s", "workspace-write"),
        ("claude", "claude", "--permission-mode", "auto"),
        ("gemini", "agy", "--mode", "accept-edits"),
    ],
)
def test_alias_mode_flag_only_repeats_in_read_only_launch(
    engine: str, cli: str, flag: str, value: str
) -> None:
    alias = herd.alias_report(
        engine,
        {"launch_args": [], "permission_off_args": []},
        f"{cli} is an alias for {cli} {flag} {value}",
    )
    assert alias["repeats"] == []
    assert alias["read_only_repeats"] == [flag]


def test_alias_permission_off_flag_is_not_repeated_in_read_only_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alias = alias_for(
        tmp_path,
        capsys,
        {"codex": {"permission_off_args": ["-a", "never"]}},
        "codex is an alias for codex -a never",
    )
    assert alias["repeats"] == ["-a"]
    assert alias["read_only_repeats"] == []


def test_alias_launch_flag_repeats_in_both_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    alias = alias_for(
        tmp_path,
        capsys,
        {"codex": {"launch_args": ["-a", "never"]}},
        "codex is an alias for codex --ask-for-approval never",
    )
    assert alias["repeats"] == alias["read_only_repeats"] == ["-a"]


@pytest.mark.parametrize("kind", ["deny", "ask"])
@pytest.mark.parametrize(
    "rule", ["Bash(*herdr*)", "Bash(herdr * *)", "Bash(* agent:*)"]
)
def test_wildcard_restrictions_block_complete_settings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    rule: str,
) -> None:
    settings = json.loads(json.dumps(COMPLETE))
    settings["permissions"][kind] = [rule]
    report = claude_settings(
        tmp_path, capsys, monkeypatch, settings, "--session-mode", "auto"
    )
    assert report["ready"] is False
    assert report["missing"] == []
    assert {item["blocks"] for item in report["conflicts"]} == {
        "herdr agent start",
        "herdr agent read",
        "herdr agent send-keys",
    }
    assert json.loads((tmp_path / "claude/settings.json").read_text()) == settings
