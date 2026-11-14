"""Full-auto restarts and scope splits in horch, hreview and hloop (#580).

Offline: the split record helper in herd-config.py and the rendered skill contracts.
Policy authentication and the GitHub inventory are stubbed; sudo is never invoked.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO, PR, HEAD = "acme/widgets", 42, "a" * 40
RUN, DIGEST = "c" * 64, "d" * 64
ROOTS = (".claude", ".codex", ".agents")
pytestmark = pytest.mark.usefixtures("offline_github")


def load() -> ModuleType:
    path = ROOT / "prompts/skills/hreview/scripts/herd-config.py"
    spec = importlib.util.spec_from_file_location("herd_config_full_auto", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


herd = load()
REAL_LIVE_PR_SCOPE = herd._live_pr_scope


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config"


LIVE: dict[int, tuple[str, list[str]]] = {}


@pytest.fixture(autouse=True)
def live_pr(monkeypatch: pytest.MonkeyPatch) -> dict[int, tuple[str, list[str]]]:
    """The GitHub view of each PR that split-plan checks a new plan against."""
    LIVE.clear()
    LIVE[PR] = (HEAD, sorted(plan()["paths"]))

    def scope(repo: str, pr: int) -> tuple[str, list[str]]:
        if pr not in LIVE:
            raise herd.SplitError(f"cannot read {repo}#{pr} from GitHub")
        return LIVE[pr]

    monkeypatch.setattr(herd, "_live_pr_scope", scope)
    return LIVE


def plan(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "paths": ["src/a.py", "src/b.py", "tests/test_a.py", "tests/test_b.py"],
        "partitions": [
            {"id": "alpha", "paths": ["src/a.py", "tests/test_a.py"]},
            {"id": "beta", "paths": ["src/b.py", "tests/test_b.py"]},
        ],
    }
    value.update(overrides)
    return value


# --- split records -----------------------------------------------------------------


def test_plan_is_private_and_accounts_for_every_original_path(config_home: Path) -> None:
    result = herd.split_plan(REPO, PR, HEAD, plan())
    path = Path(result["path"])
    assert path == config_home / "activeloom/herd-splits/acme__widgets/pr-42.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert result["resumed"] is False
    assert result["needs_branch"] == result["needs_pr"] == ["alpha", "beta"]
    assert result["accounted"] is False and result["superseded"] is None
    stored = json.loads(path.read_text())
    assert [p["id"] for p in stored["partitions"]] == ["alpha", "beta"]
    assert stored["head"] == HEAD


@pytest.mark.parametrize(
    "partitions, message",
    [
        ([{"id": "alpha", "paths": ["src/a.py", "tests/test_a.py"]}], "at least two partitions"),
        (
            [{"id": "alpha", "paths": ["src/a.py", "tests/test_a.py"]}, {"id": "beta", "paths": ["src/b.py"]}],
            "not accounted for: tests/test_b.py",
        ),
        (
            [
                {"id": "alpha", "paths": ["src/a.py", "tests/test_a.py", "src/b.py"]},
                {"id": "beta", "paths": ["src/b.py", "tests/test_b.py"]},
            ],
            "already in partition 'alpha'",
        ),
        (
            [
                {"id": "alpha", "paths": ["src/a.py", "tests/test_a.py", "src/c.py"]},
                {"id": "beta", "paths": ["src/b.py", "tests/test_b.py"]},
            ],
            "not in the original PR",
        ),
        (
            [
                {"id": "alpha", "paths": ["src/a.py", "tests/test_a.py"]},
                {"id": "alpha", "paths": ["src/b.py", "tests/test_b.py"]},
            ],
            "duplicate partition id",
        ),
        (
            [
                {"id": "Alpha One", "paths": ["src/a.py", "tests/test_a.py"]},
                {"id": "beta", "paths": ["src/b.py", "tests/test_b.py"]},
            ],
            "short lowercase slug",
        ),
    ],
)
def test_plan_that_loses_or_invents_scope_writes_nothing(partitions: list[Any], message: str) -> None:
    with pytest.raises(herd.SplitError, match=message):
        herd.split_plan(REPO, PR, HEAD, plan(partitions=partitions))
    assert not herd._split_target(REPO, PR).exists()


def test_interrupted_split_resumes_without_duplicate_prs() -> None:
    herd.split_plan(REPO, PR, HEAD, plan())
    herd.split_record(REPO, PR, HEAD, "alpha", "feat/alpha-42", None)
    herd.split_record(REPO, PR, HEAD, "alpha", None, 50)
    herd.split_record(REPO, PR, HEAD, "beta", "feat/beta-42", None)
    # The orchestrator restarts here: an identical replan resumes the same record.
    resumed = herd.split_plan(REPO, PR, HEAD, plan())
    assert resumed["resumed"] is True
    assert resumed["needs_branch"] == [] and resumed["needs_pr"] == ["beta"]
    # Repeating a step already recorded is idempotent; a different value is refused.
    herd.split_record(REPO, PR, HEAD, "alpha", "feat/alpha-42", 50)
    with pytest.raises(herd.SplitError, match="never duplicate"):
        herd.split_record(REPO, PR, HEAD, "alpha", None, 51)
    with pytest.raises(herd.SplitError, match="never duplicate"):
        herd.split_record(REPO, PR, HEAD, "beta", "feat/beta-again", None)
    with pytest.raises(herd.SplitError, match="another partition"):
        herd.split_record(REPO, PR, HEAD, "beta", None, 50)
    assert herd.split_status(REPO, PR, HEAD)["partitions"][1]["replacement_pr"] is None


def test_a_different_plan_for_the_same_pr_is_refused() -> None:
    path = Path(herd.split_plan(REPO, PR, HEAD, plan())["path"])
    before = path.read_bytes()
    with pytest.raises(herd.SplitError, match="original head changed") as moved:
        herd.split_plan(REPO, PR, "b" * 40, plan())
    assert moved.value.reason == "head-changed"
    other = plan(partitions=[
        {"id": "alpha", "paths": ["src/a.py"]},
        {"id": "beta", "paths": ["src/b.py", "tests/test_a.py", "tests/test_b.py"]},
    ])
    with pytest.raises(herd.SplitError, match="resume the recorded plan") as different:
        herd.split_plan(REPO, PR, HEAD, other)
    assert different.value.reason == "refused"
    assert path.read_bytes() == before


def test_a_new_plan_must_match_the_live_pr(live_pr: dict[int, tuple[str, list[str]]]) -> None:
    live_pr[PR] = (HEAD, sorted([*plan()["paths"], "src/c.py"]))
    with pytest.raises(herd.SplitError, match=r"missing \['src/c.py'\]") as dropped:
        herd.split_plan(REPO, PR, HEAD, plan())
    assert dropped.value.reason == "scope-mismatch"
    live_pr[PR] = ("b" * 40, sorted(plan()["paths"]))
    with pytest.raises(herd.SplitError, match="on GitHub") as moved:
        herd.split_plan(REPO, PR, HEAD, plan())
    assert moved.value.reason == "scope-mismatch"
    del live_pr[PR]
    with pytest.raises(herd.SplitError, match="from GitHub"):
        herd.split_plan(REPO, PR, HEAD, plan())
    assert not herd._split_target(REPO, PR).exists()


def test_split_head_must_be_an_exact_sha() -> None:
    with pytest.raises(herd.UsageError):
        herd.split_plan(REPO, PR, HEAD + "\n", plan())
    assert not herd._split_target(REPO, PR).exists()


def test_refusals_carry_the_reason_the_skills_route_on() -> None:
    with pytest.raises(herd.SplitError) as missing:
        herd.split_status(REPO, PR, HEAD)
    assert missing.value.reason == "no-record"
    herd.split_plan(REPO, PR, HEAD, plan())
    herd.split_record(REPO, PR, HEAD, "alpha", "feat/alpha-42", 50)
    with pytest.raises(herd.SplitError) as recorded:
        herd.split_record(REPO, PR, HEAD, "alpha", "feat/other", None)
    assert recorded.value.reason == "recorded"
    with pytest.raises(herd.SplitError) as replacement:
        herd.split_plan(REPO, 50, "b" * 40, plan())
    assert replacement.value.reason == "replacement"


def test_malformed_sibling_record_is_a_refusal_not_a_traceback(config_home: Path) -> None:
    directory = config_home / "activeloom/herd-splits/acme__widgets"
    directory.mkdir(parents=True, mode=0o700)
    sibling = directory / "pr-9.json"
    sibling.write_text(json.dumps({"version": 1}))
    sibling.chmod(0o600)
    with pytest.raises(herd.SplitError, match="malformed split record"):
        herd.split_plan(REPO, PR, HEAD, plan())
    assert not herd._split_target(REPO, PR).exists()


def test_unsafe_split_lock_is_a_split_refusal(config_home: Path, capsys: pytest.CaptureFixture[str],
                                              tmp_path: Path) -> None:
    herd.split_plan(REPO, PR, HEAD, plan())
    lock = herd._split_target(REPO, PR).with_suffix(".lock")
    lock.chmod(0o666)
    base = ["--repo", REPO, "--pr", str(PR), "--head", HEAD]
    assert herd.main(["split-record", *base, "--partition", "alpha", "--branch", "feat/a"]) == 5
    assert "unsafe lock" in capsys.readouterr().err


def test_a_split_pr_never_gets_a_policy_restart(authority: Any) -> None:
    authority[1]["budget_used"] = 0
    herd.split_plan(REPO, PR, HEAD, plan())
    with pytest.raises(herd.ApprovalError, match="scope split record"):
        herd.record_policy_restart(REPO, PR, HEAD, "keep")
    assert not herd._approval_target(REPO, PR, HEAD).exists()


def test_replacement_pr_needs_its_branch_first() -> None:
    herd.split_plan(REPO, PR, HEAD, plan())
    with pytest.raises(herd.SplitError, match="branch before"):
        herd.split_record(REPO, PR, HEAD, "alpha", None, 50)
    with pytest.raises(herd.UsageError):
        herd.split_record(REPO, PR, HEAD, "alpha", "feat/a", PR)


def test_a_replacement_partition_is_never_split_again() -> None:
    herd.split_plan(REPO, PR, HEAD, plan())
    herd.split_record(REPO, PR, HEAD, "alpha", "feat/alpha-42", 50)
    with pytest.raises(herd.SplitError, match="never split it again"):
        herd.split_plan(REPO, 50, "b" * 40, plan())
    assert not herd._split_target(REPO, 50).exists()


def test_original_is_superseded_only_after_all_scope_is_accounted() -> None:
    herd.split_plan(REPO, PR, HEAD, plan())
    herd.split_record(REPO, PR, HEAD, "alpha", "feat/alpha-42", 50)
    with pytest.raises(herd.SplitError, match="partitions beta have no replacement PR"):
        herd.split_supersede(REPO, PR, HEAD)
    assert herd.split_status(REPO, PR, HEAD)["superseded"] is None
    herd.split_record(REPO, PR, HEAD, "beta", "feat/beta-42", 51)
    done = herd.split_supersede(REPO, PR, HEAD)
    assert done["accounted"] is True and done["superseded"]
    assert herd.split_supersede(REPO, PR, HEAD)["superseded"] == done["superseded"]
    with pytest.raises(herd.SplitError, match="already marked superseded"):
        herd.split_record(REPO, PR, HEAD, "beta", "feat/beta-42", 52)


def test_unsafe_split_record_is_refused() -> None:
    result = herd.split_plan(REPO, PR, HEAD, plan())
    os.chmod(result["path"], 0o666)
    with pytest.raises(herd.SplitError, match="owned and writable only"):
        herd.split_status(REPO, PR, HEAD)


@pytest.mark.parametrize("operation", ["status", "record", "supersede"])
def test_original_head_drift_refuses_recovery_without_changing_the_plan(operation: str) -> None:
    result = herd.split_plan(REPO, PR, HEAD, plan())
    herd.split_record(REPO, PR, HEAD, "alpha", "feat/alpha-42", 50)
    herd.split_record(REPO, PR, HEAD, "beta", "feat/beta-42", 51)
    path = Path(result["path"])
    before = path.read_bytes()
    changed_head = "b" * 40
    with pytest.raises(herd.SplitError, match="original head changed"):
        if operation == "record":
            herd.split_record(REPO, PR, changed_head, "alpha", "feat/alpha-42", 50)
        elif operation == "supersede":
            herd.split_supersede(REPO, PR, changed_head)
        else:
            herd.split_status(REPO, PR, changed_head)
    assert path.read_bytes() == before
    assert herd.split_status(REPO, PR, HEAD)["superseded"] is None


def test_cli_reports_refusals_with_exit_5(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan()))
    base = ["--repo", REPO, "--pr", str(PR), "--head", HEAD]
    assert herd.main(["split-status", *base]) == 5
    assert "split refused [no-record]: no split record" in capsys.readouterr().err
    assert herd.main(["split-plan", *base, "--plan-file", str(plan_file)]) == 0
    assert json.loads(capsys.readouterr().out)["needs_pr"] == ["alpha", "beta"]
    assert herd.main(["split-record", *base, "--partition", "alpha", "--branch", "feat/a", "--replacement-pr", "50"]) == 0
    capsys.readouterr()
    assert herd.main(["split-supersede", *base]) == 5
    assert "not accounted for" in capsys.readouterr().err
    for command in ("split-status", "split-record", "split-supersede"):
        extra = ["--partition", "alpha", "--branch", "feat/a"] if command == "split-record" else []
        assert herd.main([command, "--repo", REPO, "--pr", str(PR), "--head", "b" * 40, *extra]) == 5
        assert "split refused [head-changed]: original head changed" in capsys.readouterr().err


# --- policy restart guards -----------------------------------------------------------


@pytest.fixture
def authority(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    settings = {"protected": True, "mode": "full", "launch_blocked": False,
                "reason": "fixture", "policy_sha256": DIGEST, "stops": [], "restart_budget": 2}
    inventory = {"state": "ended", "latest": {"run_id": RUN}, "end": {"outcome": "converged", "head": HEAD},
                 "budget_used": 2}
    monkeypatch.setattr(herd, "_policy_report", lambda: settings)
    monkeypatch.setattr(herd, "_restart_inventory", lambda *a: inventory)
    return settings, inventory


def test_spent_budget_names_the_count_and_both_ways_forward(authority: Any) -> None:
    with pytest.raises(herd.ApprovalError) as refused:
        herd.record_policy_restart(REPO, PR, HEAD, "keep")
    message = str(refused.value)
    assert f"{REPO}#{PR}: 2/2 used" in message
    assert "approve-restart" in message and "horch" in message and "restart_budget" in message
    assert not herd._approval_target(REPO, PR, HEAD).exists()


def test_split_never_records_a_restart_for_the_original(authority: Any) -> None:
    authority[1]["budget_used"] = 0
    with pytest.raises(herd.UsageError, match="split must finish"):
        herd.record_policy_restart(REPO, PR, HEAD, "split")
    assert not herd._approval_target(REPO, PR, HEAD).exists()


# --- rendered skill contracts ---------------------------------------------------------


def skill(root: str, name: str) -> str:
    return (ROOT / root / "skills" / name / "SKILL.md").read_text()


@pytest.mark.parametrize("root", ROOTS)
def test_hreview_policy_restart_follows_user_routes_and_guards_first(root: str) -> None:
    ended = skill(root, "hreview").split("- **Ended**", 1)[1].split("10. Roster", 1)[0]
    order = ["`--restart` typed by the user", "check-restart", "**Policy restart**", "**Otherwise**"]
    assert [ended.index(item) for item in order] == sorted(ended.index(item) for item in order)
    policy = ended.split("**Policy restart**", 1)[1].split("**Otherwise**", 1)[0]
    for guard in ("`protected` true", "`mode` `full`", "`launch_blocked` false", "`network.mode: open`",
                  "`restart` is not in `stops`", "`status: verified`", "`restart-after-exhausted`",
                  "head other than the exhausted one", "failed guard"):
        assert guard in policy, guard
    # Scope checkpoint: assessed, never a default keep, and a stop is honoured.
    assert "With `scope-decision` in `stops`, stop" in policy
    assert "cause" in policy and "coupling" in policy and "separability" in policy
    assert "Never pick `keep` without the assessment" in policy
    assert "never split again" in policy
    # Split never starts a run on the original; standalone hreview hands off to horch.
    split = policy.split("**`split`:**", 1)[1].split("3. **`keep`", 1)[0]
    assert "do not call `record-policy-restart` or `start-run` on this PR" in split
    assert "split-plan --repo $repo --pr <n> --head <head> --plan-file <file>" in split
    assert "Never start build sessions from this tab" in split
    assert "SPLIT PLANNED" in policy and "horch" in policy.split("SPLIT PLANNED", 1)[1]
    # Keep: the herder mints, then claims through the restart transaction; no Deep for restarting.
    keep = policy.split("3. **`keep`", 1)[1]
    assert keep.index("record-policy-restart") < keep.index("proceed as for a recorded approval")
    assert "never Deep trigger 6 by itself" in keep
    spent = policy.split("AUTOMATIC RESTART BUDGET SPENT", 1)[1]
    assert "<used>/<limit>" in spent and "approve-restart --repo <repo> --pr <n>" in spent
    assert "--restart-budget" in spent and "horch" in spent
    assert "autonomy policy restart <id>" in ended


@pytest.mark.parametrize("root", ROOTS)
def test_hreview_refuses_stale_relays(root: str) -> None:
    text = skill(root, "hreview")
    assert "refuse a stale or mismatched relay" in text
    assert "A relay is never a restart" in text


@pytest.mark.parametrize("root", ROOTS)
def test_horch_policy_restart_split_and_relay(root: str) -> None:
    text = skill(root, "horch")
    chain = text.split("**Review chain**", 1)[1].split("**Build**", 1)[0]
    order = ["**Recorded approval:**", "**Policy restart**", "**None**"]
    assert [chain.index(item) for item in order] == sorted(chain.index(item) for item in order)
    policy = chain.split("**Policy restart**", 1)[1].split("**None**", 1)[0]
    assert "record-policy-restart --repo $repo --pr <n> --head <head>" in policy
    assert "never default to `keep`" in policy
    assert "`state.md`" in policy and "<used>/<limit>" in policy
    assert "start no herder on this PR" in policy
    split = chain.split("**Scope split**", 2)[2]
    assert "Never call `start-run` on the original PR" in split
    assert "never rewrite its branch or history" in split
    for command in ("split-status", "split-plan", "split-record", "split-supersede"):
        assert command in split, command
    assert "pass it as `--head <head>`" in split
    assert "recheck before each dispatch" in split.lower()
    assert "stop and alert for scope reconciliation" in split
    assert "record's immutable `head`" in split
    assert "Never open a second PR for a partition" in split
    assert "its own first run" in split
    assert "Never mark it superseded earlier" in split
    assert "never split it again" in split
    full = text.split("## Full auto", 1)[1].split("## Holding", 1)[0]
    assert "`protected` true, `mode` `full` and `launch_blocked` false" in full
    assert "Never run its setter" in full and "--restart-budget <k>" in full
    for kept in ("ownership proof", "drift", "assignment", "locks", "exclusions", "round caps", "human-glance"):
        assert kept in full, kept
    relay = text.split("**Relaying an owner decision.**", 1)[1]
    for field in ("receiver", "target issue or PR", "head SHA or plan", "expiry or single use"):
        assert field in relay, field
    assert "stale or mismatched relay is refused" in relay
    assert "disclosed trusted-session convention" in relay
    assert "A relay is never a restart approval" in relay
    assert "send it `--restart`" in text


@pytest.mark.parametrize("root", ROOTS)
def test_build_questions_route_without_widening_scope(root: str) -> None:
    horch, hloop = skill(root, "horch"), skill(root, "hloop")
    question = horch.split("**`BUILD-QUESTION`**", 1)[1].split("\n", 1)[0]
    assert "already authorized grill chains" in question and "never adds the issue to an allowlist" in question
    assert "grill chains are already authorized for that issue" in hloop
    assert "budgeted policy restart" in hloop and "**Scope split**" in hloop


@pytest.mark.parametrize(
    "path",
    [
        ".claude/REVIEW_WORKFLOW.md",
        ".codex/REVIEW_WORKFLOW.md",
        ".agents/REVIEW_WORKFLOW.md",
        "packages/review-ledger/protocol/local-review-ledger.md",
    ],
)
def test_canonical_restart_authorization_names_the_policy_route(path: str) -> None:
    text = " ".join((ROOT / path).read_text().split())
    assert "policy-sourced restart record" in text
    assert "restart approval the user recorded" in text


@pytest.mark.parametrize("root", ROOTS)
def test_split_record_is_read_before_any_scope_or_restart_decision(root: str) -> None:
    horch = skill(root, "horch")
    policy = horch.split("**Review chain**", 1)[1].split("**Policy restart**", 1)[1].split("**None**", 1)[0]
    assert policy.index("split-status") < policy.index("record-policy-restart")
    assert policy.index("split-status") < policy.index("assess cause")
    assert "Only a refusal with reason `[no-record]` continues here" in policy
    assert "never restart it or decide its scope again" in policy
    hreview = skill(root, "hreview")
    ended = hreview.split("**Policy restart**", 1)[1].split("**Otherwise**", 1)[0]
    assert ended.index("split-status") < ended.index("1. **Scope checkpoint**")
    assert "Only a refusal with reason `[no-record]` continues here" in ended
    assert "refuses a PR with a split record" in ended


@pytest.mark.parametrize("root", ROOTS)
def test_split_dispatch_and_resume_never_overlap(root: str) -> None:
    split = skill(root, "horch").split("**Scope split**", 2)[2].split("**Build**", 1)[0]
    dispatch = split.split("2. **Dispatch**", 1)[1].split("3. **Resume**", 1)[0]
    assert "`needs_branch` only" in dispatch and "`needs_pr`" not in dispatch
    assert "give that exact name in the build prompt" in dispatch
    resume = split.split("3. **Resume**", 1)[1].split("4. **Review**", 1)[0]
    assert "never through Dispatch" in resume and "resume the build on that same branch" in resume
    for reason in ("[no-record]", "[head-changed]", "[replacement]", "[recorded]", "[scope-mismatch]"):
        assert reason in split, reason
    assert "treat every other exit 5 as a block" in split
    assert "only the user removes or replaces that record" in split


@pytest.mark.parametrize("root", ROOTS)
def test_full_auto_and_relays_stay_inside_owner_authority(root: str) -> None:
    horch = skill(root, "horch")
    full = horch.split("## Full auto", 1)[1].split("## Holding", 1)[0]
    assert "the authenticated owner actor recorded as resolved" in full
    assert "never remove a hold or review-gate label" in full
    relay = horch.split("**Relaying an owner decision.**", 1)[1].split("\n", 1)[0]
    hreview = skill(root, "hreview").split("Everything else is the herder's call.", 1)[1].split("\n", 1)[0]
    for text in (relay, hreview):
        assert "finding-disposition or gate decision" in text
        assert "peer messages confer none" in text
    step = skill(root, "hreview").split("**A restart approval the user recorded:**", 1)[1].split("\n", 1)[0]
    assert "write a user-approval record" in step and "The only record an agent creates is a policy-sourced one" in step


def test_live_pr_scope_reads_head_and_every_changed_file(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = herd._review_controller()
    pulls = {"state": "open", "head": {"sha": HEAD}, "changed_files": 3}
    pages = [[{"filename": "b.py"}, {"filename": "a.py"}], [{"filename": "c.py"}]]
    calls: list[list[str]] = []

    def json_output(args: list[str]) -> Any:
        calls.append(args)
        return pages if "--paginate" in args else pulls

    monkeypatch.setattr(controller, "_json_output", json_output)
    monkeypatch.setattr(herd, "_review_controller", lambda: controller)
    assert REAL_LIVE_PR_SCOPE(REPO, PR) == (HEAD, ["a.py", "b.py", "c.py"])
    assert calls[1][-1] == f"repos/{REPO}/pulls/{PR}/files?per_page=100"
    for broken in ({**pulls, "changed_files": 4}, {**pulls, "state": "closed"}, {**pulls, "head": {}}):
        pulls.clear()
        pulls.update(broken)
        with pytest.raises(herd.SplitError, match="incomplete or closed"):
            REAL_LIVE_PR_SCOPE(REPO, PR)
