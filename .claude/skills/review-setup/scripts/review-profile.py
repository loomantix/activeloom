#!/usr/bin/env python3
"""Per-user review profile: the model, effort and engine order local reviews use.

Stdlib only, and the only writer of the profile file. Nothing is defaulted
silently: launchers and the review-chain runner resolve settings through this
script, and a missing profile is an error that points at setup. `init` writes
the recommended defaults only when explicitly asked to.

Each engine holds reviewer settings (`model`, `effort`, and for Codex an
optional `fallback` pair), worker settings under `worker` (`model`, `effort`,
optional `fallback` pair), and an optional global `availability`. An engine
marked `unavailable` is never launched and cannot appear in an order. A key the
profile does not hold yet is reported as missing rather than making the whole
profile invalid; older schema_version profiles are read unchanged. The
profile is shared by every repository's synced copy of this helper, so it is
written at the oldest version that holds it: 1 without version 2 settings, 2
without the version 3 `reviewit` setting. Older copies can read those.

Those copies are version-skewed by design, so reads are forward-tolerant and
writes are not. A profile newer than this helper is read for the settings this
helper models, and anything else is set aside rather than rejected; a write is
then refused, because serializing the pruned document would delete settings a
newer checkout depends on. A change that genuinely breaks older readers sets
`min_reader_version`, which they refuse explicitly instead of misreading.

Exit status: 0 success, 1 refused operation (including an unavailable engine
or hosted review), 2 invalid input or profile, 3 no profile or missing keys.
When keys are missing, stdout carries JSON with a `missing` list of dotted keys
such as `claude.worker.model`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = 3
# The oldest schema this reader still understands. A profile NEWER than
# SCHEMA_VERSION is read rather than rejected: the writer below stores the
# lowest version that fits, so a bump means new content exists, not that old
# content changed meaning. A future version that genuinely breaks older readers
# says so with `min_reader_version` instead of relying on the number alone.
OLDEST_READABLE_SCHEMA_VERSION = 1
ENGINES = ("claude", "codex", "gemini")
TIERS = ("lean", "deep")
ROLES = ("reviewer", "worker")
PAIR = ("model", "effort")
AVAILABILITY = ("available", "unavailable")

# Every key this helper models, in one place. `prune_foreign` keeps exactly
# these from a newer profile and `validate_profile` rejects anything else in a
# profile at or below this version. The two must agree by construction: if they
# drift, a newer profile's added key is silently pruned instead of kept, which
# is the same silent-drop this forward-tolerance exists to prevent — only
# harder to see, because nothing fails.
PROFILE_REQUIRED_KEYS = frozenset({"schema_version", "defaults_version", "confirmed_at"})
PROFILE_OPTIONAL_KEYS = frozenset(
    {"engines", "order", "repos", "min_reader_version", "reviewit"}
)
PROFILE_KEYS = PROFILE_REQUIRED_KEYS | PROFILE_OPTIONAL_KEYS
ENGINE_WORKER_KEYS = frozenset({*PAIR, "fallback"})
REPO_OVERRIDE_KEYS = frozenset({"engines", "order"})


def engine_settings_keys(engine: str) -> frozenset[str]:
    """Settings keys one engine accepts. `fallback` is codex-only."""
    keys = frozenset({*PAIR, "worker", "availability"})
    return keys | {"fallback"} if engine == "codex" else keys

CLIS = {"claude": "claude", "codex": "codex", "gemini": "agy"}
# Values each engine CLI accepts for its effort flag.
EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("minimal", "low", "medium", "high", "xhigh", "max"),
    "gemini": ("low", "medium", "high"),
}
# `inherit` omits the model flag so the engine CLI's own configured default
# applies. Gemini's launcher needs an explicit model for its skill preflight.
INHERIT = "inherit"
INHERIT_ENGINES = ("claude", "codex")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
DEFAULTS_PATH = Path(__file__).resolve().with_name("review-profile.defaults.json")
PIN_MODEL = "ACTIVELOOM_REVIEW_MODEL"
PIN_EFFORT = "ACTIVELOOM_REVIEW_EFFORT"

EXIT_REFUSED = 1
EXIT_INVALID = 2
EXIT_UNCONFIGURED = 3


class ProfileError(Exception):
    def __init__(
        self,
        message: str,
        status: int = EXIT_INVALID,
        missing: list[str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.missing = missing


def profile_path() -> Path:
    explicit = os.environ.get("ACTIVELOOM_REVIEW_PROFILE")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            raise ProfileError("ACTIVELOOM_REVIEW_PROFILE must be an absolute path")
        return path
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "activeloom" / "review-profile.json"


# Keys a newer writer stored that this reader does not model. Reads tolerate
# them and drop them from the working document; writes refuse, because writing
# back a document this reader cannot represent would silently delete settings a
# newer checkout is relying on.
FOREIGN_CONTENT: list[str] = []

# The schema_version as it sat on disk. validate_profile relabels the working
# document to SCHEMA_VERSION, so the stored value has to be kept here to tell
# whether a save raises it. None when no profile was read.
STORED_SCHEMA_VERSION: int | None = None


def _foreign(label: str, keys: set[str]) -> None:
    FOREIGN_CONTENT.extend(f"{label}{key}" for key in sorted(keys))


def _sync_remedy() -> str:
    return (
        "this checkout's review-profile helper is older than the profile it "
        "read; sync this checkout to the current upstream before changing "
        "review settings"
    )


def _fail(message: str, status: int = EXIT_INVALID) -> NoReturn:
    raise ProfileError(message, status)


def repository_key(repo: str) -> str:
    if not REPO_RE.fullmatch(repo):
        _fail(f"invalid repository {repo!r}; expected OWNER/REPO")
    return repo.lower()


def validate_pair(engine: str, label: str, settings: dict[str, Any]) -> None:
    if "model" in settings:
        model = settings["model"]
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            _fail(f"{label}: invalid model identifier {model!r}")
        if model == INHERIT and engine not in INHERIT_ENGINES:
            _fail(
                f"{label}: an explicit model is required; {INHERIT!r} is not supported"
            )
    if "effort" in settings:
        effort = settings["effort"]
        if effort not in EFFORTS[engine]:
            _fail(
                f"{label}: invalid effort {effort!r}; "
                f"expected one of {', '.join(EFFORTS[engine])}"
            )


def validate_fallback(engine: str, label: str, fallback: Any) -> None:
    """A fallback is absent, None (disabled), or a complete explicit pair."""
    if fallback is None:
        return
    if not isinstance(fallback, dict) or set(fallback) != set(PAIR):
        _fail(f"{label}: fallback requires both model and effort")
    validate_pair(engine, label, fallback)
    if fallback["model"] == INHERIT:
        _fail(f"{label}: fallback requires an explicit model")


def validate_worker(engine: str, worker: Any) -> None:
    label = f"{engine}.worker"
    if not isinstance(worker, dict):
        _fail(f"{label}: settings must be an object")
    unknown = set(worker) - ENGINE_WORKER_KEYS
    if unknown:
        _fail(f"{label}: unknown keys {sorted(unknown)}")
    validate_pair(engine, label, worker)
    validate_fallback(engine, label, worker.get("fallback"))


def validate_engine_settings(
    engine: str, settings: Any, *, partial: bool = False, repo: bool = False
) -> None:
    if engine not in ENGINES:
        _fail(f"unknown engine {engine!r}; expected one of {', '.join(ENGINES)}")
    if not isinstance(settings, dict):
        _fail(f"{engine}: settings must be an object")
    unknown = set(settings) - engine_settings_keys(engine)
    if unknown:
        _fail(f"{engine}: unknown keys {sorted(unknown)}")
    if not partial and not set(PAIR) <= set(settings):
        _fail(f"{engine}: model and effort are both required")
    if "availability" in settings:
        if repo:
            _fail(
                f"{engine}: availability is global only; "
                "a repository override cannot change it"
            )
        if settings["availability"] not in AVAILABILITY:
            _fail(
                f"{engine}: invalid availability {settings['availability']!r}; "
                f"expected one of {', '.join(AVAILABILITY)}"
            )
    validate_fallback(engine, engine, settings.get("fallback"))
    validate_pair(engine, engine, settings)
    if "worker" in settings:
        validate_worker(engine, settings["worker"])


def validate_order(tier: str, order: Any) -> None:
    if tier not in TIERS:
        _fail(f"unknown tier {tier!r}; expected one of {', '.join(TIERS)}")
    if (
        not isinstance(order, list)
        or not 1 <= len(order) <= len(ENGINES)
        or len(set(order)) != len(order)
        or any(engine not in ENGINES for engine in order)
    ):
        _fail(f"order.{tier}: expected 1-3 distinct engines from {', '.join(ENGINES)}")


def validate_engines_and_orders(
    container: dict[str, Any], label: str, *, repo: bool
) -> None:
    engines = container.get("engines", {})
    if not isinstance(engines, dict):
        _fail(f"{label} engines must be an object")
    for engine, settings in engines.items():
        validate_engine_settings(engine, settings, partial=True, repo=repo)
    order = container.get("order", {})
    if not isinstance(order, dict):
        _fail(f"{label} order must be an object")
    for tier, engines_in_order in order.items():
        validate_order(tier, engines_in_order)


def prune_foreign(document: dict[str, Any]) -> dict[str, Any]:
    """Drop content a newer writer stored, recording it in FOREIGN_CONTENT.

    Only ever applied to a profile whose schema_version is newer than this
    helper's. Unknown keys are otherwise a typo or corruption and stay an error,
    but in a newer profile the same strictness turns any additive upstream
    change into a hard failure for every checkout that has not synced yet, so
    the unknown parts are set aside and the known ones still resolve.
    """
    pruned = {
        key: value
        for key, value in document.items()
        if key in PROFILE_KEYS
    }
    _foreign("", set(document) - set(pruned))

    def prune_engines(engines: Any, label: str) -> dict[str, Any]:
        if not isinstance(engines, dict):
            return engines
        kept = {}
        for engine, settings in engines.items():
            if engine not in ENGINES:
                _foreign(f"{label}engines.", {engine})
                continue
            if not isinstance(settings, dict):
                kept[engine] = settings
                continue
            allowed = engine_settings_keys(engine)
            settings = dict(settings)
            _foreign(f"{label}engines.{engine}.", set(settings) - allowed)
            settings = {k: v for k, v in settings.items() if k in allowed}
            worker = settings.get("worker")
            if isinstance(worker, dict):
                _foreign(
                    f"{label}engines.{engine}.worker.",
                    set(worker) - ENGINE_WORKER_KEYS,
                )
                settings["worker"] = {
                    k: v for k, v in worker.items() if k in ENGINE_WORKER_KEYS
                }
            kept[engine] = settings
        return kept

    if "engines" in pruned:
        pruned["engines"] = prune_engines(pruned["engines"], "")
    reviewit = pruned.get("reviewit")
    if isinstance(reviewit, dict):
        _foreign("reviewit.", set(reviewit) - {"availability"})
        pruned["reviewit"] = {k: v for k, v in reviewit.items() if k == "availability"}
    repos = pruned.get("repos")
    if isinstance(repos, dict):
        kept_repos = {}
        for repo, override in repos.items():
            if isinstance(override, dict):
                override = dict(override)
                _foreign(f"repos.{repo}.", set(override) - REPO_OVERRIDE_KEYS)
                override = {
                    k: v for k, v in override.items() if k in REPO_OVERRIDE_KEYS
                }
                if "engines" in override:
                    override["engines"] = prune_engines(
                        override["engines"], f"repos.{repo}."
                    )
            kept_repos[repo] = override
        pruned["repos"] = kept_repos
    return pruned


def validate_profile(
    document: Any, *, tolerate_foreign: bool = False
) -> dict[str, Any]:
    """Validate structure and values; absent settings are reported by missing_keys."""
    if not isinstance(document, dict):
        _fail("profile must be a JSON object")
    # Tolerance is scoped to profiles a NEWER writer produced. At or below this
    # helper's version an unrecognized key is a typo or corruption, not content
    # from the future, and it stays an error.
    written_by_newer = (
        isinstance(document.get("schema_version"), int)
        and not isinstance(document.get("schema_version"), bool)
        and document["schema_version"] > SCHEMA_VERSION
    )
    if tolerate_foreign and written_by_newer:
        document = prune_foreign(document)
    required = PROFILE_REQUIRED_KEYS
    unknown = set(document) - PROFILE_KEYS
    if unknown:
        _fail(f"profile has unknown keys {sorted(unknown)}")
    missing = required - set(document)
    if missing:
        _fail(f"profile is missing {sorted(missing)}")
    stored = document["schema_version"]
    if not isinstance(stored, int) or isinstance(stored, bool) or stored < 1:
        _fail(f"unsupported profile schema_version {stored!r}")
    if stored < OLDEST_READABLE_SCHEMA_VERSION:
        _fail(f"unsupported profile schema_version {stored!r}")
    # A newer writer sets this only when its change genuinely breaks older
    # readers. Absent it, a higher schema_version means additional content,
    # which prune_foreign has already set aside.
    floor = document.get("min_reader_version", 1)
    if not isinstance(floor, int) or isinstance(floor, bool) or floor < 1:
        _fail(f"invalid min_reader_version {floor!r}")
    if floor > SCHEMA_VERSION:
        _fail(
            f"profile requires a reader of at least version {floor} and this one "
            f"is version {SCHEMA_VERSION}: {_sync_remedy()}"
        )
    for key in ("defaults_version", "confirmed_at"):
        if not isinstance(document[key], str) or not document[key]:
            _fail(f"profile {key} must be a nonempty string")
    validate_engines_and_orders(document, "profile", repo=False)
    if "reviewit" in document:
        reviewit = document["reviewit"]
        if not isinstance(reviewit, dict):
            _fail("profile reviewit must be an object")
        unknown = set(reviewit) - {"availability"}
        if unknown:
            _fail(f"reviewit: unknown keys {sorted(unknown)}")
        if "availability" in reviewit:
            if reviewit["availability"] not in AVAILABILITY:
                _fail(
                    f"reviewit: invalid availability {reviewit['availability']!r}; "
                    f"expected one of {', '.join(AVAILABILITY)}"
                )
    repos = document.get("repos", {})
    if not isinstance(repos, dict):
        _fail("profile repos must be an object")
    normalized_repos = {}
    for repo, override in repos.items():
        key = repository_key(repo)
        if key in normalized_repos:
            _fail(f"repos: duplicate repository {repo!r} ignoring case")
        if not isinstance(override, dict) or not override:
            _fail(f"repos.{repo}: override must be a nonempty object")
        if set(override) - {"engines", "order"}:
            _fail(f"repos.{repo}: only engines and order may be overridden")
        validate_engines_and_orders(override, f"repos.{repo}", repo=True)
        normalized_repos[key] = override
    # Each version holds a subset of the next, so migration only relabels it;
    # every stored value is kept as confirmed.
    migrated = {**document, "schema_version": SCHEMA_VERSION}
    if "repos" in document:
        migrated["repos"] = normalized_repos
    return migrated


def unavailable_engines(document: dict[str, Any]) -> set[str]:
    return {
        engine
        for engine, settings in document.get("engines", {}).items()
        if settings.get("availability") == "unavailable"
    }


def require_consistent_orders(document: dict[str, Any]) -> None:
    unavailable = unavailable_engines(document)
    scopes = [("", document)] + [
        (f"repos.{repo} ", override)
        for repo, override in document.get("repos", {}).items()
    ]
    for prefix, container in scopes:
        for tier, engines_in_order in container.get("order", {}).items():
            named = [engine for engine in engines_in_order if engine in unavailable]
            if named:
                _fail(
                    f"{prefix}order.{tier} names unavailable engine(s) "
                    f"{', '.join(named)}; change the order or the availability "
                    "in the same command",
                    EXIT_REFUSED,
                )


def load_defaults() -> dict[str, Any]:
    try:
        document = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"recommended defaults are unreadable: {error}")
    if not isinstance(document, dict):
        _fail("recommended defaults must be a JSON object")
    # `notes` is explanatory text for setup; it is never stored in a profile.
    settings = {key: value for key, value in document.items() if key != "notes"}
    validated = validate_profile({**settings, "confirmed_at": "defaults"})
    if document.get("schema_version") != SCHEMA_VERSION or missing_keys(
        effective(validated, None), NEEDS_ALL
    ):
        _fail("recommended defaults must be a complete current-schema profile")
    return dict(document)


def load_profile() -> dict[str, Any] | None:
    # Both module globals describe the profile read by THIS call, and a save
    # decides what to do from them. Clear them first so a second load in one
    # process cannot inherit the first's verdict — stale FOREIGN_CONTENT would
    # refuse a save of a profile that is perfectly representable.
    global STORED_SCHEMA_VERSION
    FOREIGN_CONTENT.clear()
    STORED_SCHEMA_VERSION = None
    path = profile_path()
    if path.is_symlink():
        _fail(f"review profile cannot be a symlink: {path}")
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"review profile is unreadable: {path}: {error}")
    profile = validate_profile(document, tolerate_foreign=True)
    if isinstance(document, dict):
        stored = document.get("schema_version")
        STORED_SCHEMA_VERSION = stored if isinstance(stored, int) else None
    return profile


def require_profile() -> dict[str, Any]:
    document = load_profile()
    if document is None:
        _fail(
            f"no review profile at {profile_path()}. Run the review-setup skill "
            "(or `npx activeloom review-config init`) and confirm the model, effort "
            "and engine order before launching reviewers.",
            EXIT_UNCONFIGURED,
        )
    return document


def storage_schema_version(document: dict[str, Any]) -> int:
    """The oldest schema that holds the document, so older readers keep accepting it."""
    if "reviewit" in document:
        return SCHEMA_VERSION
    engines = document.get("engines", {})
    scopes = [engines] + [
        override.get("engines", {}) for override in document.get("repos", {}).values()
    ]
    fits_v1 = (
        set(engines) == set(ENGINES)
        and all(set(PAIR) <= set(settings) for settings in engines.values())
        and set(document.get("order", {})) == set(TIERS)
        and not any(
            {"worker", "availability"} & set(settings)
            for scope in scopes
            for settings in scope.values()
        )
    )
    return 1 if fits_v1 else 2


def save_profile(document: dict[str, Any]) -> Path:
    if FOREIGN_CONTENT:
        # Read tolerance stops at the write. Saving now would serialize only the
        # parts this reader models and drop the rest, turning a recoverable
        # version skew into deleted settings.
        _fail(
            f"profile holds settings this helper does not model "
            f"({', '.join(sorted(set(FOREIGN_CONTENT)))}): {_sync_remedy()}",
            EXIT_REFUSED,
        )
    document = validate_profile(document)
    require_consistent_orders(document)
    document["schema_version"] = storage_schema_version(document)
    previous = STORED_SCHEMA_VERSION
    if previous is not None and document["schema_version"] > previous:
        # The moment a human is present and the consequence is still cheap to
        # avoid. The profile is shared by every checkout's copy of this helper,
        # and a checkout older than this schema cannot read what is about to be
        # written.
        sys.stderr.write(
            f"review profile: storing schema_version "
            f"{document['schema_version']} (was {previous}); checkouts whose "
            f"helper predates it cannot read this profile until they sync\n"
        )
    path = profile_path()
    if path.is_symlink() or path.parent.is_symlink():
        _fail(f"review profile path cannot be a symlink: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".review-profile-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def effective(document: dict[str, Any], repo: str | None) -> dict[str, Any]:
    engines = json.loads(json.dumps(document.get("engines", {})))
    order = {tier: list(value) for tier, value in document.get("order", {}).items()}
    override = (
        document.get("repos", {}).get(repository_key(repo))
        if repo is not None
        else None
    )
    if override:
        for engine, settings in override.get("engines", {}).items():
            target = engines.setdefault(engine, {})
            for key, value in settings.items():
                if key == "worker":
                    target.setdefault("worker", {}).update(value)
                else:
                    target[key] = value
        order.update(
            {tier: list(value) for tier, value in override.get("order", {}).items()}
        )
    return {"engines": engines, "order": order, "repo_override": bool(override)}


# A need names what one run requires: ("reviewer" | "worker", engine) or
# ("order", tier).
Need = tuple[str, str]
NEEDS_ALL: list[Need] = [
    *((role, engine) for engine in ENGINES for role in ROLES),
    *(("order", tier) for tier in TIERS),
]


def parse_need(text: str) -> Need:
    section, dot, field = text.partition(".")
    if section in ENGINES and not dot:
        return ("reviewer", section)
    if section in ENGINES and field == "worker":
        return ("worker", section)
    if section == "order" and field in TIERS:
        return ("order", field)
    if text == "reviewit" or (
        section == "reviewit" and (not dot or field == "availability")
    ):
        return ("hosted", "reviewit")
    _fail(
        f"invalid need {text!r}; expected ENGINE, ENGINE.worker, order.TIER or reviewit "
        f"with ENGINE one of {', '.join(ENGINES)}"
    )


def role_settings(settings: dict[str, Any], role: str) -> dict[str, Any]:
    if role == "worker":
        return dict(settings.get("worker", {}))
    return {key: settings[key] for key in (*PAIR, "fallback") if key in settings}


def missing_keys(merged: dict[str, Any], needs: list[Need]) -> list[str]:
    missing = []
    for kind, name in needs:
        if kind == "order":
            if name not in merged["order"]:
                missing.append(f"order.{name}")
            continue
        if kind == "hosted":
            continue
        settings = merged["engines"].get(name, {})
        if settings.get("availability") == "unavailable":
            continue
        present = role_settings(settings, kind)
        prefix = f"{name}.worker" if kind == "worker" else name
        missing.extend(f"{prefix}.{key}" for key in PAIR if key not in present)
    return missing


def suggestions(merged: dict[str, Any], missing: list[str]) -> dict[str, str]:
    """Worker values pre-fill from the same engine's reviewer values."""
    suggested = {}
    for key in missing:
        engine, _, field = key.partition(".worker.")
        if field and field in merged["engines"].get(engine, {}):
            suggested[key] = merged["engines"][engine][field]
    return suggested


def missing_error(missing: list[str]) -> NoReturn:
    raise ProfileError(
        f"the review profile at {profile_path()} is missing {', '.join(missing)}. "
        "Run the review-setup skill (or `npx activeloom review-config set`) to "
        "confirm them.",
        EXIT_UNCONFIGURED,
        missing,
    )


def resolve(engine: str, repo: str | None, role: str = "reviewer") -> dict[str, Any]:
    if engine not in ENGINES:
        _fail(f"unknown engine {engine!r}")
    if role not in ROLES:
        _fail(f"unknown role {role!r}")
    pinned_model = os.environ.get(PIN_MODEL)
    pinned_effort = os.environ.get(PIN_EFFORT)
    if role == "reviewer" and (pinned_model is not None or pinned_effort is not None):
        # The review-chain runner pins a run's settings when it starts, so a
        # profile edit during the run cannot change the reviewers it launches.
        if pinned_model is None or pinned_effort is None:
            _fail(f"{PIN_MODEL} and {PIN_EFFORT} must be set together")
        settings = {"model": pinned_model, "effort": pinned_effort}
        validate_engine_settings(engine, settings)
        return {"engine": engine, **settings, "source": "run-pinned"}
    merged = effective(require_profile(), repo)
    if merged["engines"].get(engine, {}).get("availability") == "unavailable":
        _fail(
            f"{engine} is marked unavailable in the review profile; run the "
            "review-setup skill to change that",
            EXIT_REFUSED,
        )
    missing = missing_keys(merged, [(role, engine)])
    if missing:
        missing_error(missing)
    source = "repository override" if merged["repo_override"] else "user profile"
    result = {"engine": engine, **role_settings(merged["engines"][engine], role)}
    if role == "worker":
        result["role"] = role
    return {**result, "source": source}


def parse_assignment(assignment: str) -> tuple[str, str, str]:
    key, separator, value = assignment.partition("=")
    section, dot, field = key.partition(".")
    if not separator or not dot or not value:
        _fail(f"invalid assignment {assignment!r}; expected e.g. claude.effort=medium")
    return section, field, value


def set_fallback_field(settings: dict[str, Any], field: str, value: str) -> None:
    if settings.get("fallback") is None:
        settings["fallback"] = {}
    settings["fallback"][field] = value


def apply_assignments(target: dict[str, Any], assignments: list[str]) -> None:
    """Apply assignments; save_profile validates the complete result."""
    for assignment in assignments:
        section, field, value = parse_assignment(assignment)
        if section == "order":
            order = [engine.strip() for engine in value.split(",")]
            validate_order(field, order)
            target.setdefault("order", {})[field] = order
            continue
        if section == "reviewit":
            if field != "availability":
                _fail(f"unknown setting reviewit.{field}; expected reviewit.availability")
            if value not in AVAILABILITY:
                _fail(
                    f"reviewit: invalid availability {value!r}; "
                    f"expected one of {', '.join(AVAILABILITY)}"
                )
            target.setdefault("reviewit", {})["availability"] = value
            continue
        if section not in ENGINES:
            _fail(f"unknown setting {section}.{field}")
        settings = target.setdefault("engines", {}).setdefault(section, {})
        worker_field = field.removeprefix("worker.")
        if field in (*PAIR, "availability"):
            validate_engine_settings(section, {field: value}, partial=True)
            settings[field] = value
        elif section == "codex" and field == "fallback" and value == "none":
            settings["fallback"] = None
        elif section == "codex" and field in ("fallback.model", "fallback.effort"):
            set_fallback_field(settings, field.split(".")[1], value)
        elif field.startswith("worker.") and worker_field in PAIR:
            validate_worker(section, {worker_field: value})
            settings.setdefault("worker", {})[worker_field] = value
        elif field == "worker.fallback" and value == "none":
            settings.setdefault("worker", {})["fallback"] = None
        elif field in ("worker.fallback.model", "worker.fallback.effort"):
            set_fallback_field(
                settings.setdefault("worker", {}), field.split(".")[2], value
            )
        else:
            _fail(f"unknown setting {section}.{field}")


def command_show(args: argparse.Namespace) -> None:
    defaults = load_defaults()
    document = load_profile()
    report: dict[str, Any] = {
        "path": str(profile_path()),
        "configured": document is not None,
        "current_defaults_version": defaults["defaults_version"],
    }
    if document is not None:
        merged = effective(document, args.repo)
        missing = missing_keys(merged, NEEDS_ALL)
        report.update(
            defaults_version=document["defaults_version"],
            confirmed_at=document["confirmed_at"],
            missing=missing,
            suggested=suggestions(merged, missing),
            **merged,
        )
        if "reviewit" in document:
            report["reviewit"] = document["reviewit"]
    print(json.dumps(report, indent=2, sort_keys=True))


def command_init(args: argparse.Namespace) -> None:
    # --replace must also recover from a profile that no longer validates.
    if not args.replace and load_profile() is not None:
        _fail(
            f"a review profile already exists at {profile_path()}; use set to change it",
            EXIT_REFUSED,
        )
    defaults = load_defaults()
    if args.from_file:
        try:
            source = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            _fail(f"cannot read {args.from_file}: {error}")
        if not isinstance(source, dict):
            _fail("the source file must hold a JSON object")
        document = {**source, "schema_version": SCHEMA_VERSION}
        document.setdefault("defaults_version", defaults["defaults_version"])
    else:
        document = json.loads(
            json.dumps(
                {
                    key: defaults[key]
                    for key in ("schema_version", "defaults_version", "engines", "order")
                }
            )
        )
    document["confirmed_at"] = now()
    apply_assignments(document, args.assignments)
    if args.accept_defaults:
        propose_from_choices(document, args.assignments)
    print(save_profile(document))


def propose_from_choices(document: dict[str, Any], assignments: list[str]) -> None:
    """Fit the proposed defaults to the choices made in the same init command.

    A worker value the user did not assign follows that engine's reviewer
    value, and a proposed order leaves out engines marked unavailable.
    """
    assigned = {parse_assignment(item)[:2] for item in assignments}
    for engine, settings in document["engines"].items():
        for field in PAIR:
            if (engine, field) in assigned and (
                engine,
                f"worker.{field}",
            ) not in assigned:
                settings.setdefault("worker", {})[field] = settings[field]
    unavailable = unavailable_engines(document)
    for tier in TIERS:
        if ("order", tier) not in assigned:
            document["order"][tier] = [
                engine for engine in document["order"][tier] if engine not in unavailable
            ]


def command_set(args: argparse.Namespace) -> None:
    document = require_profile()
    if args.repo is not None:
        for assignment in args.assignments:
            sec, _, _ = parse_assignment(assignment)
            if sec == "reviewit":
                _fail(
                    "reviewit: availability is global only; "
                    "a repository override cannot change it"
                )
        repo = repository_key(args.repo)
        override = document.setdefault("repos", {}).setdefault(repo, {})
        apply_assignments(override, args.assignments)
    else:
        apply_assignments(document, args.assignments)
    document["confirmed_at"] = now()
    save_profile(document)
    print(json.dumps(effective(document, args.repo), indent=2, sort_keys=True))


def command_unset(args: argparse.Namespace) -> None:
    document = require_profile()
    repos = document.get("repos", {})
    repo = repository_key(args.repo)
    if repo not in repos:
        _fail(f"no override for {args.repo}", EXIT_REFUSED)
    if not args.keys:
        del repos[repo]
    for key in args.keys:
        section, _, field = key.partition(".")
        if section == "order":
            node: Any = repos[repo].get("order", {})
            path = [field]
        else:
            node = repos[repo].get("engines", {})
            path = [section, *field.split(".")]
        parents = []
        for part in path[:-1]:
            if not isinstance(node, dict) or not isinstance(node.get(part), dict):
                _fail(f"{args.repo} has no override for {key}", EXIT_REFUSED)
            parents.append((node, part))
            node = node[part]
        if not isinstance(node, dict) or path[-1] not in node:
            _fail(f"{args.repo} has no override for {key}", EXIT_REFUSED)
        del node[path[-1]]
        for parent, part in reversed(parents):
            if not parent[part]:
                del parent[part]
    if repo in repos:
        override = repos[repo]
        for name in ("engines", "order"):
            if name in override and not override[name]:
                del override[name]
        if not override:
            del repos[repo]
    if not repos:
        document.pop("repos", None)
    document["confirmed_at"] = now()
    save_profile(document)
    print(json.dumps(effective(document, args.repo), indent=2, sort_keys=True))


def command_resolve(args: argparse.Namespace) -> None:
    print(json.dumps(resolve(args.engine, args.repo, args.role), sort_keys=True))


def command_launch_args(args: argparse.Namespace) -> None:
    settings = resolve(args.engine, args.repo, args.role)
    print(settings["model"])
    print(settings["effort"])


def command_order(args: argparse.Namespace) -> None:
    merged = effective(require_profile(), args.repo)
    if args.tier not in merged["order"]:
        missing_error([f"order.{args.tier}"])
    print(",".join(merged["order"][args.tier]))


def command_detect(args: argparse.Namespace) -> None:
    # A missing CLI only suggests unavailability; setup asks the user to decide.
    report = {}
    for engine, cli in CLIS.items():
        installed = shutil.which(cli) is not None
        report[engine] = {
            "cli": cli,
            "installed": installed,
            "suggested_availability": AVAILABILITY[0 if installed else 1],
        }
    print(json.dumps(report, indent=2, sort_keys=True))


def command_check(args: argparse.Namespace) -> None:
    needs = [parse_need(item) for item in args.need] if args.need else NEEDS_ALL
    document = load_profile()
    merged = effective(document or {}, args.repo if document else None)
    explicit = bool(args.need)
    unavailable = sorted(
        {
            name
            for kind, name in needs
            if explicit
            and (
                (
                    kind != "order"
                    and kind != "hosted"
                    and merged["engines"].get(name, {}).get("availability")
                    == "unavailable"
                )
                or (
                    kind == "hosted"
                    and name == "reviewit"
                    and (document or {}).get("reviewit", {}).get("availability")
                    == "unavailable"
                )
            )
        }
    )
    missing = missing_keys(merged, needs)
    report = {
        "path": str(profile_path()),
        "configured": document is not None,
        "complete": document is not None and not missing and not unavailable,
        "missing": missing,
        "suggested": suggestions(merged, missing),
        "unavailable": unavailable,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if unavailable:
        raise ProfileError(
            f"marked unavailable: {', '.join(unavailable)}", EXIT_REFUSED
        )
    if missing or document is None:
        raise ProfileError(
            f"missing {', '.join(missing) or 'the review profile'}", EXIT_UNCONFIGURED
        )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("path", help="print the profile path")
    commands.add_parser("defaults", help="print the recommended defaults")
    commands.add_parser("detect", help="report which engine CLIs are on PATH")
    show = commands.add_parser("show", help="print the stored profile, if any")
    show.add_argument("--repo")
    init = commands.add_parser("init", help="write a first profile")
    source = init.add_mutually_exclusive_group(required=True)
    source.add_argument("--accept-defaults", action="store_true")
    source.add_argument("--from-file")
    init.add_argument("--replace", action="store_true")
    init.add_argument("assignments", nargs="*")
    set_parser = commands.add_parser("set", help="change stored settings")
    set_parser.add_argument("--repo")
    set_parser.add_argument("assignments", nargs="+")
    unset = commands.add_parser("unset", help="remove a repository override")
    unset.add_argument("--repo", required=True)
    unset.add_argument("keys", nargs="*")
    for name in ("resolve", "launch-args"):
        command = commands.add_parser(name, help="settings one engine launches with")
        command.add_argument("--engine", required=True, choices=ENGINES)
        command.add_argument("--role", choices=ROLES, default="reviewer")
        command.add_argument("--repo")
    check = commands.add_parser(
        "check", help="report the keys a run needs that the profile lacks"
    )
    check.add_argument(
        "--need",
        nargs="+",
        action="extend",
        metavar="ENGINE[.worker]|order.TIER",
    )
    check.add_argument("--repo")
    order = commands.add_parser("order", help="engine order for a tier")
    order.add_argument("--tier", required=True, choices=TIERS)
    order.add_argument("--repo")
    return root


HANDLERS = {
    "path": lambda args: print(profile_path()),
    "defaults": lambda args: print(
        json.dumps(load_defaults(), indent=2, sort_keys=True)
    ),
    "detect": command_detect,
    "show": command_show,
    "init": command_init,
    "set": command_set,
    "unset": command_unset,
    "resolve": command_resolve,
    "launch-args": command_launch_args,
    "order": command_order,
    "check": command_check,
}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        HANDLERS[args.command](args)
    except ProfileError as error:
        if error.missing:
            print(json.dumps({"missing": error.missing, "path": str(profile_path())}))
        print(f"review profile: {error}", file=sys.stderr)
        return error.status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
