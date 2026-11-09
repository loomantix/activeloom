"""Offline policy authority, installation and inventory cases; never invoke sudo."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO, PR, HEAD = "acme/widgets", 7, "a" * 40
RUN, DIGEST = "c" * 64, "d" * 64
pytestmark = pytest.mark.usefixtures("offline_github")


def load(path: str) -> Any:
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT / path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("prompts/skills/hreview/scripts/autonomy-policy.py")
herd = load("prompts/skills/hreview/scripts/herd-config.py")
controller = load("packages/review-ledger/protocol/local-review-handoff.py")


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "sudoers.d").mkdir()
    monkeypatch.setattr(policy, "ETC", etc)
    monkeypatch.setattr(policy, "POLICY", etc / "activeloom/policy.json")
    monkeypatch.setattr(policy, "SETTER", etc / "activeloom/activeloom-policy")
    monkeypatch.setattr(policy, "DROP_IN", etc / "sudoers.d/activeloom-policy")
    return etc


def write_install() -> None:
    policy.POLICY.parent.mkdir()
    policy.POLICY.write_text(json.dumps(policy.DEFAULT))
    policy.SETTER.write_text("test fixture")
    policy.DROP_IN.write_text(policy.sudoers_text())


def test_no_install_is_manual_unenforced_and_read_only(installation: Path) -> None:
    before = list(installation.rglob("*"))
    report = policy.report()
    assert report["mode"] == "manual" and not report["installed"]
    assert report["network"]["mode"] == "unenforced" and not report["launch_blocked"]
    assert list(installation.rglob("*")) == before


def test_unreadable_install_is_not_treated_as_absent(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(path: Path) -> Any:
        raise PermissionError("cannot inspect installation")
    monkeypatch.setattr(Path, "lstat", denied)
    report = policy.report()
    assert report["installed"] and report["launch_blocked"]
    assert report["network"]["mode"] == "restricted"


def test_setter_refuses_non_root_without_writing(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy.os, "geteuid", lambda: 1000)
    assert policy.main(["install"]) == 1
    assert not policy.POLICY.parent.exists()
    assert policy.main(["--check"]) == 0
    assert policy.main(["--check", "install"]) == 1


def test_system_link_through_unprotected_directory_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def protected(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == Path("/home/untrusted"):
            raise policy.PolicyError("unprotected directory")
    monkeypatch.setattr(policy, "protected", protected)
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0))
    monkeypatch.setattr(policy.os, "readlink", lambda path: "/home/untrusted/link")
    with pytest.raises(policy.PolicyError, match="unprotected directory"):
        policy.protected_program(Path("/usr/bin/program"))


@pytest.mark.parametrize("artifact", ["directory", "setter", "drop-in"])
def test_partial_install_never_silently_drops_restrictions(installation: Path, artifact: str) -> None:
    if artifact in ("directory", "setter"):
        policy.POLICY.parent.mkdir()
    if artifact == "setter":
        policy.SETTER.write_text("fixture")
    elif artifact == "drop-in":
        policy.DROP_IN.write_text("fixture")
    report = policy.report()
    assert report["installed"] and report["launch_blocked"] and not report["protected"]
    assert report["network"]["mode"] == "restricted"


@pytest.mark.parametrize("field,value", [
    ("mode", "unknown"), ("stops", ["deep-escalation"]), ("stops", ["restart", "restart"]),
    ("stops", [4]), ("restart_budget", True), ("restart_budget", -1), ("unknown", 3),
])
def test_invalid_autonomy(field: str, value: Any) -> None:
    config = copy.deepcopy(policy.DEFAULT)
    config["autonomy"][field] = value
    with pytest.raises(policy.PolicyError):
        policy.validate(config)


@pytest.mark.parametrize("allow", [["https://example.org"], ["*.example.org"], ["foo..bar"], ["-foo"], ["foo-"], ["A.org"], ["a", "a"], [1]])
def test_allowlist_is_data_not_command_or_url(allow: list[Any]) -> None:
    config = copy.deepcopy(policy.DEFAULT)
    config["network"]["allow"] = allow
    with pytest.raises(policy.PolicyError):
        policy.validate(config)


def test_defaults_and_fixed_stops() -> None:
    result = policy.validate({"autonomy": {"mode": "full", "stops": list(policy.STOPS)}, "network": {"mode": "restricted"}})
    assert result["autonomy"]["restart_budget"] == 2
    assert result["network"]["allow"] == []


@pytest.mark.parametrize("where,mutation", [("file", "owner"), ("parent", "owner"), ("parent", "writable"), ("file", "symlink"), ("parent", "symlink"), ("file", "mode")])
def test_protected_paths_reject_unsafe_components(monkeypatch: pytest.MonkeyPatch, where: str, mutation: str) -> None:
    path = Path("/etc/activeloom/policy.json")
    def fake_stat(item: Path) -> Any:
        mode = stat.S_IFREG | 0o644 if item == path else stat.S_IFDIR | 0o755
        uid = 0
        if item == (path if where == "file" else path.parent):
            if mutation == "owner":
                uid = 1000
            elif mutation == "writable":
                mode |= 0o020
            elif mutation == "symlink":
                mode = stat.S_IFLNK | 0o777
            elif mutation == "mode":
                mode = stat.S_IFREG | 0o600
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=0)
    monkeypatch.setattr(Path, "lstat", fake_stat)
    with pytest.raises(policy.PolicyError):
        policy.protected(path, 0o644)


@pytest.mark.parametrize("listing,probe,expected", [
    ((0, "exact", ""), (1, "", "sudo: a password is required\n"), True),
    ((0, "exact", ""), (0, "", ""), False),
    ((1, "", "sudo: a password is required"), (1, "", "sudo: a password is required"), False),
    ((0, "different", ""), (1, "", "sudo: a password is required"), False),
    ((0, "exact", ""), (1, "", "sudo: command not allowed"), False),
    ((0, "exact", ""), (127, "", "not found"), False),
    ((0, "exact", ""), (1, "", "sudo: syntax error\nsudo: a password is required"), False),
    ((0, "exact", ""), (1, "", "sudo: authentication is required"), False),
    # sudo-rs 0.2.x: `sudo -n` without a usable credential.
    ((0, "exact", ""), (1, "", "sudo: interactive authentication is required\n"), True),
    ((1, "", "sudo: interactive authentication is required"), (1, "", "sudo: interactive authentication is required"), False),
    ((0, "exact", ""), (1, "", "sudo-rs: interactive authentication is required"), False),
    ((0, "exact", ""), (1, "", "sudo: Sorry, user fixture may not run /etc/activeloom/activeloom-policy on host."), False),
    ((0, "exact", ""), (1, "", "sudo: A terminal is required to authenticate"), False),
])
def test_authentication_requires_positive_permission_and_exact_diagnostic(monkeypatch: pytest.MonkeyPatch, listing: Any, probe: Any, expected: bool) -> None:
    monkeypatch.setattr(policy, "protected", lambda *a, **kw: None)
    results = [listing, probe, (0, "User fixture may run the following commands on host:\n    (ALL : ALL) ALL\n", "")]
    def run(argv: list[str]) -> Any:
        status, out, err = results.pop(0)
        return subprocess.CompletedProcess(argv, status, f"{policy.SETTER} --check\n" if out == "exact" else out, err)
    monkeypatch.setattr(policy, "_run", run)
    assert policy.authentication_status()[0] is expected


@pytest.mark.parametrize("entry", ["(root) NOPASSWD: /etc/activeloom/activeloom-policy set *", "(root) NOPASSWD: /unrelated", "(root) ALL\nDefaults !authenticate", "unrecognized listing"])
def test_passwordless_set_rule_cannot_hide_behind_authenticated_check(monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    monkeypatch.setattr(policy, "protected_program", lambda *args: None)
    results = [
        (0, f"{policy.SETTER} --check\n", ""),
        (1, "", "sudo: a password is required\n"),
        (0, f"User fixture may run the following commands on host:\n    {entry}\n", ""),
    ]
    def run(argv: list[str]) -> Any:
        status, out, err = results.pop(0)
        return subprocess.CompletedProcess(argv, status, out, err)
    monkeypatch.setattr(policy, "_run", run)
    assert policy.authentication_status()[0] is False


@pytest.mark.parametrize("failure", [FileNotFoundError("sudo missing"), subprocess.TimeoutExpired("sudo", 15)])
def test_missing_or_hung_probe_disables_authority(installation: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    write_install()
    monkeypatch.setattr(policy, "protected", lambda *a, **kw: None)
    def fail() -> None:
        raise failure
    monkeypatch.setattr(policy, "authentication_status", fail)
    assert policy.report()["launch_blocked"]


def test_restricted_unavailable_and_explicit_open_gate(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_install()
    monkeypatch.setattr(policy, "protected", lambda *a, **kw: None)
    monkeypatch.setattr(policy, "authentication_status", lambda: (True, "authenticated"))
    restricted = policy.report()
    assert restricted["protected"] and restricted["mode"] == "manual" and restricted["launch_blocked"]
    assert restricted["containment"]["status"] == "unavailable"
    value = copy.deepcopy(policy.DEFAULT)
    value["network"]["mode"] = "open"
    policy.POLICY.write_text(json.dumps(value))
    opened = policy.report()
    assert opened["mode"] == "full" and not opened["launch_blocked"] and opened["warning"]
    assert opened["policy_sha256"] != restricted["policy_sha256"]
    monkeypatch.setattr(policy, "authentication_status", lambda: (False, "passwordless"))
    assert policy.report()["mode"] == "manual" and policy.report()["launch_blocked"]


CLASSIC_VISUDO_V = "Visudo version 1.9.15p5\nSudoers file grammar version 50\n"
SUDO_RS_VISUDO_V = "visudo-rs 0.2.13\n"  # captured from sudo-rs 0.2.13 on Ubuntu


def fake_installer(monkeypatch: pytest.MonkeyPatch, *, platform: str = "linux", sudo_rs: bool = False,
                   output: str | None = None, syntax_error: bool = False) -> list[tuple[Path, int]]:
    """Offline installer: real files under tmp, fake ownership and visudo."""
    writes: list[tuple[Path, int]] = []
    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(policy.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=80))
    monkeypatch.setattr(policy, "protected", lambda *a, **kw: None)
    monkeypatch.setattr(policy, "protected_program", lambda *a, **kw: None)
    def atomic(path: Path, data: bytes, mode: int) -> None:
        writes.append((path, mode))
        path.write_bytes(data)
    monkeypatch.setattr(policy, "_atomic", atomic)
    def run(argv: list[str]) -> Any:
        assert argv == [str(policy.VISUDO), "-V"]
        return subprocess.CompletedProcess(argv, 0, SUDO_RS_VISUDO_V if sudo_rs else CLASSIC_VISUDO_V, "")
    monkeypatch.setattr(policy, "_run", run)
    def syntax(path: Path) -> str:
        if syntax_error:
            raise policy.PolicyError("syntax")
        if path != policy.ETC / "sudoers":
            return f"{path}: parsed OK\n"
        if output is not None:
            return output
        if sudo_rs:  # visudo-rs prints only the file it was given
            return f"{path}: parsed OK\n"
        return f"{path}: parsed OK\n{policy.DROP_IN}: parsed OK\n"
    monkeypatch.setattr(policy, "_syntax", syntax)
    return writes


def assert_nothing_installed(etc: Path) -> None:
    assert not os.path.lexists(policy.POLICY.parent) and not os.path.lexists(policy.DROP_IN)
    assert {p.name for p in etc.iterdir()} <= {"sudoers", "sudoers.d"} and not any(policy.DROP_IN.parent.iterdir())
    report = policy.report()
    assert not report["installed"] and not report["launch_blocked"]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("sudo_rs", [False, True])
def test_installer_activates_only_after_complete_sudoers_validation(installation: Path, monkeypatch: pytest.MonkeyPatch, platform: str, sudo_rs: bool) -> None:
    owners: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(policy.os, "chown", lambda path, uid, gid: owners.append((path, uid, gid)))
    (installation / "sudoers").write_text(f"root ALL=(ALL:ALL) ALL\n@includedir {policy.DROP_IN.parent}\n")
    writes = fake_installer(monkeypatch, platform=platform, sudo_rs=sudo_rs)
    policy.install()
    assert writes[-1] == (policy.POLICY, 0o644)
    assert (policy.SETTER, 0o755) in writes and (policy.DROP_IN, 0o444) in writes
    assert json.loads(policy.POLICY.read_text()) == policy.DEFAULT
    assert not (policy.POLICY.parent / "sudoers-check").exists()
    assert owners == ([(policy.POLICY.parent, 0, 80)] if platform == "darwin" else [])
    with pytest.raises(policy.PolicyError, match="already exists") as refused:
        policy.install()
    assert f"sudo rm -f {policy.DROP_IN} && sudo rm -rf {policy.POLICY.parent}" in str(refused.value)
    assert owners == ([(policy.POLICY.parent, 0, 80)] if platform == "darwin" else [])


@pytest.mark.parametrize("directive", ["@includedir {d}", "#includedir {d}", "  @includedir {d}/  "])
def test_sudo_rs_inclusion_is_proven_by_top_level_includedir(installation: Path, monkeypatch: pytest.MonkeyPatch, directive: str) -> None:
    (installation / "sudoers").write_text("Defaults env_reset\n" + directive.format(d=policy.DROP_IN.parent) + "\n")
    fake_installer(monkeypatch, sudo_rs=True)
    policy.install()
    assert json.loads(policy.POLICY.read_text()) == policy.DEFAULT


@pytest.mark.parametrize("case", [
    "syntax", "classic-without-drop-in", "sudo-rs-no-directive", "sudo-rs-commented",
    "sudo-rs-other-directory", "sudo-rs-file-include", "sudo-rs-extra-output", "sudo-rs-substring-only",
])
def test_failed_install_leaves_no_partial_state(installation: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    d = policy.DROP_IN.parent
    sudoers = {
        "sudo-rs-no-directive": "root ALL=(ALL:ALL) ALL\n",
        "sudo-rs-commented": f"# includedir {d}\n",
        "sudo-rs-other-directory": f"@includedir {d}.local\n",
        "sudo-rs-file-include": f"@include {policy.DROP_IN}\n",
    }.get(case, f"@includedir {d}\n")
    (installation / "sudoers").write_text(sudoers)
    top = f"{installation / 'sudoers'}: parsed OK\n"
    output = {"classic-without-drop-in": top, "sudo-rs-extra-output": top + "unexpected\n",
              "sudo-rs-substring-only": f"x{policy.DROP_IN}: parsed OK\n"}.get(case)
    fake_installer(monkeypatch, sudo_rs=case.startswith("sudo-rs"), output=output, syntax_error=case == "syntax")
    with pytest.raises(policy.PolicyError) as refused:
        policy.install()
    assert "rollback incomplete" not in str(refused.value)
    assert_nothing_installed(installation)


def test_incomplete_rollback_prints_exact_cleanup(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (installation / "sudoers").write_text("root ALL=(ALL:ALL) ALL\n")
    fake_installer(monkeypatch, sudo_rs=True)
    real_unlink = Path.unlink
    def unlink(path: Path, missing_ok: bool = False) -> None:
        if path == policy.DROP_IN:
            raise PermissionError("busy")
        real_unlink(path, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(policy.PolicyError, match="rollback incomplete") as refused:
        policy.install()
    assert f"sudo rm -f {policy.DROP_IN} && sudo rm -rf {policy.POLICY.parent}" in str(refused.value)
    assert not policy.POLICY.exists()


@pytest.mark.parametrize("version,expected", [
    (SUDO_RS_VISUDO_V, True), (CLASSIC_VISUDO_V, False), ("", False), ("visudo-rsx\n", False),
])
def test_visudo_flavor_detection(monkeypatch: pytest.MonkeyPatch, version: str, expected: bool) -> None:
    monkeypatch.setattr(policy, "protected_program", lambda *a, **kw: None)
    monkeypatch.setattr(policy, "_run", lambda argv: subprocess.CompletedProcess(argv, 0, version, ""))
    assert policy._visudo_is_sudo_rs() is expected


def hide_drop_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unprivileged user cannot search a root 0750 sudoers.d (Debian/Ubuntu)."""
    real_lstat = Path.lstat
    def lstat(path: Path) -> Any:
        if path.parent == policy.DROP_IN.parent:
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path)
    monkeypatch.setattr(Path, "lstat", lstat)


def test_unsearchable_sudoers_dir_without_policy_dir_is_never_installed(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hide_drop_in(monkeypatch)
    report = policy.report()
    assert not report["installed"] and not report["launch_blocked"] and not report["protected"]
    assert report["network"]["mode"] == "unenforced" and "never installed" in report["reason"]


@pytest.mark.parametrize("artifact", ["directory", "full"])
def test_unsearchable_sudoers_dir_with_policy_dir_stays_fail_closed(installation: Path, monkeypatch: pytest.MonkeyPatch, artifact: str) -> None:
    if artifact == "full":
        write_install()
    else:
        policy.POLICY.parent.mkdir()
    monkeypatch.setattr(policy, "protected", lambda *a, **kw: None)
    monkeypatch.setattr(policy, "authentication_status", lambda: (True, "authenticated"))
    hide_drop_in(monkeypatch)
    report = policy.report()
    assert report["installed"] and report["launch_blocked"] and not report["protected"]
    assert report["mode"] == "manual" and report["network"]["mode"] == "restricted"
    assert "unverifiable" in report["reason"]


def test_atomic_failure_preserves_previous_policy(installation: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_install()
    before = policy.POLICY.read_bytes()
    monkeypatch.setattr(policy, "protected", lambda *a, **kw: None)
    monkeypatch.setattr(policy.os, "fchown", lambda *a: None)
    def fail(*args: Any) -> None:
        raise OSError("replace interrupted")
    monkeypatch.setattr(policy.os, "replace", fail)
    with pytest.raises(OSError):
        policy._atomic(policy.POLICY, b"new", 0o644)
    assert policy.POLICY.read_bytes() == before
    assert not list(policy.POLICY.parent.glob(".policy-*"))


@pytest.fixture
def authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings = {"protected": True, "mode": "full", "launch_blocked": False,
                "reason": "fixture", "policy_sha256": DIGEST, "stops": [], "restart_budget": 2}
    inventory = {"state": "ended", "latest": {"run_id": RUN}, "end": {"outcome": "converged", "head": HEAD}, "budget_used": 0}
    monkeypatch.setattr(herd, "_policy_report", lambda: settings)
    monkeypatch.setattr(herd, "_restart_inventory", lambda *a: inventory)
    return settings, inventory


@pytest.mark.parametrize("case", ["unprotected", "manual", "restricted", "restart", "spent", "unchanged", "exhausted-stop", "active", "missing-end"])
def test_automatic_refusals_write_no_record(authority: Any, case: str) -> None:
    settings, inventory = authority
    if case == "unprotected":
        settings["protected"] = False
    elif case == "manual":
        settings["mode"] = "manual"
    elif case == "restricted":
        settings["launch_blocked"] = True
    elif case == "restart":
        settings["stops"] = ["restart"]
    elif case == "spent":
        inventory["budget_used"] = 2
    elif case in ("unchanged", "exhausted-stop"):
        inventory["end"]["outcome"] = "exhausted"
        if case == "exhausted-stop":
            inventory["end"]["head"] = "b" * 40
            settings["stops"] = ["restart-after-exhausted"]
    elif case == "active":
        inventory["state"] = "active"
    else:
        inventory["end"] = None
    with pytest.raises(herd.ApprovalError):
        herd.record_policy_restart(REPO, PR, HEAD)
    assert not herd._approval_target(REPO, PR, HEAD).exists()


def test_changed_exhausted_head_records_provenance_without_deep(authority: Any) -> None:
    _, inventory = authority
    inventory["end"] = {"outcome": "exhausted", "head": "b" * 40}
    record = herd.record_policy_restart(REPO, PR, HEAD, "keep")
    assert record["ended_run_id"] == RUN and record["ended_outcome"] == "exhausted"
    assert record["budget_used"] == 1 and record["source"] == "autonomy-policy"
    assert record["policy_sha256"] == DIGEST and not record["deep"]
    with pytest.raises(herd.ApprovalError, match="transactional"):
        herd.claim_restart(REPO, PR, HEAD)


@pytest.mark.parametrize("change", ["revoked", "digest", "spent", "parent"])
def test_reservation_rechecks_policy_and_inventory(authority: Any, change: str) -> None:
    settings, inventory = authority
    record = herd.record_policy_restart(REPO, PR, HEAD)
    if change == "revoked":
        settings["mode"] = "manual"
    elif change == "digest":
        settings["policy_sha256"] = "e" * 64
    elif change == "spent":
        inventory["budget_used"] = 2
    else:
        inventory["latest"]["run_id"] = "f" * 64
    with pytest.raises(herd.ApprovalError):
        herd.restart_transaction(REPO, PR, HEAD, record["id"], "f" * 64, lambda r, reserve: reserve("e" * 64))
    assert "intent" not in json.loads(Path(record["path"]).read_text())


def test_reserved_intent_reconciles_after_revocation_but_cannot_publish(authority: Any) -> None:
    settings, _ = authority
    record = herd.record_policy_restart(REPO, PR, HEAD)
    def publish(r: Any, reserve: Any) -> None:
        reserve("e" * 64)
        raise OSError("uncertain publication")
    with pytest.raises(OSError):
        herd.restart_transaction(REPO, PR, HEAD, record["id"], "f" * 64, publish)
    settings["mode"] = "manual"
    with pytest.raises(herd.ApprovalError):
        herd.restart_transaction(REPO, PR, HEAD, record["id"], "f" * 64, publish)
    assert herd.restart_transaction(REPO, PR, HEAD, record["id"], "f" * 64, lambda r, reserve: "reconciled") == "reconciled"


def test_next_ended_run_at_same_head_uses_remaining_budget(authority: Any) -> None:
    _, inventory = authority
    first = herd.record_policy_restart(REPO, PR, HEAD)
    with pytest.raises(herd.ApprovalError, match="already exists"):
        herd.record_policy_restart(REPO, PR, HEAD)
    herd.restart_transaction(REPO, PR, HEAD, first["id"], "f" * 64, lambda r, reserve: reserve("e" * 64))
    inventory.update(budget_used=1, latest={"run_id": "e" * 64}, end={"outcome": "aborted", "head": HEAD})
    second = herd.record_policy_restart(REPO, PR, HEAD)
    assert second["budget_used"] == 2 and second["id"] != first["id"]
    archived = list(Path(first["path"]).parent.glob("*.used.json"))
    assert len(archived) == 1 and json.loads(archived[0].read_text())["id"] == first["id"]


def test_read_only_budget_failure_is_unknown_not_zero(authority: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: Any) -> None:
        raise herd.ApprovalError("missing inventory")
    monkeypatch.setattr(herd, "_restart_inventory", missing)
    result = herd.policy_report(REPO, [PR])
    assert result["budgets"][f"{REPO}#{PR}"]["used"] is None


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], Any]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(herd, "_load_helper", lambda path: controller)
    monkeypatch.setattr(controller, "_current_actor", lambda: "owner")
    monkeypatch.setattr(controller, "_json_output", lambda args: [rows])
    monkeypatch.setattr(controller, "_verify_live_head", lambda *args: None)
    def lifecycle(*args: Any) -> dict[str, Any]:
        records = controller._run_records([r for r in rows if r["user"]["login"] == "owner"])
        return {"state": "ended", "run": {"id": records[-1]["run_id"]} if records else None}
    monkeypatch.setattr(controller, "_require_lifecycle_owner", lifecycle)

    def add(source: str | None, *, actor: str = "owner", ordinal: int = 1, marker_change: str | None = None) -> str:
        prior_records = controller._run_records([r for r in rows if r["user"]["login"] == actor])
        parent = prior_records[-1] if prior_records else None
        content = "Synthetic authorization."
        if source:
            record = {"source": source, "id": f"{len(rows):016x}",
                      "policy_sha256": DIGEST if source == "autonomy-policy" else None,
                      "ended_run_id": parent["run_id"] if parent else None,
                      "ended_outcome": "converged", "budget_used": ordinal}
            marker = controller._restart_marker(record, REPO, PR, HEAD, parent, {"outcome": "converged"})
            if marker_change:
                marker = marker.replace("pr=7", marker_change)
            content = controller._with_restart_marker(content, marker)
        supersedes = parent["comment_id"] if parent else None
        digest = controller._run_digest(tier="lean", max_rounds=2, base="f" * 40, start_head=HEAD, supersedes=supersedes, content=content)
        identifier = len(rows) + 1
        body = (f"<!-- local-review-run:v1 id={digest} tier=lean max-rounds=2 base={'f' * 40} "
                f"start-head={HEAD} supersedes={supersedes if supersedes else 'none'} content-sha256={digest} -->\n{content}")
        rows.append({"id": identifier, "body": body, "user": {"login": actor}})
        rows.append({"id": identifier + 1, "user": {"login": actor}, "body": f"<!-- local-review-run-end:v1 id={digest} outcome=converged head={HEAD} -->"})
        return str(digest)
    add(None)
    return rows, add


def test_inventory_counts_only_authenticated_policy_runs_without_manual_reset(history: Any) -> None:
    _, add = history
    add("autonomy-policy")
    add("user-approval")
    add("autonomy-policy", ordinal=2)
    add(None, actor="other")
    add("autonomy-policy", actor="other")
    assert herd._restart_inventory(REPO, PR, HEAD)["budget_used"] == 2


@pytest.mark.parametrize("case", ["missing", "bad-ordinal", "wrong-pr", "missing-parent", "orphan", "bad-digest", "reused-id", "missing-user"])
def test_inventory_refuses_missing_or_inconsistent_evidence(history: Any, case: str) -> None:
    rows, add = history
    add(None if case == "missing" else "autonomy-policy", ordinal=2 if case == "bad-ordinal" else 1,
        marker_change="pr=8" if case == "wrong-pr" else None)
    if case == "missing-parent":
        del rows[:2]
    elif case == "orphan":
        rows.append({"id": 99, "user": {"login": "owner"}, "body": "<!-- local-review-restart:v1 malformed -->"})
    elif case == "bad-digest":
        rows[-2]["body"] += "tampered"
    elif case == "reused-id":
        rows.append({**rows[-2], "id": 99})
    elif case == "missing-user":
        del rows[0]["user"]
    with pytest.raises(herd.ApprovalError):
        herd._restart_inventory(REPO, PR, HEAD)


@pytest.mark.parametrize("prefix", ["", "owner", "sequence", "plan", "scope", "all"])
def test_restart_marker_position(prefix: str) -> None:
    owner = f"<!-- local-review-owner-transition:v1 id={'a' * 64} -->\n"
    sequence = "<!-- local-review-sequence:v1 engines=codex,claude -->\n\n"
    plan = "<!-- local-review-plan:v1 mode=chain engines=codex,claude -->\n\n"
    scope = "<!-- local-review-scope:v1 behaviour-commits=0 missing-patch-files=0 decision=keep -->\n\n"
    before = {"": "", "owner": owner, "sequence": sequence, "plan": plan, "scope": scope, "all": owner + plan + scope}[prefix]
    assert controller._with_restart_marker(before + "Authorization", "MARKER") == before + "MARKER\n\nAuthorization"
