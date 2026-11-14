#!/usr/bin/python3 -I
"""Protected policy reader and human-run installer/setter (no launch containment)."""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any

ETC = Path("/private/etc" if sys.platform == "darwin" else "/etc")
POLICY = ETC / "activeloom/policy.json"
SETTER = ETC / "activeloom/activeloom-policy"
DROP_IN = ETC / "sudoers.d/activeloom-policy"
PROOF = ETC / "activeloom/sudo-rs-proof.json"
SUDO = Path("/usr/bin/sudo")
VISUDO = Path("/usr/sbin/visudo")
STOPS = ("restart", "restart-after-exhausted", "scope-decision", "author")
DEFAULT = {"autonomy": {"mode": "full", "stops": [], "restart_budget": 2},
           "network": {"mode": "restricted", "allow": []}}
LIMITATION = "Protected from edits, not yet from same-user workers; unwrapped herders remain trusted."
# Exact noninteractive password-required diagnostics: classic sudo, then sudo-rs.
PASSWORD_REQUIRED = ("sudo: a password is required", "sudo: interactive authentication is required")
SUDO_RS_AUTH = PASSWORD_REQUIRED[1]
INVENTORY_HEADER = r"^User .+ may run the following commands on .+:$"


class PolicyError(ValueError):
    """Policy or its installation cannot establish automatic authority."""


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"autonomy", "network"}:
        raise PolicyError("policy requires exactly autonomy and network")
    autonomy, network = value["autonomy"], value["network"]
    if not isinstance(autonomy, dict) or set(autonomy) - {"mode", "stops", "restart_budget"}:
        raise PolicyError("invalid autonomy fields")
    if autonomy.get("mode") not in ("full", "manual"):
        raise PolicyError("autonomy.mode must be full or manual")
    stops = autonomy.get("stops", [])
    if not isinstance(stops, list) or any(not isinstance(s, str) or s not in STOPS for s in stops) or len(set(stops)) != len(stops):
        raise PolicyError("unknown or duplicate autonomy stop")
    budget = autonomy.get("restart_budget", 2)
    if type(budget) is not int or budget < 0:
        raise PolicyError("restart_budget must be a nonnegative integer")
    if not isinstance(network, dict) or set(network) - {"mode", "allow"}:
        raise PolicyError("invalid network fields")
    if network.get("mode") not in ("restricted", "open"):
        raise PolicyError("network.mode must be restricted or open")
    allow = network.get("allow", [])
    # Hosts only: no schemes, ports, wildcards, paths or shell fragments.
    host = r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
    if (not isinstance(allow, list) or any(not isinstance(s, str) or not re.fullmatch(host, s)
                                        or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in s.split(".")) for s in allow)
            or len(set(allow)) != len(allow)):
        raise PolicyError("network.allow must contain unique lowercase DNS hosts")
    return {"autonomy": {"mode": autonomy["mode"], "stops": sorted(stops), "restart_budget": budget},
            "network": {"mode": network["mode"], "allow": sorted(allow)}}


def protected(path: Path, mode: int | None = None, *, directory: bool = False) -> None:
    """Use lstat on every component; never bless a symlink or writable ancestor."""
    for item in [*reversed(path.parents), path]:
        info = item.lstat()
        is_dir = item != path or directory
        if (info.st_uid != 0 or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) if is_dir else stat.S_ISREG(info.st_mode))):
            raise PolicyError(f"unprotected path: {item}")
        if item == path and mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise PolicyError(f"{path}: expected mode {mode:04o}")
        if sys.platform == "darwin" and item == path and info.st_gid != grp.getgrnam("wheel").gr_gid:
            raise PolicyError(f"{path}: expected group wheel")


def sudoers_text() -> str:
    return f"Defaults!{SETTER} timestamp_timeout=0\n"


def protected_program(path: Path, depth: int = 0) -> None:
    """Permit system alternatives links only when the entire chain is protected."""
    protected(path.parent, directory=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        if info.st_uid != 0 or depth >= 16:
            raise PolicyError(f"unprotected or cyclic executable link: {path}")
        target = Path(os.readlink(path))
        protected_program(Path(os.path.abspath(path.parent / target)), depth + 1)
    else:
        protected(path)
        if not info.st_mode & 0o111:
            raise PolicyError(f"program is not executable: {path}")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=15, check=False, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})


def _inventory_ok(output: str) -> bool:
    """A complete listing with at least one rule and no passwordless entry."""
    return bool(re.search(INVENTORY_HEADER, output, re.MULTILINE)
                and re.search(r"^\s+\([^)]+\) .+", output, re.MULTILINE)
                and not re.search(r"NOPASSWD\s*:|!\s*authenticate", output))


def _needs_proof(result: subprocess.CompletedProcess[str]) -> bool:
    """sudo-rs refuses unauthenticated `-n -l`; only that exact refusal may defer to the proof."""
    return result.returncode == 1 and not result.stdout and result.stderr.strip() == SUDO_RS_AUTH


def _fingerprint(entries: list[str]) -> dict[str, list[int]]:
    """lstat identity of every file that can change the sudo-rs rules for a user.

    Covers file-backed sudoers and accounts only; any change, even a harmless
    one, invalidates the proof until the human re-runs `set`.
    """
    paths = [SUDO.resolve(), ETC / "sudoers", DROP_IN.parent, ETC / "passwd", ETC / "group",
             *(DROP_IN.parent / name for name in entries)]
    out = {}
    for path in paths:
        info = path.lstat()
        out[str(path)] = [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
                          info.st_mtime_ns, info.st_ctime_ns]
    return out


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _proof_status() -> tuple[bool, str]:
    """Check the root-captured sudo-rs proof against the live installation."""
    stale = "sudo-rs permission proof missing or stale; re-run the setter's set command through sudo"
    try:
        protected(PROOF, 0o644)
        proof = json.loads(PROOF.read_bytes())
        if (not isinstance(proof, dict) or proof.get("version") != 1 or proof.get("uid") != os.getuid()
                or proof.get("policy_sha256") != _sha256(POLICY) or proof.get("setter_sha256") != _sha256(SETTER)
                or not isinstance(proof.get("entries"), list)
                or any(not isinstance(e, str) or "/" in e or e in ("", ".", "..") for e in proof["entries"])
                or proof.get("fingerprint") != _fingerprint(proof["entries"])):
            return False, stale
    except (OSError, ValueError):
        return False, stale
    return True, "command permitted by root-captured sudo-rs proof; fresh authentication required"


def authentication_status() -> tuple[bool, str]:
    """An auth diagnostic alone does not prove sudo would allow this command."""
    protected_program(SUDO)
    listing = _run([str(SUDO), "-n", "-l", str(SETTER), "--check"])
    probe = _run([str(SUDO), "-n", str(SETTER), "--check"])
    if probe.returncode == 0:
        return False, "passwordless setter: automatic authority disabled"
    if probe.returncode != 1 or probe.stdout or probe.stderr.strip() not in PASSWORD_REQUIRED:
        return False, "sudo check denied, unavailable, misconfigured or inconclusive"
    deferred = _needs_proof(listing)
    if not deferred and (listing.returncode != 0 or listing.stderr or listing.stdout.strip() != f"{SETTER} --check"):
        return False, "authentication required but command permission is inconclusive"
    # A rule may require a password for --check but permit `set` without one.
    # Conservatively reject any passwordless rule, even for an unrelated command.
    inventory = _run([str(SUDO), "-n", "-l"])
    if _needs_proof(inventory):
        deferred = True
    elif inventory.returncode or inventory.stderr or not _inventory_ok(inventory.stdout):
        return False, "sudo permission inventory is passwordless or inconclusive"
    if deferred:
        # sudo-rs requires authentication for `-l`; root captured the same
        # evidence at install/set time, bound to the policy and sudoers state.
        return _proof_status()
    return True, "command permitted; fresh authentication required"


def report() -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": "manual", "stops": [], "restart_budget": 2,
        "network": {"mode": "unenforced", "allow": []},
        "protected": False, "installed": False, "policy_sha256": None,
        "containment": {"status": "unavailable", "reason": "worker/reviewer containment is not implemented"},
        "launch_blocked": False, "limitation": LIMITATION,
    }
    # A partial install is installed-but-invalid, never a never-installed fallback.
    # The one exception: a sudoers.d this user cannot search (Debian/Ubuntu 0750)
    # hides the drop-in, so only the policy directory can show an installation.
    present = drop_in_hidden = False
    try:
        for path in (POLICY.parent, SETTER, DROP_IN):
            try:
                path.lstat()
                present = True
            except FileNotFoundError:
                pass
            except PermissionError:
                if path != DROP_IN:
                    raise
                drop_in_hidden = True
    except OSError as exc:
        result.update(installed=True, launch_blocked=True, reason=str(exc),
                      network={"mode": "restricted", "allow": []})
        return result
    if not present:
        result["reason"] = "policy never installed; existing manual behavior, network unenforced"
        if drop_in_hidden:
            result["reason"] += f" ({DROP_IN.parent} is not searchable by this user)"
        return result
    result.update(installed=True, launch_blocked=True, network={"mode": "restricted", "allow": []})
    if drop_in_hidden:
        result["reason"] = (f"sudoers drop-in unverifiable: {DROP_IN.parent} is not searchable by this user;"
                            " automatic authority disabled")
        return result
    if os.path.lexists(ETC / "sudoers-rs"):
        # sudo-rs would read sudoers-rs, where the drop-in's inclusion was never proven.
        result["reason"] = f"{ETC / 'sudoers-rs'} exists; drop-in inclusion unproven, automatic authority disabled"
        return result
    try:
        protected(POLICY, 0o644)
        protected(SETTER, 0o755)
        protected(DROP_IN, 0o444)
        protected_program(Path("/usr/bin/python3"))
        if DROP_IN.read_text() != sudoers_text():
            raise PolicyError("sudoers drop-in does not enforce the expected timestamp policy")
        raw = POLICY.read_bytes()
        value = validate(json.loads(raw))
        result.update(configured=value, policy_sha256=hashlib.sha256(raw).hexdigest(),
                      stops=value["autonomy"]["stops"], restart_budget=value["autonomy"]["restart_budget"])
        ok, reason = authentication_status()
        result["reason"] = reason
        if not ok:
            return result
        result.update(protected=True, network=value["network"])
        # No assertion or command-line switch can supply containment proof here.
        result["launch_blocked"] = value["network"]["mode"] == "restricted"
        if result["launch_blocked"]:
            result["reason"] = "restricted containment preflight unavailable; automatic authority disabled"
        else:
            result["mode"] = value["autonomy"]["mode"]
            result["warning"] = "Explicit open network: worker/reviewer launches are uncontained."
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["reason"] = str(exc)
    return result


def _atomic(path: Path, data: bytes, mode: int) -> None:
    protected(path.parent, directory=True)
    if os.path.lexists(path):
        protected(path, mode)
    fd, temporary = tempfile.mkstemp(prefix=".policy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, grp.getgrnam("wheel").gr_gid if sys.platform == "darwin" else 0)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _syntax(path: Path) -> str:
    protected_program(VISUDO)
    check = _run([str(VISUDO), "-c", "-f", str(path)])
    if check.returncode:
        raise PolicyError("sudoers syntax validation failed: " + check.stderr.strip())
    return check.stdout


def _visudo_is_sudo_rs() -> bool:
    protected_program(VISUDO)
    return _run([str(VISUDO), "-V"]).stdout.startswith("visudo-rs ")


def _prove_inclusion(output: str) -> None:
    """Classic visudo lists every included file; visudo-rs reports only the top file."""
    # sudo-rs reads sudoers-rs instead of sudoers when it exists; only sudoers is proven here.
    if os.path.lexists(ETC / "sudoers-rs"):
        raise PolicyError(f"{ETC / 'sudoers-rs'} exists; inclusion can be proven only in {ETC / 'sudoers'}")
    if f"{DROP_IN}: parsed OK" in output.splitlines():
        return
    sudoers = ETC / "sudoers"
    if not _visudo_is_sudo_rs() or output.strip() != f"{sudoers}: parsed OK":
        raise PolicyError("sudoers does not demonstrate inclusion of the policy drop-in")
    # visudo-rs has just parsed every included file; prove the drop-in's
    # directory is one of them with a top-level includedir directive.
    protected(sudoers)
    directive = re.compile(rf"[ \t]*[@#]includedir[ \t]+{re.escape(str(DROP_IN.parent))}/?[ \t]*")
    if not any(directive.fullmatch(line) for line in sudoers.read_text().splitlines()):
        raise PolicyError(f"{sudoers} has no top-level includedir for {DROP_IN.parent}")


def capture_proof() -> str | None:
    """As root on sudo-rs, record `sudo -l -U` evidence for the invoking user.

    Returns a warning when no proof could be recorded; authority then stays
    disabled. Classic sudo lists without a credential and needs no proof.
    """
    if not _visudo_is_sudo_rs():
        return None
    PROOF.unlink(missing_ok=True)
    user, uid = os.environ.get("SUDO_USER", ""), os.environ.get("SUDO_UID", "")
    if not user or user == "root" or not uid.isdigit():
        return "no invoking sudo user; sudo-rs permission proof not recorded"
    protected_program(SUDO)
    sudoers = ETC / "sudoers"
    entries = sorted(os.listdir(DROP_IN.parent))
    include = re.compile(r"[ \t]*[@#]include(?:dir)?[ \t]")
    top = re.compile(rf"[ \t]*[@#]includedir[ \t]+{re.escape(str(DROP_IN.parent))}/?[ \t]*")
    # Only the proven top-level includedir may pull in rules; anything else
    # could change outside the fingerprint.
    for path in (sudoers, *(DROP_IN.parent / e for e in entries)):
        for line in path.read_text().splitlines():
            if include.match(line) and not (path == sudoers and top.fullmatch(line)):
                return f"{path} includes other sudoers files; sudo-rs permission proof not recorded"
    fingerprint = _fingerprint(entries)
    listing = _run([str(SUDO), "-n", "-l", "-U", user, str(SETTER), "--check"])
    if listing.returncode or listing.stderr or listing.stdout.strip() != f"{SETTER} --check":
        return "setter not permitted for the invoking user; sudo-rs permission proof not recorded"
    inventory = _run([str(SUDO), "-n", "-l", "-U", user])
    if inventory.returncode or inventory.stderr or not _inventory_ok(inventory.stdout):
        return "permission inventory is passwordless or inconclusive; sudo-rs permission proof not recorded"
    proof = {"version": 1, "uid": int(uid), "policy_sha256": _sha256(POLICY), "setter_sha256": _sha256(SETTER),
             "entries": entries, "fingerprint": fingerprint}
    _atomic(PROOF, (json.dumps(proof, indent=2, sort_keys=True) + "\n").encode(), 0o644)
    return None


def _record_proof() -> str | None:
    """A proof failure never undoes a valid install or set; authority stays disabled."""
    try:
        return capture_proof()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        PROOF.unlink(missing_ok=True)
        return f"sudo-rs permission proof not recorded: {exc}"


def cleanup_hint() -> str:
    return f"sudo rm -f {DROP_IN} && sudo rm -rf {POLICY.parent}"


def _rollback(staged: Path) -> list[Path]:
    """Remove a failed fresh install, drop-in first; return what remains."""
    left = []
    for path in (DROP_IN, PROOF, POLICY, SETTER, staged, POLICY.parent):
        try:
            path.rmdir() if path == POLICY.parent else path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            left.append(path)
            if path == DROP_IN:
                # Keep visible installation evidence when sudoers.d cannot be searched.
                return left
    return left


def install() -> str | None:
    """Activate a fresh installation last; a failure removes what it created."""
    protected(ETC, directory=True)
    protected(DROP_IN.parent, directory=True)
    protected_program(Path("/usr/bin/python3"))
    if any(os.path.lexists(p) for p in (POLICY.parent, DROP_IN)):
        raise PolicyError("installation already exists or is partial; change a complete installation with set."
                          " Only if inspection shows a failed install, remove it with: " + cleanup_hint())
    POLICY.parent.mkdir(mode=0o755)
    staged = POLICY.parent / "sudoers-check"
    try:
        if sys.platform == "darwin":
            os.chown(POLICY.parent, 0, grp.getgrnam("wheel").gr_gid)
        _atomic(SETTER, Path(__file__).read_bytes(), 0o755)
        _atomic(staged, sudoers_text().encode(), 0o444)
        _syntax(staged)
        _atomic(DROP_IN, staged.read_bytes(), 0o444)
        staged.unlink()
        _prove_inclusion(_syntax(ETC / "sudoers"))
        _atomic(POLICY, (json.dumps(validate(DEFAULT), indent=2) + "\n").encode(), 0o644)
    except BaseException as exc:
        if _rollback(staged):
            raise PolicyError(f"{exc}; rollback incomplete, remove the failed install with: " + cleanup_hint()) from exc
        raise
    return _record_proof()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="read-only sudo authentication probe")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("install", help="human only: fresh protected installation")
    setter = sub.add_parser("set", help="human only: atomically replace policy")
    setter.add_argument("--autonomy", choices=("full", "manual"), required=True)
    setter.add_argument("--stop", choices=STOPS, action="append", default=[])
    setter.add_argument("--restart-budget", type=int, default=2)
    setter.add_argument("--network", choices=("restricted", "open"), required=True)
    setter.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.check:
            if args.command:
                raise PolicyError("--check cannot mutate policy")
            # A success means sudo launched us, even if the installation is invalid.
            return 0
        if os.geteuid() != 0:
            raise PolicyError("only the user may run this through sudo in their terminal")
        if args.command == "install":
            warning = install()
        elif args.command == "set":
            protected(SETTER, 0o755)
            protected(POLICY, 0o644)
            protected(DROP_IN, 0o444)
            if DROP_IN.read_text() != sudoers_text():
                raise PolicyError("repair the sudoers drop-in before setting policy")
            value = validate({"autonomy": {"mode": args.autonomy, "stops": args.stop, "restart_budget": args.restart_budget},
                              "network": {"mode": args.network, "allow": args.allow}})
            _atomic(POLICY, (json.dumps(value, indent=2) + "\n").encode(), 0o644)
            warning = _record_proof()
        else:
            raise PolicyError("choose install, set or --check")
        if warning:
            print(f"policy warning: {warning}; automatic authority stays disabled", file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"policy refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
