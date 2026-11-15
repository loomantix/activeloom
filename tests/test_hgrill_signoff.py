"""Contract for hgrill's sign-off renderer and loopback return path (`hgrill/scripts/signoff.py`)."""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import json
import re
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prompts/skills/hgrill/scripts/signoff.py"
FIXTURE = Path(__file__).parent / "fixtures/hgrill-signoff/legacy-body.md"
REPO = "example-org/example-repo"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hgrill_signoff", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve the module by name
    spec.loader.exec_module(module)
    return module


so = load()


def legacy() -> Any:
    return so.load_snapshot(REPO, 7, "Synthetic issue", FIXTURE.read_text(encoding="utf-8"))


def by_id(snapshot: Any) -> dict[str, dict[str, Any]]:
    return {i["id"]: i for i in snapshot.items}


def v1_snapshot() -> dict[str, Any]:
    return {
        "schema": "hgrill-assumptions:v1",
        "items": [
            {
                "id": "P1",
                "kind": "product",
                "title": "Pick a mode",
                "disputed": True,
                "options": [{"key": "a", "text": "fast", "engines": "alpha"}, {"key": "b", "text": "safe", "engines": "beta"}],
                "applied": "b",
                "recommended": "b",
            },
            {"id": "A1", "kind": "assumption", "title": "Rows are immutable", "if_wrong": "edits slip in"},
        ],
        "checked": [],
    }


def v1_body(snapshot: dict[str, Any], prose: str = "**Product calls:**\n1. prose that is ignored") -> str:
    return f"{so.START}\n<!-- hgrill-assumptions:v1 {json.dumps(snapshot)} -->\n{prose}\n{so.END}\n"


def entry_for(snapshot: Any) -> dict[str, Any]:
    entry: dict[str, Any] = so.build_manifest([snapshot])["issues"][0]
    return entry


def all_confirm(snapshot: Any) -> dict[str, Any]:
    answers = []
    for item in snapshot.items:
        if item["kind"] == "refuted":
            answers.append({"id": item["id"], "answer": "decide", "note": "keep per-tenant"})
        elif item["options"] and not item["applied"]:
            answers.append({"id": item["id"], "answer": "choose", "option": item["options"][0]["key"]})
        else:
            answers.append({"id": item["id"], "answer": "confirm"})
    return {
        "schema": "hgrill-signoff:v1",
        "repo": snapshot.repo,
        "issue": snapshot.issue,
        "snapshot_source": snapshot.source,
        "snapshot_hash": snapshot.snapshot_hash,
        "answers": answers,
    }


# --- legacy parser -------------------------------------------------------------------


def test_legacy_ids_follow_block_numbering() -> None:
    snap = legacy()
    assert snap.source == "legacy-prose-sha256"
    assert [i["id"] for i in snap.items] == ["R1", "P1", "P2", "P3", "P4", "P5", "A1", "A2"]
    assert snap.checked == ["The export endpoint is rate limited (`api.py:40`)."]


def test_legacy_semicolon_options_and_engine_tails() -> None:
    p1 = by_id(legacy())["P1"]
    assert p1["disputed"] is True
    assert p1["options"] == [
        {"key": "a", "text": "only admins", "engines": "alpha"},
        {"key": "b", "text": "any member", "engines": "beta"},
    ]
    assert (p1["applied"], p1["recommended"]) == ("b", "a")
    assert p1["evidence"] == "none — the passes' judgment"
    assert p1["detail"] == "Recommended (a), because exports carry #12 data"


def test_legacy_three_option_dispute() -> None:
    p2 = by_id(legacy())["P2"]
    assert [o["key"] for o in p2["options"]] == ["a", "b", "c"]
    assert [o["engines"] for o in p2["options"]] == ["alpha", "beta", "gamma"]
    assert p2["options"][2]["text"] == "forever"
    assert (p2["applied"], p2["recommended"]) == ("c", "b")
    assert p2["evidence"] == "`retention.md`"


def test_legacy_options_without_applied_one() -> None:
    p3 = by_id(legacy())["P3"]
    assert len(p3["options"]) == 2 and p3["applied"] is None
    assert so.allowed_answers(p3) == {"choose", "amend"}


def test_legacy_titles_keep_raw_text() -> None:
    items = by_id(legacy())
    assert items["P1"]["title"] == "Who can export a `report` → CSV?"
    assert items["P4"]["title"] == r"Undo is limited to $n \to 10$ steps"
    assert items["P4"]["passes"] == "alpha, beta"
    assert items["P4"]["if_reversed"] == "undo history grows without bound"
    assert items["P5"]["if_reversed"] == ""


def test_legacy_assumption_warning_is_a_dispute_not_options() -> None:
    a2 = by_id(legacy())["A2"]
    assert a2["disputed"] is True
    assert a2["options"] == [] and a2["applied"] is None
    assert a2["reconciled"].startswith("kept auto-select")
    assert a2["if_wrong"].endswith("It also assumes rows are immutable")
    assert so.allowed_answers(a2) == {"confirm", "doubt", "wrong"}


def test_legacy_refuted_entry() -> None:
    r1 = by_id(legacy())["R1"]
    assert r1["title"] == "The widget cache is per user"
    assert r1["detail"].startswith("is false: it is shared per tenant")
    assert so.allowed_answers(r1) == {"decide"}


@pytest.mark.parametrize(
    "inner",
    [
        # An unknown heading must not file the next assumption as a product call.
        "**Product calls — yours to confirm:**\n1. **Call** — alpha.\n**Assumptions (reconciled):**\n1. **Hidden** — alpha.",
        # A refuted entry in the numbered form must not vanish.
        "**Refuted and not resolved — x:**\n1. ❌ **Numbered** is false: y.\n**Assumptions:**\n1. **A** — alpha.",
        # A quote between items must not drop the items after it.
        "**Assumptions:**\n1. **A one** — alpha.\n> aside\n2. **A two** — alpha.",
        # An unindented wrapped line is not silently discarded.
        "**Assumptions:**\n1. **A one** — alpha.\nwrapped without indent",
    ],
)
def test_legacy_unclaimed_section_lines_fail_closed(inner: str) -> None:
    with pytest.raises(so.SignoffError, match="unrecognised line"):
        so.load_snapshot(REPO, 1, "t", f"{so.START}\n{inner}\n{so.END}")


def test_legacy_option_letters_need_parentheses() -> None:
    inner = (
        "**Product calls — yours to confirm:**\n"
        "1. ⚠ **Q** — the passes disagreed. Options: (a) one — x; (b) two — y. "
        "Applied now: both, pending. Recommended: apply (a) now. Evidence: none."
    )
    p1 = by_id(so.load_snapshot(REPO, 1, "t", f"{so.START}\n{inner}\n{so.END}"))["P1"]
    assert (p1["applied"], p1["recommended"]) == (None, None)


@pytest.mark.parametrize(
    "body, message",
    [
        ("no block here", "no hgrill-assumptions block"),
        (f"{so.START}\nx\n{so.END}\n{so.START}\ny\n{so.END}", "exactly one"),
        (f"{so.END}\n{so.START}\n", "precedes"),
        (f"{so.START}\n**Assumptions:**\n\n{so.END}", "has no product calls"),
    ],
)
def test_block_errors(body: str, message: str) -> None:
    with pytest.raises(so.SignoffError, match=message):
        so.load_snapshot(REPO, 1, "t", body)


def test_disputed_first_keeps_ids() -> None:
    ordered = [i["id"] for i in so.display_order(legacy().items)]
    assert ordered == ["R1", "P1", "P2", "P3", "A2", "P4", "P5", "A1"]


# --- hashing -------------------------------------------------------------------------


def test_legacy_hash_normalizes_line_endings_and_trailing_space() -> None:
    body = FIXTURE.read_text(encoding="utf-8")
    noisy = body.replace("\n", "   \r\n").replace(so.START, so.START + "\n\n")
    base = so.load_snapshot(REPO, 7, "t", body).snapshot_hash
    assert so.load_snapshot(REPO, 7, "t", noisy).snapshot_hash == base
    inner = body.split(so.START)[1].split(so.END)[0]
    assert base == hashlib.sha256(so.normalize_block(inner).encode()).hexdigest()
    changed = body.replace("only admins", "only owners")
    assert so.load_snapshot(REPO, 7, "t", changed).snapshot_hash != base


def test_legacy_hash_ignores_text_outside_markers() -> None:
    body = FIXTURE.read_text(encoding="utf-8")
    assert so.load_snapshot(REPO, 7, "t", body + "\nmore\n").snapshot_hash == so.load_snapshot(REPO, 7, "t", body).snapshot_hash


def test_v1_json_takes_precedence_and_hash_is_canonical() -> None:
    snap = v1_snapshot()
    first = so.load_snapshot(REPO, 3, "t", v1_body(snap))
    assert first.source == "v1-json"
    assert [i["id"] for i in first.items] == ["P1", "A1"]
    reordered = json.loads(json.dumps(snap))
    reordered["items"][0] = dict(reversed(list(reordered["items"][0].items())))
    second = so.load_snapshot(REPO, 3, "t", v1_body(reordered, prose="different prose"))
    assert second.snapshot_hash == first.snapshot_hash
    reordered["snapshot_hash"] = "0" * 64
    assert so.load_snapshot(REPO, 3, "t", v1_body(reordered)).snapshot_hash == first.snapshot_hash
    snap["items"][1]["title"] = "Rows are append-only"
    assert so.load_snapshot(REPO, 3, "t", v1_body(snap)).snapshot_hash != first.snapshot_hash


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda s: s.update(schema="v0"), "schema"),
        (lambda s: s["items"].append(dict(s["items"][1])), "duplicate item id"),
        (lambda s: s["items"][1].update(kind="product"), "does not match its id"),
        (lambda s: s["items"][0].update(applied="z"), "not an option"),
        (lambda s: s["items"][1].update(options=[{"key": "a"}]), "only product calls"),
        (lambda s: s["items"][0].update(id="X1"), "invalid item id"),
    ],
)
def test_v1_json_validation(mutate: Any, message: str) -> None:
    snap = v1_snapshot()
    mutate(snap)
    with pytest.raises(so.SignoffError, match=message):
        so.load_snapshot(REPO, 3, "t", v1_body(snap))


def test_v1_json_duplicate_keys_rejected() -> None:
    body = f'{so.START}\n<!-- hgrill-assumptions:v1 {{"schema": "hgrill-assumptions:v1", "items": [], "items": []}} -->\n{so.END}'
    with pytest.raises(so.SignoffError, match="duplicate JSON key"):
        so.load_snapshot(REPO, 3, "t", body)


# --- submission validation -----------------------------------------------------------


def test_valid_submission_normalizes_choose_of_applied_option() -> None:
    snap = legacy()
    payload = all_confirm(snap)
    for a in payload["answers"]:
        if a["id"] == "P1":
            a.update(answer="choose", option="b")
    out = so.validate_submission(payload, entry_for(snap))
    p1 = next(a for a in out["answers"] if a["id"] == "P1")
    assert p1 == {"id": "P1", "answer": "confirm", "option": None, "note": ""}
    assert [a["id"] for a in out["answers"]] == [i["id"] for i in snap.items]


def _with(payload: dict[str, Any], iid: str, **changes: Any) -> dict[str, Any]:
    for a in payload["answers"]:
        if a["id"] == iid:
            a.clear()
            a.update(id=iid, **changes)
    return payload


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p.update(snapshot_hash="0" * 64), "snapshot_hash does not match"),
        (lambda p: p.update(snapshot_source="v1-json"), "snapshot_source"),
        (lambda p: p.update(issue=8), "repo or issue"),
        (lambda p: p.update(issue=True), "integer"),
        (lambda p: p.update(schema="hgrill-signoff:v0"), "schema"),
        (lambda p: p.update(extra=1), "unknown payload keys"),
        (lambda p: p["answers"].pop(), "unanswered items: A2"),
        (lambda p: p["answers"].append({"id": "P9", "answer": "confirm"}), "unknown item id"),
        (lambda p: p["answers"].append({"id": "A1", "answer": "confirm"}), "duplicate answer"),
        (lambda p: _with(p, "A1", answer="wrong"), "note is required for wrong"),
        (lambda p: _with(p, "A1", answer="wrong", note="   "), "note is required"),
        (lambda p: _with(p, "P1", answer="amend"), "note is required for amend"),
        (lambda p: _with(p, "R1", answer="decide"), "note is required for decide"),
        (lambda p: _with(p, "P5", answer="reverse"), "note is required for reverse"),
        (lambda p: _with(p, "P3", answer="confirm"), "not allowed"),
        (lambda p: _with(p, "P1", answer="reverse", note="x"), "not allowed"),
        (lambda p: _with(p, "P1", answer="choose", option="z"), "choose needs one of the options"),
        (lambda p: _with(p, "A1", answer="confirm", option="a"), "option is only valid with choose"),
        (lambda p: _with(p, "A1", answer="doubt", note="x" * 4001), "at most 4000"),
        (lambda p: _with(p, "A1", answer="confirm", why="x"), "unknown answer keys"),
    ],
)
def test_invalid_submissions(mutate: Any, message: str) -> None:
    snap = legacy()
    payload = all_confirm(snap)
    mutate(payload)
    with pytest.raises(so.SignoffError, match=message):
        so.validate_submission(payload, entry_for(snap))


def test_reverse_with_if_reversed_needs_no_note_and_doubt_note_optional() -> None:
    snap = legacy()
    payload = _with(_with(all_confirm(snap), "P4", answer="reverse"), "A1", answer="doubt")
    out = so.validate_submission(payload, entry_for(snap))
    assert {a["id"]: a["answer"] for a in out["answers"]}["P4"] == "reverse"


def test_manifest_holds_identifiers_only() -> None:
    manifest = json.dumps(so.build_manifest([legacy()]))
    for prose in ("only admins", "widget cache", "rate limited", "Synthetic issue"):
        assert prose not in manifest


# --- page ----------------------------------------------------------------------------


def test_page_is_self_contained_and_csp_hash_matches_script() -> None:
    page = so.render_page([legacy(), so.load_snapshot(REPO, 3, "Other", v1_body(v1_snapshot()))])
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert len(scripts) == 1
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in page
    assert not re.search(r"(src|href)=\"?https?:", page)
    match = re.search(r'id="signoff-data">(.*?)</script>', page, re.S)
    assert match
    data = json.loads(match.group(1))
    assert [i["issue"] for i in data["issues"]] == [7, 3]
    assert [i["id"] for i in data["issues"][0]["items"]][:2] == ["R1", "P1"]


def test_page_escapes_hostile_titles() -> None:
    snap = v1_snapshot()
    snap["items"][0]["title"] = "</script><script>alert(1)</script><!--"
    page = so.render_page([so.load_snapshot(REPO, 3, "<b>t</b>", v1_body(snap))])
    assert page.count("</script>") == 2
    assert "<script>alert" not in page and "<b>t</b>" not in page
    assert "innerHTML" not in so.PAGE_JS


def test_page_keeps_same_number_cross_repo_answers_selected() -> None:
    snapshots = [so.load_snapshot(repo, 7, "Synthetic", v1_body(v1_snapshot())) for repo in (REPO, "example-org/other")]
    probe = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const {script, data} = JSON.parse(fs.readFileSync(0, 'utf8'));
const nodes = [];
function node(tag) {
  let checked = false;
  const n = {tag, children: [], attrs: {}, listeners: {}, value: '',
    classList: {toggle() {}},
    appendChild(c) {this.children.push(c); return c;},
    setAttribute(k, v) {this.attrs[k] = v;},
    addEventListener(e, f) {this.listeners[e] = f;},
    fire(e) {this.listeners[e]?.();},
    get checked() {return checked;},
    set checked(v) {
      // Native radios with the same name and no form owner share a group,
      // including radios in hidden tab panels.
      if (v && this.type === 'radio') {
        nodes.filter(o => o !== this && o.type === 'radio' && o.name === this.name)
          .forEach(o => {o.checked = false;});
      }
      checked = v;
    }
  };
  nodes.push(n); return n;
}
const document = {
  getElementById: id => id === 'signoff-data' ? {textContent: JSON.stringify(data)} : node('main'),
  createElement: node, createTextNode: text => ({textContent: text})
};
vm.runInNewContext(script, {document, URLSearchParams,
  location: {protocol: 'file:', search: ''},
  localStorage: {getItem() {return null;}, setItem() {}}});
const tabs = nodes.filter(n => n.attrs.role === 'tab');
const panels = nodes.filter(n => n.attrs.role === 'tabpanel');
const selected = [];
panels.forEach((panel, ix) => {
  tabs[ix].fire('click');
  const cards = panel.children.filter(n => n.className === 'card');
  const option = cards[0].children.find(n => n.className === 'opt').children[0];
  option.checked = true; option.fire('change');
  const choices = cards[1].children.find(n => n.className === 'choices');
  const action = choices.children[ix].children[0];
  action.checked = true; action.fire('change');
  selected.push(option, action);
});
tabs[0].fire('click');
process.stdout.write(JSON.stringify(selected.map(n => n.checked)));
"""
    result = subprocess.run(
        ["node", "-e", probe],
        input=json.dumps({"script": so.PAGE_JS, "data": so.page_data(snapshots)}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == [True, True, True, True]


def test_render_cli_writes_private_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = so.main(["render", "--repo", REPO, "7", "--body", f"7={FIXTURE}", "--out-dir", str(out)])
    assert code == 0
    for name in ("signoff.html", "signoff-manifest.json"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    manifest = json.loads((out / "signoff-manifest.json").read_text())
    assert manifest["issues"][0]["snapshot_hash"] == legacy().snapshot_hash


def test_render_cli_rejects_bad_refs(tmp_path: Path) -> None:
    assert so.main(["render", "7", "--out-dir", str(tmp_path)]) == 2
    assert so.main(["render", "--repo", REPO, "7", "7", "--out-dir", str(tmp_path)]) == 2


# --- serve ---------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path: Path) -> Any:
    snap = legacy()
    stored: dict[tuple[str, int], Path] = {}

    def resolve(repo: str, issue: int) -> Path:
        stored[(repo, issue)] = tmp_path / "state" / f"{issue}" / "signoff-answers.json"
        return stored[(repo, issue)]

    srv = so.SignoffServer(so.render_page([snap]).encode(), so.build_manifest([snap]), "tok-123", resolve_path=resolve)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.snap, srv.stored = snap, stored
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv: Any, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
    base = {"Content-Type": "application/json", "X-Signoff-Token": "tok-123"}
    base.update(headers or {})
    conn.request(method, path, body=data, headers=base)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw.decode()


def test_serve_binds_loopback_and_guards_the_page(server: Any) -> None:
    assert server.server_address[0] == "127.0.0.1"
    assert server.url().startswith(f"http://127.0.0.1:{server.port}/?token=")
    assert request(server, "GET", "/?token=tok-123")[0] == 200
    assert request(server, "GET", "/?token=wrong")[0] == 403
    assert request(server, "GET", "/")[0] == 403
    assert request(server, "GET", "/?token=tok-123", headers={"Host": "attacker.example"})[0] == 403


def test_page_navigation_remains_reloadable(server: Any) -> None:
    # Execute the page script with an empty tab list; follow the resulting URL
    # through the real HTTP handler, as a browser reload does.
    probe = r"""
const vm = require('node:vm');
const fs = require('node:fs');
let location = new URL(process.argv[1]);
const node = () => ({appendChild() {}, setAttribute() {}});
const document = {
  getElementById: id => id === 'signoff-data' ? {textContent: '{"issues":[]}'} : node(),
  createElement: node
};
vm.runInNewContext(fs.readFileSync(0, 'utf8'), {
  document, URLSearchParams, location,
  history: {replaceState: (_state, _title, path) => { location = new URL(path, location); }}
});
process.stdout.write(location.href);
"""
    result = subprocess.run(["node", "-e", probe, server.url()], input=so.PAGE_JS, text=True, capture_output=True, check=True)
    url = urlsplit(result.stdout)
    assert request(server, "GET", url.path + ("?" + url.query if url.query else ""))[0] == 200
    assert request(server, "GET", "/")[0] == 403


def test_post_accepts_valid_answers_and_writes_state(server: Any) -> None:
    status, body = request(server, "POST", "/answers", all_confirm(server.snap))
    assert status == 200 and body["ok"] is True
    path = server.stored[(REPO, 7)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = json.loads(path.read_text())
    assert record["schema"] == "hgrill-signoff-answers:v1"
    assert record["submission"]["snapshot_hash"] == server.snap.snapshot_hash
    assert server.all_done.is_set()


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"X-Signoff-Token": "wrong"}, 403),
        ({"X-Signoff-Token": ""}, 403),
        ({"Origin": "https://attacker.example"}, 403),
        ({"Host": "rebound.example"}, 403),
        ({"Content-Type": "text/plain"}, 415),
    ],
)
def test_post_rejects_bad_request_envelope(server: Any, headers: dict[str, str], status: int) -> None:
    assert request(server, "POST", "/answers", all_confirm(server.snap), headers)[0] == status
    assert not server.stored


def test_post_rejects_wrong_hash_and_bad_items(server: Any) -> None:
    payload = all_confirm(server.snap)
    payload["snapshot_hash"] = "f" * 64
    assert request(server, "POST", "/answers", payload)[0] == 409
    payload = all_confirm(server.snap)
    payload["answers"].append({"id": "A9", "answer": "confirm"})
    status, body = request(server, "POST", "/answers", payload)
    assert status == 400 and "unknown item id" in body["error"]
    payload = all_confirm(server.snap)
    payload["answers"].pop(0)
    assert request(server, "POST", "/answers", payload)[0] == 400
    payload = _with(all_confirm(server.snap), "A1", answer="wrong")
    status, body = request(server, "POST", "/answers", payload)
    assert status == 400 and "note is required" in body["error"]
    payload = all_confirm(server.snap)
    payload["issue"] = 99
    assert request(server, "POST", "/answers", payload)[0] == 404
    assert request(server, "POST", "/answers", b"{not json")[0] == 400
    assert request(server, "POST", "/answers", b"[]")[0] == 400
    assert not server.stored and not server.all_done.is_set()


def test_answers_path_uses_herd_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = so.answers_path(REPO, 7)
    assert path == tmp_path / "activeloom-herd/example-org__example-repo/hgrill/issue-7/signoff-answers.json"
