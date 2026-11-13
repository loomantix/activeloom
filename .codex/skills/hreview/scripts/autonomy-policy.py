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
SUDO = Path("/usr/bin/sudo")
VISUDO = Path("/usr/sbin/visudo")
STOPS = ("restart", "restart-after-exhausted", "scope-decision", "author")
DEFAULT = {"autonomy": {"mode": "full", "stops": [], "restart_budget": 2},
           "network": {"mode": "restricted", "allow": []}}
LIMITATION = "Protected from edits, not yet from same-user workers; unwrapped herders remain trusted."
# Exact noninteractive password-required diagnostics: classic sudo, then sudo-rs.
PASSWORD_REQUIRED = ("sudo: a password is required", "sudo: interactive authentication is required")


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


def authentication_status() -> tuple[bool, str]:
    """An auth diagnostic alone does not prove sudo would allow this command."""
    protected_program(SUDO)
    listing = _run([str(SUDO), "-n", "-l", str(SETTER), "--check"])
    probe = _run([str(SUDO), "-n", str(SETTER), "--check"])
    if probe.returncode == 0:
        return False, "passwordless setter: automatic authority disabled"
    if probe.returncode != 1 or probe.stdout or probe.stderr.strip() not in PASSWORD_REQUIRED:
        return False, "sudo check denied, unavailable, misconfigured or inconclusive"
    if listing.returncode != 0 or listing.stderr or listing.stdout.strip() != f"{SETTER} --check":
        return False, "authentication required but command permission is inconclusive"
    # A rule may require a password for --check but permit `set` without one.
    # Conservatively reject any passwordless rule, even for an unrelated command.
    inventory = _run([str(SUDO), "-n", "-l"])
    if (inventory.returncode or inventory.stderr
            or not re.search(r"^User .+ may run the following commands on .+:$", inventory.stdout, re.MULTILINE)
            or not re.search(r"^\s+\([^)]+\) .+", inventory.stdout, re.MULTILINE)
            or re.search(r"NOPASSWD\s*:|!\s*authenticate", inventory.stdout)):
        return False, "sudo permission inventory is passwordless or inconclusive"
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


def cleanup_hint() -> str:
    return f"sudo rm -f {DROP_IN} && sudo rm -rf {POLICY.parent}"


def _rollback(staged: Path) -> list[Path]:
    """Remove a failed fresh install, drop-in first; return what remains."""
    left = []
    for path in (DROP_IN, POLICY, SETTER, staged, POLICY.parent):
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


def install() -> None:
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
            install()
        elif args.command == "set":
            protected(SETTER, 0o755)
            protected(POLICY, 0o644)
            protected(DROP_IN, 0o444)
            if DROP_IN.read_text() != sudoers_text():
                raise PolicyError("repair the sudoers drop-in before setting policy")
            value = validate({"autonomy": {"mode": args.autonomy, "stops": args.stop, "restart_budget": args.restart_budget},
                              "network": {"mode": args.network, "allow": args.allow}})
            _atomic(POLICY, (json.dumps(value, indent=2) + "\n").encode(), 0o644)
        else:
            raise PolicyError("choose install, set or --check")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"policy refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
