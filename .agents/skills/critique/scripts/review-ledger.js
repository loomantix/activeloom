#!/usr/bin/env node

// src/cli.ts
import { readFileSync as readFileSync4 } from "fs";

// src/constants.ts
var PROTOCOL_VERSION = 3;
var PACKAGE_VERSION = true ? "1.1.0" : "0.0.0-dev";
var SUBPROCESS_MAX_BUFFER = 256 * 1024 * 1024;
var EXPECTED_ACTOR_ENV = "AGENT_LOOP_REVIEW_ACTOR";
var EXPECTED_THREADS_SHA256_ENV = "AGENT_LOOP_REVIEW_THREADS_SHA256";
var HISTORICAL_COMMENT_IDS_ENV = "AGENT_LOOP_REVIEW_HISTORICAL_COMMENT_IDS_FILE";
var SUPPORTED_ENGINES = [
  "codex",
  "claude",
  "gemini",
  "antigravity"
];
var SUPPORTED_SEVERITIES = [
  "blocking",
  "major",
  "minor",
  "nit"
];
var SUPPORTED_OUTCOMES = [
  "fixed",
  "dismissed",
  "deferred"
];
var SUPPORTED_CLASSIFICATIONS = [
  "minor",
  "material"
];
var SUPPORTED_SIDES = [
  "RIGHT",
  "LEFT"
];
var HUNK_WITH_LEFT_RE = /^@@ -(?<left>\d+)(?:,\d+)? \+(?<right>\d+)(?:,\d+)? @@/;
var SHA_RE = /^[0-9a-f]{40}$/;
var SHA_64_RE = /^[0-9a-f]{64}$/;
var TOKEN_RE = /^[A-Za-z0-9._:/-]+$/;
var PROTOCOL_THREAD_MARKER_RE = /^<!--[ \t]*local-review(?=[: \t-])/;
var LEGACY_THREAD_MARKER_RE = /^<!--[ \t]*local-review(?:-disposition)?:v1(?=[ \t]|-->)/;
var FINDING_V1 = "<!-- local-review:v1 ";
var DISPOSITION_V1 = "<!-- local-review-disposition:v1 ";
var FINDING_V3_OPENER = "<!-- local-review:v3";
var PR_V1_MARKERS = [
  "<!-- local-review-refactor:v1 ",
  "<!-- local-review-pass:v1 ",
  "<!-- local-review-complete:v1 ",
  "<!-- local-review-tier:v1 "
];
var FINDING_V3_RE = /^<!-- local-review:v3 engine=(?<engine>codex|claude|gemini|antigravity) round=(?<round>[1-9][0-9]*) head=(?<head>[0-9a-f]{40}) fingerprint=(?<fingerprint>[A-Za-z0-9._:/-]+) occurrence=(?<occurrence>[1-9][0-9]*) severity=(?<severity>blocking|major|minor|nit) lens=(?<lens>[A-Za-z0-9._:/-]+) content-sha256=(?<content_sha>[0-9a-f]{64}) -->$/m;
var PSEUDO_V3_RE = /^<!-- local-review:v3 engine=(?:claude|gemini|antigravity) fingerprint=(?<fingerprint>[A-Za-z0-9._:/-]+)(?: outcome=deferred)? -->$/m;
var DISPOSITION_V3_RE = /^<!-- local-review-disposition:v3 engine=(?<engine>codex|claude|gemini|antigravity) round=(?<round>[1-9][0-9]*) head=(?<head>[0-9a-f]{40}) fingerprint=(?<fingerprint>[A-Za-z0-9._:/-]+) occurrence=(?<occurrence>[1-9][0-9]*) outcome=(?<outcome>fixed|dismissed|deferred) content-sha256=(?<content_sha>[0-9a-f]{64}) -->$/m;
var ROSTER_V1_MARKER = "<!-- local-review-roster:v1";
var ROSTER_V1_RE = /^<!-- local-review-roster:v1 author=(?<author>codex|claude|gemini|antigravity) reviewers=(?<reviewers>none|(?:codex|claude|gemini|antigravity)(?:,(?:codex|claude|gemini|antigravity))?) content-sha256=(?<content_sha>[0-9a-f]{64}) -->$/m;
var ROSTER_V2_MARKER = "<!-- local-review-roster:v2";
var ROSTER_V2_RE = /^<!-- local-review-roster:v2 author=(?<author>codex|claude|gemini|antigravity) reviewers=(?<reviewers>none|(?:codex|claude|gemini|antigravity)(?:,(?:codex|claude|gemini|antigravity))?) head=(?<head>[0-9a-f]{40}) supersedes=(?<supersedes>none|[1-9][0-9]*) declaration-sha256=(?<declaration_sha>[0-9a-f]{64}) -->$/m;
var ROSTER_ANY_MARKER = "<!-- local-review-roster:";
var PASS_V3_RE = /^<!-- local-review-pass:v3 engine=(?<engine>codex|claude|gemini|antigravity) round=(?<round>[1-9][0-9]*) base=(?<base>[0-9a-f]{40}) head=(?<head>[0-9a-f]{40}) result-sha256=(?<result_sha>[0-9a-f]{64}) -->$/m;
var COMPLETE_V3_RE = /^<!-- local-review-complete:v3 engine=(?<engine>codex|claude|gemini|antigravity) round=(?<round>[1-9][0-9]*) base=(?<base>[0-9a-f]{40}) before=(?<before>[0-9a-f]{40}) head=(?<head>[0-9a-f]{40}) classification=(?<classification>minor|material) fingerprints=(?<fingerprints>[A-Za-z0-9._:/,-]*) result-sha256=(?<result_sha>[0-9a-f]{64}) -->$/m;
var FINDING_V1_RE = /^<!-- local-review:v1 engine=(?<engine>codex|claude|gemini|antigravity) round=(?<round>[1-9][0-9]*) head=(?<head>[0-9a-f]{40}) fingerprint=(?<fingerprint>[A-Za-z0-9._:/-]+) -->$/m;
var DISPOSITION_V1_RE = /^<!-- local-review-disposition:v1 engine=(?<engine>codex|claude|gemini|antigravity) round=(?<round>[1-9][0-9]*) head=(?<head>[0-9a-f]{40}) fingerprint=(?<fingerprint>[A-Za-z0-9._:/-]+) outcome=(?<outcome>fixed|dismissed|deferred) -->$/m;

// src/errors.ts
var LedgerError = class extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "LedgerError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
};
function fail(message) {
  throw new LedgerError(message);
}

// src/io.ts
import { lstatSync } from "fs";
function assertRegularFile(pathValue, message) {
  try {
    const stat = lstatSync(pathValue);
    if (stat.isSymbolicLink() || !stat.isFile()) {
      fail(message);
    }
  } catch (err) {
    if (err instanceof LedgerError) throw err;
    fail(message);
  }
}
function parseJsonOrFail(raw, message) {
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new LedgerError(message, { cause: error });
  }
}

// src/protocol.ts
import { readFileSync } from "fs";

// src/hash.ts
import { createHash } from "crypto";
function sha256Text(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}
function sha256Bytes(value) {
  return createHash("sha256").update(value).digest("hex");
}
function requireToken(value, label) {
  if (!TOKEN_RE.test(value)) {
    fail(`${label} must match [A-Za-z0-9._:/-]+`);
  }
  return value;
}
function requireSha(value, name) {
  if (!SHA_RE.test(value)) {
    fail(`--${name} must be a full 40-character lowercase commit SHA`);
  }
  return value;
}

// src/protocol.ts
function readLegacyBody(path, marker, bodyText) {
  let body;
  if (bodyText !== void 0) {
    body = bodyText;
  } else if (path === "-") {
    body = readFileSync(0, "utf8");
  } else {
    body = readFileSync(path, "utf8");
  }
  if (!body.trim()) {
    fail("comment body is empty");
  }
  const markers = typeof marker === "string" ? [marker] : marker;
  const matchesMarker = markers.some((candidate) => body.includes(candidate));
  if (!matchesMarker) {
    fail("comment body lacks the required local-review marker");
  }
  return body.trimEnd();
}
function validateContentString(content) {
  if (!content.trim()) {
    fail("comment content is empty");
  }
  if (content.includes("\0")) {
    fail("comment content contains NUL");
  }
  if (content.includes("<!-- local-review")) {
    fail("comment content must not contain local-review markers");
  }
  return content;
}
function readContent(path) {
  if (path === "-") {
    fail(
      "v3 content must be a regular file; stdin and heredocs are not accepted"
    );
  }
  assertRegularFile(path, "content file must be a regular non-symlink file");
  let content;
  try {
    content = new TextDecoder("utf-8", { fatal: true }).decode(
      readFileSync(path)
    );
  } catch (error) {
    throw new LedgerError("content file must be valid UTF-8", { cause: error });
  }
  return validateContentString(content);
}
function resolveContent(params) {
  if (params.content === void 0 && params.contentFile) {
    return readContent(params.contentFile);
  }
  if (params.content !== void 0) {
    return validateContentString(params.content);
  }
  return void 0;
}
function buildFindingBody(params) {
  if (!SUPPORTED_ENGINES.includes(params.engine)) {
    fail("engine must be one of: codex, claude, gemini, antigravity");
  }
  if (!Number.isSafeInteger(params.round) || params.round < 1) {
    fail("round must be a positive integer");
  }
  if (!Number.isSafeInteger(params.occurrence) || params.occurrence < 1) {
    fail("occurrence must be a positive integer");
  }
  if (!SUPPORTED_SEVERITIES.includes(params.severity)) {
    fail("severity must be one of: blocking, major, minor, nit");
  }
  const marker = `<!-- local-review:v3 engine=${params.engine} round=${params.round} head=${requireSha(params.head, "head")} fingerprint=${requireToken(params.fingerprint, "fingerprint")} occurrence=${params.occurrence} severity=${params.severity} lens=${requireToken(params.lens, "lens")} content-sha256=${sha256Text(params.content)} -->`;
  return { marker, body: `${marker}
${params.content}` };
}
function buildDispositionBody(params) {
  if (!SUPPORTED_ENGINES.includes(params.engine)) {
    fail("engine must be one of: codex, claude, gemini, antigravity");
  }
  if (!Number.isSafeInteger(params.round) || params.round < 1) {
    fail("round must be a positive integer");
  }
  if (!Number.isSafeInteger(params.occurrence) || params.occurrence < 1) {
    fail("occurrence must be a positive integer");
  }
  if (!SUPPORTED_OUTCOMES.includes(params.outcome)) {
    fail("outcome must be one of: fixed, dismissed, deferred");
  }
  const marker = `<!-- local-review-disposition:v3 engine=${params.engine} round=${params.round} head=${requireSha(params.head, "head")} fingerprint=${requireToken(params.fingerprint, "fingerprint")} occurrence=${params.occurrence} outcome=${params.outcome} content-sha256=${sha256Text(params.content)} -->`;
  return { marker, body: `${marker}
${params.content}` };
}
function matchProtocol(body, pattern, marker) {
  const matched = matchMarkerLine(body, pattern, marker);
  if (matched === null) {
    return null;
  }
  if (sha256Text(matched.content) !== matched.match.groups["content_sha"]) {
    fail(`authenticated ${marker} record has an invalid content hash`);
  }
  return matched.match;
}
function matchMarkerLine(body, pattern, marker) {
  if (!body.includes(marker)) {
    return null;
  }
  const match = pattern.exec(body);
  if (!match || !match.groups) {
    fail(`authenticated ${marker} record is malformed`);
  }
  const matchEnd = match.index + match[0].length;
  if (match.index !== 0 || !body.slice(matchEnd).startsWith("\n")) {
    fail(`authenticated ${marker} record is not the first complete line`);
  }
  const content = body.slice(matchEnd + 1);
  if (!content.trim()) {
    fail(`authenticated ${marker} content is empty`);
  }
  return { match, content };
}
function verifyV1Marker(body, match, label) {
  const matchEnd = match.index + match[0].length;
  if (match.index !== 0 || !body.slice(matchEnd).startsWith("\n")) {
    fail(`actor-owned v1 ${label} marker is malformed`);
  }
  if (!body.slice(matchEnd + 1).trim()) {
    fail(`actor-owned v1 ${label} content is empty`);
  }
}
function matchPseudoV3(body) {
  const regex = new RegExp(PSEUDO_V3_RE.source, "gm");
  const matches = [];
  let m;
  while ((m = regex.exec(body)) !== null) {
    matches.push(m);
  }
  if (matches.length === 0) {
    return null;
  }
  const markerCount = (body.match(/<!-- local-review/g) || []).length;
  const first = matches[0];
  if (matches.length !== 1 || markerCount !== 1 || first.index === 0 || body[first.index - 1] !== "\n" || first.index + first[0].length !== body.length || !body.slice(0, first.index).trim()) {
    fail("authenticated historical local-review:v3 record is malformed");
  }
  return {
    fingerprint: first.groups["fingerprint"],
    outcome: first[0].includes("outcome=deferred") ? "deferred" : void 0
  };
}
function matchFinding(body) {
  if (matchPseudoV3(body) !== null) {
    return null;
  }
  const match = matchProtocol(body, FINDING_V3_RE, "<!-- local-review:v3");
  if (!match || !match.groups) {
    return null;
  }
  return {
    engine: match.groups["engine"],
    round: parseInt(match.groups["round"], 10),
    head: match.groups["head"],
    fingerprint: match.groups["fingerprint"],
    occurrence: parseInt(match.groups["occurrence"], 10),
    severity: match.groups["severity"],
    lens: match.groups["lens"],
    contentSha: match.groups["content_sha"]
  };
}
function matchDisposition(body) {
  const match = matchProtocol(
    body,
    DISPOSITION_V3_RE,
    "<!-- local-review-disposition:v3"
  );
  if (!match || !match.groups) {
    return null;
  }
  return {
    engine: match.groups["engine"],
    round: parseInt(match.groups["round"], 10),
    head: match.groups["head"],
    fingerprint: match.groups["fingerprint"],
    occurrence: parseInt(match.groups["occurrence"], 10),
    outcome: match.groups["outcome"],
    contentSha: match.groups["content_sha"]
  };
}

// src/ledger.ts
import { execFileSync as execFileSync2 } from "child_process";

// src/github.ts
import { execFileSync } from "child_process";
import { readFileSync as readFileSync2 } from "fs";
var defaultActor = null;
function execFailureDetail(error) {
  const execError = error;
  if (execError.code === "ENOBUFS") {
    return `output exceeded the ${SUBPROCESS_MAX_BUFFER}-byte subprocess buffer`;
  }
  return execError.stderr?.trim() || "no diagnostic returned";
}
var DefaultGitHubRunner = class {
  actor = null;
  constructor(customActor) {
    if (customActor) {
      this.actor = requireToken(customActor, "actor");
    }
  }
  setActor(actor) {
    this.actor = actor ? requireToken(actor, "actor") : null;
  }
  runGh(args, payload) {
    const command = "gh";
    const finalArgs = [...args];
    let input;
    if (payload !== void 0) {
      finalArgs.push("--input", "-");
      input = JSON.stringify(payload);
    }
    try {
      const stdout = execFileSync(command, finalArgs, {
        input,
        encoding: "utf8",
        stdio: ["pipe", "pipe", "pipe"],
        maxBuffer: SUBPROCESS_MAX_BUFFER
      });
      return stdout;
    } catch (error) {
      fail(`GitHub operation failed: ${execFailureDetail(error)}`);
    }
  }
  currentActor() {
    if (this.actor !== null) {
      return this.actor;
    }
    this.actor = resolveLoginOrFail(this.runGh(["api", "user"]));
    return this.actor;
  }
  gitCompare(repo, before, after) {
    const raw = this.runGh([
      "api",
      `repos/${repo}/compare/${before}...${after}`
    ]);
    return parseJsonOrFail(raw, "GitHub returned invalid JSON");
  }
  gitRevList(before, head) {
    try {
      const stdout = execFileSync(
        "git",
        ["rev-list", "--reverse", "--ancestry-path", `${before}..${head}`],
        {
          encoding: "utf8",
          stdio: ["pipe", "pipe", "pipe"],
          maxBuffer: SUBPROCESS_MAX_BUFFER
        }
      );
      return stdout.trim().split(/\r?\n/).filter(Boolean);
    } catch {
      fail("could not derive the forward review transition");
    }
  }
  runGit(args) {
    try {
      return execFileSync("git", args, {
        encoding: "utf8",
        stdio: ["pipe", "pipe", "pipe"],
        maxBuffer: SUBPROCESS_MAX_BUFFER
      });
    } catch (error) {
      fail(`Git operation failed: ${execFailureDetail(error)}`);
    }
  }
  isAncestor(ancestor, descendant) {
    try {
      execFileSync(
        "git",
        ["merge-base", "--is-ancestor", ancestor, descendant],
        {
          encoding: "utf8",
          stdio: ["pipe", "pipe", "pipe"],
          maxBuffer: SUBPROCESS_MAX_BUFFER
        }
      );
      return true;
    } catch (error) {
      const execError = error;
      if (execError.status === 1) {
        return false;
      }
      fail(`Git ancestry check failed: ${execFailureDetail(error)}`);
    }
  }
};
var activeRunner = new DefaultGitHubRunner();
function getGitHubRunner() {
  return activeRunner;
}
function resetGitHubRunner(actor) {
  defaultActor = actor ?? null;
  activeRunner = new DefaultGitHubRunner(actor);
}
function runGh(args, payload) {
  return activeRunner.runGh(args, payload);
}
function jsonOutput(args, payload) {
  const raw = runGh(args, payload);
  return parseJsonOrFail(raw, "GitHub returned invalid JSON");
}
function compareIsForward(repo, before, after) {
  const comparison = activeRunner.gitCompare ? activeRunner.gitCompare(repo, before, after) : jsonOutput([
    "api",
    `repos/${repo}/compare/${before}...${after}`
  ]);
  const mergeBase = comparison?.["merge_base_commit"];
  return typeof comparison === "object" && comparison !== null && comparison["status"] === "ahead" && typeof mergeBase === "object" && mergeBase !== null && mergeBase.sha === before;
}
function resolveLoginOrFail(raw) {
  const response = parseJsonOrFail(
    raw,
    "GitHub returned an invalid authenticated-user response"
  );
  const login = response?.["login"];
  if (typeof response !== "object" || response === null || typeof login !== "string") {
    fail("GitHub returned an invalid authenticated-user response");
  }
  if (!login) {
    fail("GitHub returned an empty authenticated user");
  }
  return login;
}
function currentActor() {
  let login;
  if (activeRunner.currentActor) {
    login = activeRunner.currentActor();
  } else {
    if (defaultActor === null) {
      defaultActor = resolveLoginOrFail(runGh(["api", "user"]));
    }
    login = defaultActor;
  }
  if (!login) {
    fail("GitHub returned an empty authenticated user");
  }
  const expected = process.env[EXPECTED_ACTOR_ENV];
  if (expected && login !== expected) {
    fail(
      `authenticated GitHub actor changed: expected ${expected}, found ${login}`
    );
  }
  return login;
}
function assertActor(expected) {
  const actor = currentActor();
  if (expected !== void 0 && requireToken(expected, "actor") !== actor) {
    fail(
      `authenticated GitHub actor changed: expected ${expected}, found ${actor}`
    );
  }
  return actor;
}
function authenticatedRows(rows, options) {
  const actor = currentActor();
  return rows.filter((row) => {
    const user = row["user"];
    const author = row["author"];
    const identity = options?.graphql ? author ?? user : user ?? author;
    return typeof identity === "object" && identity !== null && identity.login === actor;
  });
}
function verifyHead(repo, pr, expectedHead) {
  const actual = runGh([
    "pr",
    "view",
    String(pr),
    "--repo",
    repo,
    "--json",
    "headRefOid",
    "--jq",
    ".headRefOid"
  ]).trim();
  if (actual !== expectedHead) {
    fail(
      `PR head mismatch: expected ${expectedHead}, found ${actual || "<empty>"}`
    );
  }
}
function runGit(args) {
  const runner = getGitHubRunner();
  if (!runner.runGit) {
    fail("Git operations are unavailable in the active runner");
  }
  return runner.runGit(args);
}
function isAncestor(ancestor, descendant) {
  const runner = getGitHubRunner();
  if (!runner.isAncestor) {
    fail("Git operations are unavailable in the active runner");
  }
  return runner.isAncestor(ancestor, descendant);
}
function verifyReviewBase(repo, pr, base, before) {
  const resolved = runGit(["rev-parse", "--verify", `${base}^{commit}`]).trim();
  if (resolved !== base) {
    fail("review base did not resolve to the supplied commit");
  }
  if (!isAncestor(base, before)) {
    fail("review base is not an ancestor of beforeSha");
  }
  const prBase = runGh([
    "pr",
    "view",
    String(pr),
    "--repo",
    repo,
    "--json",
    "baseRefOid",
    "--jq",
    ".baseRefOid"
  ]).trim();
  if (prBase !== base) {
    fail(`PR base mismatch: expected ${base}, found ${prBase || "<empty>"}`);
  }
}
function verifyGitTransition(before, resultHead2, liveHead) {
  const localHead = runGit(["rev-parse", "HEAD"]).trim();
  if (localHead !== liveHead) {
    fail(
      `local HEAD mismatch: expected ${liveHead}, found ${localHead || "<empty>"}`
    );
  }
  if (!isAncestor(before, resultHead2)) {
    fail("review result rewrites or does not descend from beforeSha");
  }
  if (!isAncestor(resultHead2, liveHead)) {
    fail("review result head is not an ancestor of the live head");
  }
}
function flattenPages(value, label) {
  if (!Array.isArray(value)) {
    fail(`GitHub ${label} response has an unexpected shape`);
  }
  const rows = [];
  for (const page of value) {
    if (!Array.isArray(page)) {
      fail(`GitHub ${label} page has an unexpected shape`);
    }
    for (const item of page) {
      if (typeof item !== "object" || item === null) {
        fail(`GitHub ${label} item has an unexpected shape`);
      }
      rows.push(item);
    }
  }
  return rows;
}
function getPrFiles(repo, pr) {
  const pages = jsonOutput([
    "api",
    "--paginate",
    "--slurp",
    `repos/${repo}/pulls/${pr}/files?per_page=100`
  ]);
  const rows = flattenPages(
    pages,
    "PR-files"
  );
  const files = /* @__PURE__ */ Object.create(null);
  for (const item of rows) {
    if (typeof item.filename !== "string") {
      fail("GitHub PR-files item has an unexpected shape");
    }
    files[item.filename] = typeof item.patch === "string" ? item.patch : null;
  }
  return files;
}
function getReviewComments(repo, pr) {
  const pages = jsonOutput([
    "api",
    "--paginate",
    "--slurp",
    `repos/${repo}/pulls/${pr}/comments?per_page=100`
  ]);
  const rows = flattenPages(pages, "review-comments");
  return authenticatedRows(rows);
}
function getIssueComments(repo, pr) {
  const pages = jsonOutput([
    "api",
    "--paginate",
    "--slurp",
    `repos/${repo}/issues/${pr}/comments?per_page=100`
  ]);
  const rows = flattenPages(pages, "PR-comments");
  return authenticatedRows(rows);
}
function verifyComment(repo, commentId, expectedBody) {
  verifyOwnedComment(
    `repos/${repo}/pulls/comments/${commentId}`,
    commentId,
    expectedBody,
    "review comment"
  );
}
function verifyIssueComment(repo, commentId, expectedBody) {
  verifyOwnedComment(
    `repos/${repo}/issues/comments/${commentId}`,
    commentId,
    expectedBody,
    "PR comment"
  );
}
function verifyOwnedComment(endpoint, commentId, expectedBody, label) {
  const response = jsonOutput(["api", endpoint]);
  const user = response["user"] ?? response["author"];
  if (typeof response !== "object" || response === null || response["body"] !== expectedBody || typeof user !== "object" || user === null || user.login !== currentActor()) {
    fail(`could not verify ${label} ${commentId} after posting`);
  }
}
function findMatchingAttestation(rows, engine, round, body) {
  const prefixes = [
    `<!-- local-review-pass:v3 engine=${engine} round=${round} `,
    `<!-- local-review-complete:v3 engine=${engine} round=${round} `
  ];
  const matches = rows.filter(
    (row2) => prefixes.some((prefix) => String(row2["body"] ?? "").startsWith(prefix))
  );
  if (matches.length === 0) {
    return null;
  }
  if (matches.length !== 1) {
    fail("local-review attestation identity is duplicated");
  }
  const row = matches[0];
  if (row["body"] !== body || typeof row["id"] !== "number") {
    fail("local-review attestation identity conflicts with existing evidence");
  }
  return row["id"];
}
function issueCommentExists(repo, pr, commentId) {
  return getIssueComments(repo, pr).some((row) => row["id"] === commentId);
}
function deleteIssueComment(repo, pr, commentId) {
  try {
    runGh([
      "api",
      "-X",
      "DELETE",
      `repos/${repo}/issues/comments/${commentId}`
    ]);
  } catch (error) {
    if (issueCommentExists(repo, pr, commentId)) {
      throw error;
    }
    return;
  }
  if (issueCommentExists(repo, pr, commentId)) {
    fail(`could not verify rollback of PR comment ${commentId}`);
  }
}
function getPostedCommentId(response) {
  if (typeof response !== "object" || response === null || typeof response.id !== "number") {
    fail("GitHub accepted the mutation but returned no comment ID");
  }
  return response.id;
}
function findMatchingBody(rows, marker, body) {
  const matches = rows.filter(
    (row2) => String(row2["body"] ?? "").includes(marker)
  );
  if (matches.length === 0) {
    return null;
  }
  if (matches.length !== 1) {
    fail("ledger idempotency key is duplicated");
  }
  const row = matches[0];
  if (row["body"] !== body || typeof row["id"] !== "number") {
    fail("ledger idempotency key already exists with conflicting content");
  }
  return row["id"];
}
function postReviewComment(repo, pr, head, marker, body, options) {
  const existing = findMatchingBody(
    getReviewComments(repo, pr),
    marker,
    body
  );
  if (existing !== null) {
    verifyComment(repo, existing, body);
    verifyHead(repo, pr, head);
    return { commentId: existing, replayed: true };
  }
  let endpoint;
  let payload;
  if (options.replyTo === void 0) {
    payload = { body, commit_id: head, path: options.path };
    if (options.fileLevel) {
      payload["subject_type"] = "file";
    } else {
      payload["line"] = options.line;
      payload["side"] = options.side;
    }
    endpoint = `repos/${repo}/pulls/${pr}/comments`;
  } else {
    payload = { body };
    endpoint = `repos/${repo}/pulls/${pr}/comments/${options.replyTo}/replies`;
  }
  let replayed = false;
  let commentId;
  try {
    const response = jsonOutput(["api", "-X", "POST", endpoint], payload);
    commentId = getPostedCommentId(response);
  } catch (error) {
    if (error instanceof LedgerError) {
      const recovered = findMatchingBody(
        getReviewComments(repo, pr),
        marker,
        body
      );
      if (recovered === null) {
        throw error;
      }
      commentId = recovered;
      replayed = true;
    } else {
      throw error;
    }
  }
  verifyComment(repo, commentId, body);
  verifyHead(repo, pr, head);
  return { commentId, replayed };
}
function getThreadState(threadId, commentId, scope) {
  const query = `
query($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      repository { nameWithOwner }
      pullRequest { number }
      comments(first: 100) {
        nodes { databaseId }
        pageInfo { hasNextPage }
      }
    }
  }
}
`.trim();
  const response = jsonOutput(
    ["api", "graphql"],
    { query, variables: { threadId } }
  );
  const thread = response?.data?.node;
  if (typeof thread !== "object" || thread === null || thread.id !== threadId) {
    fail(`could not verify review thread ${threadId}`);
  }
  if (scope) {
    const repository = thread.repository;
    const pullRequest = thread.pullRequest;
    if (typeof repository !== "object" || repository === null || repository.nameWithOwner !== scope.repo || typeof pullRequest !== "object" || pullRequest === null || pullRequest.number !== scope.pr) {
      fail(
        `review thread ${threadId} does not belong to ${scope.repo}#${scope.pr}`
      );
    }
  }
  if (typeof thread.isResolved !== "boolean") {
    fail(`review thread ${threadId} has invalid resolution state`);
  }
  if (commentId !== void 0) {
    const comments = thread.comments;
    if (typeof comments !== "object" || comments === null) {
      fail("review thread read-back omitted comments");
    }
    const pageInfo = comments.pageInfo;
    const nodes = comments.nodes;
    if (typeof pageInfo !== "object" || pageInfo === null || pageInfo.hasNextPage !== false || !Array.isArray(nodes)) {
      fail("review thread comments are incomplete");
    }
    const ids = nodes.map((node) => node.databaseId);
    if (ids.length === 0 || ids[0] !== commentId) {
      fail("--comment-id is not the root comment of --thread-id");
    }
  }
  return thread.isResolved;
}
function setThreadState(threadId, resolved, commentId, scope) {
  if (getThreadState(threadId, commentId, scope) === resolved) {
    return true;
  }
  const field = resolved ? "resolveReviewThread" : "unresolveReviewThread";
  const mutation = `
mutation($threadId: ID!) {
  ${field}(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
`.trim();
  try {
    const response = jsonOutput(["api", "graphql"], { query: mutation, variables: { threadId } });
    const thread = response?.["data"]?.[field]?.["thread"];
    if (typeof thread !== "object" || thread === null) {
      fail("GitHub returned an invalid thread mutation response");
    }
    if (thread.id !== threadId || thread.isResolved !== resolved) {
      fail(`GitHub did not set review thread ${threadId} resolved=${resolved}`);
    }
  } catch (error) {
    let verifiedAfterError = false;
    try {
      if (getThreadState(threadId, commentId, scope) === resolved) {
        verifiedAfterError = true;
      }
    } catch {
    }
    if (verifiedAfterError) {
      return false;
    }
    if (error instanceof LedgerError) {
      throw error;
    }
    throw new LedgerError(
      "GitHub returned an invalid thread mutation response",
      { cause: error }
    );
  }
  let verified;
  try {
    verified = getThreadState(threadId, commentId, scope);
  } catch {
    verified = getThreadState(threadId, commentId, scope);
  }
  if (verified !== resolved) {
    fail(`could not verify review thread ${threadId} resolved=${resolved}`);
  }
  return false;
}
function parseReviewThreadPages(pages, options) {
  const requireFullComments = options?.requireFullComments ?? true;
  if (!Array.isArray(pages)) {
    fail("GitHub review-thread response has an unexpected shape");
  }
  if (pages.length === 0) {
    fail("GitHub review-thread response is empty");
  }
  const threads = [];
  for (let pageIndex = 0; pageIndex < pages.length; pageIndex++) {
    const page = pages[pageIndex];
    if (typeof page !== "object" || page === null || page.errors) {
      fail("GitHub review-thread response is incomplete");
    }
    const connection = page.data?.repository?.pullRequest?.reviewThreads;
    if (!connection || !Array.isArray(connection.nodes) || typeof connection.pageInfo !== "object" || connection.pageInfo === null) {
      fail("GitHub review-thread nodes have an unexpected shape");
    }
    const expectedMore = pageIndex < pages.length - 1;
    if (connection.pageInfo.hasNextPage !== expectedMore) {
      fail("GitHub review-thread pagination is incomplete");
    }
    if (expectedMore && typeof connection.pageInfo.endCursor !== "string") {
      fail("GitHub review-thread pagination omitted its cursor");
    }
    for (const thread of connection.nodes) {
      if (typeof thread !== "object" || thread === null) {
        fail("GitHub review thread has an unexpected shape");
      }
      const comments = thread.comments;
      if (!comments || !Array.isArray(comments.nodes) || typeof comments.pageInfo !== "object" || comments.pageInfo === null || typeof comments.pageInfo.hasNextPage !== "boolean" || requireFullComments && comments.pageInfo.hasNextPage !== false) {
        fail("GitHub review-thread comments are incomplete");
      }
      threads.push(thread);
    }
  }
  return threads;
}
function loadReviewThreads(pathValue, expectedDigest) {
  assertRegularFile(
    pathValue,
    "review-thread snapshot must be a regular non-symlink file"
  );
  const digest = expectedDigest ?? process.env[EXPECTED_THREADS_SHA256_ENV];
  if (digest === void 0 || !SHA_64_RE.test(digest)) {
    fail("review-thread snapshot requires a sealed SHA-256 digest");
  }
  const raw = readFileSync2(pathValue);
  if (sha256Bytes(raw) !== digest) {
    fail("review-thread snapshot changed after it was sealed");
  }
  const pages = parseJsonOrFail(
    raw.toString("utf8"),
    "review-thread snapshot must contain valid UTF-8 JSON"
  );
  return parseReviewThreadPages(pages);
}
function assertThreadScope(threads, repo, pr) {
  for (const thread of threads) {
    const repository = thread.repository;
    const pullRequest = thread.pullRequest;
    if (typeof repository !== "object" || repository === null || repository.nameWithOwner !== repo || typeof pullRequest !== "object" || pullRequest === null || pullRequest.number !== pr) {
      fail("GitHub returned a review thread outside the requested PR");
    }
  }
}
function reviewThreads(repo, pr, threadsFile, expectedDigest) {
  const threads = threadsFile === void 0 ? fetchReviewThreads(repo, pr) : loadReviewThreads(threadsFile, expectedDigest);
  assertThreadScope(threads, repo, pr);
  return threads;
}
function fetchReviewThreads(repo, pr) {
  const parts = repo.split("/");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    fail("--repo must be OWNER/REPO");
  }
  const [owner, name] = parts;
  const query = `
query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$endCursor) {
        nodes {
          id
          isResolved
          repository { nameWithOwner }
          pullRequest { number }
          comments(first:100) {
            nodes { databaseId body author { login } }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
`.trim();
  const pages = jsonOutput([
    "api",
    "graphql",
    "--paginate",
    "--slurp",
    "-f",
    `query=${query}`,
    "-f",
    `owner=${owner}`,
    "-f",
    `name=${name}`,
    "-F",
    `number=${pr}`
  ]);
  return parseReviewThreadPages(pages);
}
function findRootThread(repo, pr, rootCommentId) {
  const parts = repo.split("/");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    fail("--repo must be OWNER/REPO");
  }
  const [owner, name] = parts;
  const query = `
query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$endCursor) {
        nodes {
          id
          isResolved
          repository { nameWithOwner }
          pullRequest { number }
          comments(first:1) {
            nodes { databaseId body author { login } }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
`.trim();
  const pages = jsonOutput([
    "api",
    "graphql",
    "--paginate",
    "--slurp",
    "-f",
    `query=${query}`,
    "-f",
    `owner=${owner}`,
    "-f",
    `name=${name}`,
    "-F",
    `number=${pr}`
  ]);
  const threads = parseReviewThreadPages(pages, { requireFullComments: false });
  assertThreadScope(threads, repo, pr);
  const matching = threads.filter(
    (thread) => thread.comments.nodes[0]?.databaseId === rootCommentId
  );
  if (matching.length > 1) {
    fail("could not identify exactly one root review thread");
  }
  return matching[0] ?? null;
}
function loadHistoricalCommentIds(pathValue) {
  const finalPath = pathValue || process.env[HISTORICAL_COMMENT_IDS_ENV];
  if (!finalPath) {
    return /* @__PURE__ */ new Set();
  }
  assertRegularFile(
    finalPath,
    "historical comment IDs must be a regular non-symlink file"
  );
  const values = parseJsonOrFail(
    readFileSync2(finalPath, "utf8"),
    "historical comment IDs must contain valid UTF-8 JSON"
  );
  if (!Array.isArray(values) || values.some(
    (v) => typeof v !== "number" || !Number.isInteger(v) || v < 1
  ) || new Set(values).size !== values.length) {
    fail("historical comment IDs must be unique positive integers");
  }
  return new Set(values);
}
function verifyPseudoV3History(threads, historicalCommentIds) {
  const actor = currentActor();
  for (const thread of threads) {
    const comments = thread.comments;
    if (!comments || !Array.isArray(comments.nodes) || !comments.pageInfo || comments.pageInfo.hasNextPage !== false) {
      fail("GitHub review thread comments are incomplete");
    }
    const nodes = comments.nodes;
    for (let index = 0; index < nodes.length; index++) {
      const row = nodes[index];
      const body = String(row.body ?? "");
      const author = row.author;
      if (!author || typeof author.login !== "string") {
        if (body.includes("<!-- local-review")) {
          fail("could not establish local-review comment ownership");
        }
        continue;
      }
      if (author.login !== actor) {
        continue;
      }
      if (!body.includes("<!-- local-review:v3")) {
        continue;
      }
      const pseudo = matchPseudoV3(body);
      if (pseudo === null) {
        matchProtocol(body, FINDING_V3_RE, "<!-- local-review:v3");
        continue;
      }
      if (historicalCommentIds !== void 0 && (typeof row.databaseId !== "number" || !historicalCommentIds.has(row.databaseId))) {
        fail(
          "historical local-review:v3 finding was not captured before the current pass"
        );
      }
      const laterSameActor = nodes.slice(index + 1).some((reply2) => {
        const replyAuthor = reply2.author;
        const replyBody = String(reply2.body ?? "").trim();
        return replyAuthor && replyAuthor.login === actor && Boolean(replyBody) && !replyBody.includes("<!-- local-review");
      });
      if (index !== 0 || typeof thread.id !== "string" || thread.isResolved !== true || !laterSameActor) {
        fail(
          "historical local-review:v3 finding is not settled actor-owned history"
        );
      }
    }
  }
}
function loadAllowedHeads(path, before, afterSha, repo) {
  assertRegularFile(
    path,
    "allowed transition heads must be a regular non-symlink file"
  );
  const values = parseJsonOrFail(
    readFileSync2(path, "utf8"),
    "allowed transition heads must contain valid UTF-8 JSON"
  );
  if (!Array.isArray(values) || values.length === 0 || values.some((v) => typeof v !== "string" || !SHA_RE.test(v)) || new Set(values).size !== values.length || values[0] !== before || values[values.length - 1] !== afterSha) {
    fail(
      "allowed transition heads do not match the observed review transition"
    );
  }
  const headList = values;
  for (let i = 0; i < headList.length - 1; i++) {
    const cur = headList[i];
    const nxt = headList[i + 1];
    if (!compareIsForward(repo, cur, nxt)) {
      fail("allowed transition heads are not forward-only");
    }
  }
  const allowed = {};
  headList.forEach((val, idx) => {
    allowed[val] = idx;
  });
  return allowed;
}
function rowsHaveHistoricalMarkers(rows) {
  return rows.some((row) => {
    const body = String(row.body ?? "");
    if (body.includes(FINDING_V1) || body.includes(DISPOSITION_V1)) {
      return true;
    }
    if (body.includes(FINDING_V3_OPENER) && !FINDING_V3_RE.test(body)) {
      return true;
    }
    return PSEUDO_V3_RE.test(body);
  });
}

// src/diff.ts
function parseDiffLines(patch) {
  const leftLines = /* @__PURE__ */ new Set();
  const rightLines = /* @__PURE__ */ new Set();
  let left = 0;
  let right = 0;
  let inHunk = false;
  const lines = patch.split(/\r?\n/);
  for (const rawLine of lines) {
    const match = HUNK_WITH_LEFT_RE.exec(rawLine);
    if (match?.groups) {
      left = parseInt(match.groups["left"], 10);
      right = parseInt(match.groups["right"], 10);
      inHunk = true;
      continue;
    }
    if (!inHunk || rawLine.startsWith("\\ No newline")) {
      continue;
    }
    const prefix = rawLine.slice(0, 1);
    if (prefix === " ") {
      leftLines.add(left);
      rightLines.add(right);
      left += 1;
      right += 1;
    } else if (prefix === "-") {
      leftLines.add(left);
      left += 1;
    } else if (prefix === "+") {
      rightLines.add(right);
      right += 1;
    }
  }
  return { leftLines, rightLines };
}
function validateAnchor(files, path, line, side) {
  if (!Object.prototype.hasOwnProperty.call(files, path)) {
    fail(`path is not part of the PR diff: ${path}`);
  }
  if (line === null || line === void 0) {
    return;
  }
  const resolvedSide = side ?? "RIGHT";
  if (!SUPPORTED_SIDES.includes(resolvedSide)) {
    fail(`side must be one of: ${SUPPORTED_SIDES.join(", ")}`);
  }
  const patch = files[path];
  if (patch === null || patch === void 0) {
    fail("GitHub omitted the file patch; use --file-level only if defensible");
  }
  const { leftLines, rightLines } = parseDiffLines(patch);
  const valid = resolvedSide === "RIGHT" ? rightLines : leftLines;
  if (valid.has(line)) {
    return;
  }
  const sortedCandidates = Array.from(valid).sort((a, b) => {
    const diffA = Math.abs(a - line);
    const diffB = Math.abs(b - line);
    if (diffA !== diffB) {
      return diffA - diffB;
    }
    return a - b;
  });
  const nearest = sortedCandidates.slice(0, 5);
  const candidates = nearest.join(", ") || "none";
  fail(
    `line ${line} is not an exact ${resolvedSide} anchor in GitHub's PR patch; nearest valid lines: ${candidates}`
  );
}

// src/result.ts
import {
  closeSync,
  fchmodSync,
  fsyncSync,
  lstatSync as lstatSync2,
  mkdirSync,
  openSync,
  readFileSync as readFileSync3,
  renameSync,
  unlinkSync,
  writeSync
} from "fs";
import { dirname, join } from "path";
import { randomBytes } from "crypto";
function readResultBytes(pathValue) {
  assertRegularFile(
    pathValue,
    "review result must be a regular non-symlink file"
  );
  return readFileSync3(pathValue);
}
function resultHead(args) {
  return args.resultHead || args.head;
}
function validateResultData(args, rawInput) {
  const raw = rawInput !== void 0 ? typeof rawInput === "string" ? Buffer.from(rawInput, "utf8") : rawInput : void 0;
  if (raw === void 0) {
    fail("review result raw content is required");
  }
  const data = parseJsonOrFail(
    raw.toString("utf8"),
    "review result must contain valid UTF-8 JSON"
  );
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    fail("review result must be a JSON object");
  }
  const required = /* @__PURE__ */ new Set([
    "version",
    "status",
    "engine",
    "round",
    "baseSha",
    "beforeSha",
    "afterSha",
    "classification",
    "findingFingerprints",
    "finalLaneComplete"
  ]);
  const allowed = /* @__PURE__ */ new Set([...required, "blocker"]);
  const keys = new Set(Object.keys(data));
  const isExactRequired = keys.size === required.size && [...keys].every((k) => required.has(k));
  const isExactAllowed = keys.size === allowed.size && [...keys].every((k) => allowed.has(k));
  if (!isExactRequired && !isExactAllowed) {
    fail("review result has missing or unknown fields");
  }
  if (typeof data["version"] !== "number" || !Number.isInteger(data["version"]) || typeof data["round"] !== "number" || !Number.isInteger(data["round"])) {
    fail("review result version and round must be integers");
  }
  const expectedHead = resultHead(args);
  const expected = {
    version: PROTOCOL_VERSION,
    engine: args.engine,
    round: args.round,
    baseSha: args.base,
    beforeSha: args.before,
    afterSha: expectedHead
  };
  for (const [key, value] of Object.entries(expected)) {
    if (data[key] !== value) {
      fail(`review result ${key} mismatch`);
    }
  }
  if (!SUPPORTED_ENGINES.some((engine) => engine === data["engine"]) || data["round"] < 1 || typeof data["baseSha"] !== "string" || !SHA_RE.test(data["baseSha"]) || typeof data["beforeSha"] !== "string" || !SHA_RE.test(data["beforeSha"]) || typeof data["afterSha"] !== "string" || !SHA_RE.test(data["afterSha"])) {
    fail("review result identity fields are invalid");
  }
  const status = data["status"];
  if (status !== "clean" && status !== "changed" && status !== "blocked") {
    fail("review result status must be clean, changed, or blocked");
  }
  const fingerprints = data["findingFingerprints"];
  if (!Array.isArray(fingerprints) || fingerprints.some(
    (value) => typeof value !== "string" || !TOKEN_RE.test(value)
  ) || new Set(fingerprints).size !== fingerprints.length) {
    fail("review result findingFingerprints must be unique protocol tokens");
  }
  if (typeof data["finalLaneComplete"] !== "boolean") {
    fail("review result finalLaneComplete must be boolean");
  }
  const classification = data["classification"];
  if (status === "clean") {
    if (args.before !== expectedHead || classification !== null || data["finalLaneComplete"] !== true || "blocker" in data) {
      fail("clean review result conflicts with the observed pass");
    }
  } else if (status === "changed") {
    if (args.before === expectedHead || classification !== "minor" && classification !== "material" || args.round >= 3 && classification !== "material" || fingerprints.length === 0 || data["finalLaneComplete"] !== true || "blocker" in data) {
      fail("changed review result conflicts with the observed pass");
    }
  } else {
    const blocker = data["blocker"];
    if (classification !== null || data["finalLaneComplete"] !== false || typeof blocker !== "string" || !blocker.trim() || blocker.includes("<!-- local-review")) {
      fail("blocked review result lacks a safe blocker");
    }
  }
  return data;
}
function writeResultFile(pathValue, value) {
  const sortedKeys = Object.keys(value).sort();
  const sortedObj = {};
  for (const k of sortedKeys) {
    sortedObj[k] = value[k];
  }
  const raw = Buffer.from(JSON.stringify(sortedObj) + "\n", "utf8");
  try {
    const stat = lstatSync2(pathValue);
    if (stat.isSymbolicLink() || !stat.isFile()) {
      fail("review result destination must be a regular non-symlink file");
    }
  } catch (err) {
    if (err instanceof LedgerError) throw err;
  }
  const targetDir = dirname(pathValue);
  mkdirSync(targetDir, { recursive: true });
  const tempName = `.tmp-${randomBytes(8).toString("hex")}-${Date.now()}`;
  const tempPath = join(targetDir, tempName);
  let fd = null;
  try {
    fd = openSync(tempPath, "wx", 384);
    fchmodSync(fd, 384);
    writeSync(fd, raw);
    fsyncSync(fd);
    closeSync(fd);
    fd = null;
    renameSync(tempPath, pathValue);
  } finally {
    if (fd !== null) {
      try {
        closeSync(fd);
      } catch {
      }
    }
    try {
      unlinkSync(tempPath);
    } catch {
    }
  }
}
function readResult(resultFile) {
  const raw = readResultBytes(resultFile);
  const parsed = parseJsonOrFail(
    Buffer.from(raw).toString("utf8"),
    "review result must contain valid UTF-8 JSON"
  );
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    fail("review result must be a JSON object");
  }
  const candidate = parsed;
  const engine = candidate["engine"];
  const round = candidate["round"];
  const base = candidate["baseSha"];
  const before = candidate["beforeSha"];
  const head = candidate["afterSha"];
  if (typeof engine !== "string" || typeof round !== "number" || typeof base !== "string" || typeof before !== "string" || typeof head !== "string") {
    fail("review result has missing or invalid identity fields");
  }
  const data = validateResultData({ engine, round, base, before, head }, raw);
  data.resultSha256 = sha256Bytes(raw);
  return data;
}
function validateResult(params) {
  const raw = readResultBytes(params.resultFile);
  const data = validateResultData(params, raw);
  return { ...data, resultSha256: sha256Bytes(raw), verified: true };
}
function writeBlockedResult(params) {
  let blocker;
  if (params.blocker !== void 0) {
    blocker = validateContentString(params.blocker).trim();
  } else if (params.blockerFile) {
    blocker = readContent(params.blockerFile).trim();
  } else {
    fail("blocked review result requires a blocker file or content");
  }
  const value = {
    version: PROTOCOL_VERSION,
    status: "blocked",
    engine: params.engine,
    round: params.round,
    baseSha: params.base,
    beforeSha: params.before,
    afterSha: params.head,
    classification: null,
    findingFingerprints: [],
    finalLaneComplete: false,
    blocker
  };
  writeResultFile(params.resultFile, value);
  const raw = readResultBytes(params.resultFile);
  validateResultData(params, raw);
  return {
    ...value,
    resultSha256: sha256Bytes(raw)
  };
}

// src/ledger.ts
function threadProtocolRecords(thread, historicalCommentIds) {
  verifyPseudoV3History([thread], historicalCommentIds);
  const comments = thread.comments.nodes;
  const actor = currentActor();
  const findingsV3 = [];
  const dispositionsV3 = [];
  const findingsV1 = [];
  const dispositionsV1 = [];
  for (let index = 0; index < comments.length; index++) {
    const row = comments[index];
    const body = String(row.body ?? "");
    const author = row.author ?? row.user;
    if (!author || typeof author.login !== "string") {
      if (body.includes("<!-- local-review")) {
        fail("could not establish local-review comment ownership");
      }
      continue;
    }
    if (author.login !== actor) {
      continue;
    }
    const firstLine = body.split("\n", 1)[0].replace(/\r$/, "");
    const pseudo = matchPseudoV3(body);
    const findingV3 = matchFinding(body);
    const dispositionV3 = matchDisposition(body);
    if (body.includes("<!-- local-review:v3") && !findingV3 && !pseudo) {
      fail("actor-owned local-review:v3 marker is malformed or unsupported");
    }
    const findingV1 = FINDING_V1_RE.exec(body);
    const dispositionV1 = DISPOSITION_V1_RE.exec(body);
    if (PROTOCOL_THREAD_MARKER_RE.test(firstLine) && !findingV3 && !dispositionV3 && !findingV1 && !dispositionV1) {
      fail("actor-owned local-review marker is malformed or unsupported");
    }
    if (findingV3) {
      findingsV3.push([index, findingV3]);
    }
    if (dispositionV3) {
      dispositionsV3.push([index, dispositionV3]);
    }
    const legacyMarker = LEGACY_THREAD_MARKER_RE.test(firstLine);
    if ((body.includes(FINDING_V1) || body.includes(DISPOSITION_V1) || legacyMarker) && !findingV1 && !dispositionV1) {
      fail("actor-owned legacy local-review marker is malformed");
    }
    if (findingV1?.groups) {
      verifyV1Marker(body, findingV1, "finding");
      findingsV1.push([
        index,
        {
          engine: findingV1.groups["engine"],
          round: parseInt(findingV1.groups["round"], 10),
          head: findingV1.groups["head"],
          fingerprint: findingV1.groups["fingerprint"]
        }
      ]);
    }
    if (dispositionV1?.groups) {
      verifyV1Marker(body, dispositionV1, "disposition");
      dispositionsV1.push([
        index,
        {
          engine: dispositionV1.groups["engine"],
          round: parseInt(dispositionV1.groups["round"], 10),
          head: dispositionV1.groups["head"],
          fingerprint: dispositionV1.groups["fingerprint"],
          outcome: dispositionV1.groups["outcome"]
        }
      ]);
    }
  }
  if (findingsV1.length > 0 || dispositionsV1.length > 0) {
    if (thread.isResolved !== true) {
      fail("legacy local-review finding thread is unresolved");
    }
    const used = /* @__PURE__ */ new Set();
    for (const [findingIndex, finding] of findingsV1) {
      const matches = dispositionsV1.filter(
        ([index, disposition]) => index > findingIndex && disposition.engine === finding.engine && disposition.round === finding.round && disposition.fingerprint === finding.fingerprint
      );
      if (matches.length !== 1) {
        fail(
          "legacy local-review finding lacks exactly one matching disposition"
        );
      }
      const dispositionIndex = matches[0][0];
      if (used.has(dispositionIndex)) {
        fail("legacy local-review disposition matches multiple findings");
      }
      used.add(dispositionIndex);
    }
    if (used.size !== dispositionsV1.length) {
      fail("legacy local-review ledger contains an orphan disposition");
    }
  }
  return { findingsV3, dispositionsV3, findingsV1, dispositionsV1 };
}
function pairDispositions(findings, dispositions) {
  const matched = [];
  const used = /* @__PURE__ */ new Set();
  for (const [findingIndex, finding] of findings) {
    const candidates = dispositions.filter(([index, disposition2]) => {
      if (index <= findingIndex) return false;
      if (disposition2.engine !== finding.engine) return false;
      if (disposition2.round !== finding.round) return false;
      if (disposition2.fingerprint !== finding.fingerprint) return false;
      if ("occurrence" in finding && "occurrence" in disposition2) {
        if (finding.occurrence !== disposition2.occurrence) return false;
      }
      return true;
    });
    if (candidates.length !== 1) {
      fail("local-review finding lacks exactly one matching disposition");
    }
    const [dispositionIndex, disposition] = candidates[0];
    const nextFindingIndexes = findings.filter(
      ([nextIndex, nextFinding]) => nextIndex > findingIndex && nextFinding.fingerprint === finding.fingerprint
    ).map(([nextIndex]) => nextIndex);
    if (nextFindingIndexes.length > 0 && dispositionIndex >= Math.min(...nextFindingIndexes)) {
      fail("local-review recurrence was opened before the prior disposition");
    }
    if (used.has(dispositionIndex)) {
      fail("local-review disposition matches multiple findings");
    }
    used.add(dispositionIndex);
    matched.push([finding, disposition]);
  }
  if (used.size !== dispositions.length) {
    fail("local-review ledger contains an orphan disposition");
  }
  return matched;
}
function verifyForwardTransitionOrFail(repo, before, after, message) {
  if (!compareIsForward(repo, before, after)) {
    fail(message);
  }
}
function verifyForwardTransition(repo, before, after) {
  if (before === after) {
    fail("superseding fixed occurrence is not a forward transition");
  }
  verifyForwardTransitionOrFail(
    repo,
    before,
    after,
    "superseding local-review occurrence is not forward-only"
  );
}
function verifyHistoricalThreads(repo, pr) {
  for (const thread of reviewThreads(repo, pr)) {
    threadProtocolRecords(thread);
  }
}
function verifyBlockingNotDeferred(repo, matched) {
  const grouped = /* @__PURE__ */ new Map();
  for (const [finding, disposition] of matched) {
    const list = grouped.get(finding.fingerprint) ?? [];
    list.push([finding.occurrence, finding, disposition]);
    grouped.set(finding.fingerprint, list);
  }
  for (const records of grouped.values()) {
    records.sort((a, b) => a[0] - b[0]);
    const [, finding, disposition] = records[records.length - 1];
    if (finding.severity === "blocking" && disposition.outcome !== "fixed") {
      fail("blocking local-review findings must be fixed");
    }
    const priorUnfixedBlockers = [];
    for (let index = 0; index < records.length - 1; index++) {
      const [, priorFinding, priorDisposition] = records[index];
      if (priorFinding.severity === "blocking" && priorDisposition.outcome !== "fixed") {
        priorUnfixedBlockers.push(index);
      }
    }
    if (priorUnfixedBlockers.length === 0) {
      continue;
    }
    if (disposition.outcome !== "fixed") {
      fail("an unfixed blocker must be cleared by a later fixed occurrence");
    }
    if (repo === void 0) {
      fail(
        "clearing an unfixed blocker requires a repository to verify against"
      );
    }
    const start = priorUnfixedBlockers[priorUnfixedBlockers.length - 1];
    for (let index = start; index < records.length - 1; index++) {
      verifyForwardTransition(
        repo,
        records[index][2].head,
        records[index + 1][1].head
      );
    }
    verifyForwardTransition(repo, finding.head, disposition.head);
  }
}
function verifyThreadDispositions(threads, historicalCommentIds, options) {
  if (options?.repo !== void 0 && options.pr !== void 0) {
    assertThreadScope(threads, options.repo, options.pr);
  }
  const matched = [];
  const topology = /* @__PURE__ */ new Map();
  for (const thread of threads) {
    const { findingsV3, dispositionsV3 } = threadProtocolRecords(
      thread,
      historicalCommentIds
    );
    if (findingsV3.length === 0 && dispositionsV3.length === 0) {
      continue;
    }
    if (findingsV3.length === 0) {
      fail("local-review ledger contains a disposition without a finding");
    }
    if (typeof thread.id !== "string" || !thread.id) {
      fail("local-review finding thread has no stable identity");
    }
    for (const [findingIndex, finding] of findingsV3) {
      const list = topology.get(finding.fingerprint) ?? [];
      list.push({ threadId: thread.id, findingIndex, finding });
      topology.set(finding.fingerprint, list);
    }
    if (thread.isResolved !== true) {
      fail(`local-review finding thread ${thread.id} is unresolved`);
    }
    matched.push(...pairDispositions(findingsV3, dispositionsV3));
  }
  for (const records of topology.values()) {
    const threadIds = new Set(records.map((record) => record.threadId));
    const occurrences = records.map((record) => record.finding.occurrence);
    const expected = records.map((_, index) => index + 1);
    const roots = records.filter((record) => record.finding.occurrence === 1);
    if (threadIds.size !== 1 || occurrences.length !== expected.length || occurrences.some((value, index) => value !== expected[index]) || roots.length !== 1 || roots[0].findingIndex !== 0) {
      fail("local-review fingerprint topology is invalid");
    }
  }
  verifyBlockingNotDeferred(options?.repo, matched);
  return matched;
}
function sameRoundDispositions(args, matched, allowedHeads) {
  const evidence = /* @__PURE__ */ new Map();
  for (const [finding, disposition] of matched) {
    if (finding.engine !== args.engine || finding.round !== args.round) {
      continue;
    }
    const findingHead = finding.head;
    const dispositionHead = disposition.head;
    const findingPosition = Object.prototype.hasOwnProperty.call(
      allowedHeads,
      findingHead
    ) ? allowedHeads[findingHead] : void 0;
    if (findingPosition === void 0) {
      if (disposition.outcome !== "fixed") {
        continue;
      }
      if (dispositionHead === findingHead) {
        fail("historical fixed disposition is not a forward transition");
      }
      if (args.repo === void 0) {
        fail("historical fixed disposition is not a forward transition");
      }
      verifyForwardTransitionOrFail(
        args.repo,
        findingHead,
        dispositionHead,
        "historical fixed disposition is not a forward transition"
      );
      continue;
    }
    if (disposition.outcome === "fixed" && dispositionHead === findingHead) {
      fail("fixed finding was not posted before its disposition head");
    }
    const dispositionPosition = Object.prototype.hasOwnProperty.call(
      allowedHeads,
      dispositionHead
    ) ? allowedHeads[dispositionHead] : void 0;
    if (dispositionPosition === void 0 || dispositionPosition < findingPosition || disposition.outcome === "fixed" && dispositionPosition === findingPosition) {
      fail("same-round finding disposition is outside the observed transition");
    }
    if (finding.severity === "blocking" && disposition.outcome !== "fixed") {
      fail("blocking local-review findings must be fixed");
    }
    const fixed = disposition.outcome === "fixed";
    const fixedMajor = fixed && (finding.severity === "blocking" || finding.severity === "major");
    const fixedNonblocking = fixed && finding.severity !== "blocking";
    const previous = evidence.get(finding.fingerprint) ?? [false, false, false];
    evidence.set(finding.fingerprint, [
      previous[0] || fixed,
      previous[1] || fixedMajor,
      previous[2] || fixedNonblocking
    ]);
  }
  return Array.from(evidence.keys()).sort().map(
    (fingerprint) => [fingerprint, ...evidence.get(fingerprint)]
  );
}
function transitionHeads(params) {
  const target = resultHead(params);
  if (params.allowedHeadsFile !== void 0) {
    return loadAllowedHeads(
      params.allowedHeadsFile,
      params.before,
      target,
      params.repo
    );
  }
  if (params.before === target) {
    return { [params.before]: 0 };
  }
  const runner = getGitHubRunner();
  const list = runner.gitRevList ? runner.gitRevList(params.before, target) : execFileSync2(
    "git",
    [
      "rev-list",
      "--reverse",
      "--ancestry-path",
      `${params.before}..${target}`
    ],
    {
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
      maxBuffer: SUBPROCESS_MAX_BUFFER
    }
  ).trim().split(/\r?\n/).filter(Boolean);
  const values = [params.before, ...list];
  if (values[values.length - 1] !== target || new Set(values).size !== values.length) {
    fail("review result transition is not forward-only");
  }
  const heads = {};
  values.forEach((value, index) => {
    heads[value] = index;
  });
  return heads;
}
function verifyResultEvidence(args, threads, options) {
  const data = options?.data ?? validateResultData(args, readResultBytes(args.resultFile));
  if (data.status !== "clean" && data.status !== "changed") {
    fail("ledger result evidence requires a clean or changed review result");
  }
  verifyReviewBase(args.repo, args.pr, args.base, args.before);
  verifyGitTransition(args.before, resultHead(args), args.head);
  const matched = verifyThreadDispositions(
    threads,
    options?.historicalCommentIds,
    { repo: args.repo, pr: args.pr }
  );
  const allowedHeads = options?.allowedHeads ?? transitionHeads(args);
  const evidence = sameRoundDispositions(args, matched, allowedHeads);
  const evidenceFingerprints = evidence.map(([fingerprint]) => fingerprint);
  const expected = [...data.findingFingerprints].sort();
  if (evidenceFingerprints.length !== expected.length || evidenceFingerprints.some((value, index) => value !== expected[index])) {
    fail(
      "review result fingerprints do not equal the complete same-round disposition set"
    );
  }
  if (data.status === "clean") {
    if (evidence.some(([, hasFix]) => hasFix)) {
      fail("clean review results cannot have same-round fixes");
    }
    return data;
  }
  if (evidence.length === 0) {
    fail("changed review results require ledger evidence");
  }
  if (evidence.some(([, , hasMajorFix]) => hasMajorFix) && data.classification !== "material") {
    fail("fixed blocking or major findings require material classification");
  }
  if (!evidence.some(([, hasFix]) => hasFix)) {
    fail("changed review results require a fixed ledger finding");
  }
  if (args.round >= 3 && evidence.some(([, , , hasNonblockingFix]) => hasNonblockingFix)) {
    fail("convergence review results cannot fix non-blocking findings");
  }
  return data;
}
function writeResult(params) {
  assertActor(params.actor);
  verifyReviewBase(params.repo, params.pr, params.base, params.before);
  verifyGitTransition(params.before, params.head, params.head);
  const threads = reviewThreads(params.repo, params.pr);
  const historicalCommentIds = loadHistoricalCommentIds(
    params.historicalCommentIdsFile
  );
  const matched = verifyThreadDispositions(threads, historicalCommentIds, {
    repo: params.repo,
    pr: params.pr
  });
  const allowedHeads = transitionHeads({
    repo: params.repo,
    before: params.before,
    head: params.head,
    allowedHeadsFile: params.allowedHeadsFile
  });
  const dispositions = sameRoundDispositions(params, matched, allowedHeads);
  const changed = params.before !== params.head;
  if (!changed && dispositions.some(([, hasFix]) => hasFix)) {
    fail("clean review results cannot have same-round fixes");
  }
  if (changed && params.classification !== "minor" && params.classification !== "material") {
    fail("changed review result requires --classification");
  }
  if (!changed && params.classification !== void 0) {
    fail("clean review result cannot have a classification");
  }
  if (changed && params.round >= 3 && params.classification !== "material") {
    fail("round 3+ changed review results require material classification");
  }
  if (changed && dispositions.length === 0) {
    fail("changed review results require ledger evidence");
  }
  if (changed && !dispositions.some(([, hasFix]) => hasFix)) {
    fail("changed review results require a fixed ledger finding");
  }
  if (changed && params.round >= 3 && dispositions.some(([, , , hasNonblockingFix]) => hasNonblockingFix)) {
    fail("convergence review results cannot fix non-blocking findings");
  }
  if (changed && params.classification !== "material" && dispositions.some(([, , hasMajorFix]) => hasMajorFix)) {
    fail("fixed blocking or major findings require material classification");
  }
  const value = {
    version: PROTOCOL_VERSION,
    status: changed ? "changed" : "clean",
    engine: params.engine,
    round: params.round,
    baseSha: params.base,
    beforeSha: params.before,
    afterSha: params.head,
    classification: changed ? params.classification : null,
    findingFingerprints: dispositions.map(([fingerprint]) => fingerprint),
    finalLaneComplete: true
  };
  validateResultData(params, Buffer.from(JSON.stringify(value) + "\n", "utf8"));
  verifyResultEvidence(params, threads, {
    data: value,
    allowedHeads,
    historicalCommentIds
  });
  writeResultFile(params.resultFile, value);
  const raw = readResultBytes(params.resultFile);
  const resultSha256 = sha256Bytes(raw);
  return { ...value, resultSha256 };
}
function preflightAnchor(params) {
  verifyHead(params.repo, params.pr, params.head);
  const files = getPrFiles(params.repo, params.pr);
  const line = params.fileLevel ? null : params.line;
  const side = params.fileLevel ? null : params.side ?? "RIGHT";
  validateAnchor(files, params.path, line, side);
  return {
    anchor: params.fileLevel ? "file" : `${params.side ?? "RIGHT"}:${params.line}`,
    path: params.path,
    verified: true
  };
}
function findingRows(comments, fingerprint) {
  const rows = [];
  for (const row of comments) {
    const match = matchFinding(String(row.body ?? ""));
    if (match !== null && match.fingerprint === fingerprint) {
      rows.push({ row, match });
    }
  }
  return rows;
}
function assertRootComment(rows, commentId) {
  const roots = rows.filter((entry) => entry.match.occurrence === 1);
  if (roots.length !== 1 || (roots[0]?.row.databaseId ?? roots[0]?.row.id) !== commentId) {
    fail("--comment-id does not identify the fingerprint root comment");
  }
}
function postFinding(params) {
  const content = resolveContent(params);
  let marker;
  let body;
  const isV3 = content !== void 0;
  if (isV3) {
    if (!params.engine || params.round === void 0 || !params.fingerprint || !params.severity || !params.lens) {
      fail(
        "v3 content mode requires --engine, --round, --fingerprint, --severity, --lens"
      );
    }
    const contentStr = content;
    const built = buildFindingBody({
      engine: params.engine,
      round: params.round,
      head: params.head,
      fingerprint: params.fingerprint,
      occurrence: params.occurrence ?? 1,
      severity: params.severity,
      lens: params.lens,
      content: contentStr
    });
    marker = built.marker;
    body = built.body;
  } else {
    marker = FINDING_V1;
    body = readLegacyBody(params.bodyFile ?? "-", marker, params.content);
  }
  verifyHead(params.repo, params.pr, params.head);
  const files = getPrFiles(params.repo, params.pr);
  const line = params.fileLevel ? null : params.line;
  const side = params.fileLevel ? null : params.side ?? "RIGHT";
  validateAnchor(files, params.path, line, side);
  let commentId;
  let replayed = false;
  if (isV3) {
    const comments = getReviewComments(params.repo, params.pr);
    if (rowsHaveHistoricalMarkers(comments)) {
      verifyHistoricalThreads(params.repo, params.pr);
    }
    const existing = findMatchingBody(
      comments,
      marker,
      body
    );
    const records = findingRows(comments, params.fingerprint);
    if (existing === null && records.length > 0) {
      fail("fingerprint already has a root thread; use reopen-occurrence");
    }
    if ((params.occurrence ?? 1) !== 1) {
      fail("post-finding creates occurrence 1; use reopen-occurrence later");
    }
    const res = postReviewComment(
      params.repo,
      params.pr,
      params.head,
      marker,
      body,
      {
        path: params.path,
        line: params.fileLevel ? void 0 : params.line,
        side: params.fileLevel ? void 0 : params.side ?? "RIGHT",
        fileLevel: params.fileLevel
      }
    );
    commentId = res.commentId;
    replayed = res.replayed;
  } else {
    const payload = {
      body,
      commit_id: params.head,
      path: params.path
    };
    if (params.fileLevel) {
      payload["subject_type"] = "file";
    } else {
      payload["line"] = params.line;
      payload["side"] = params.side ?? "RIGHT";
    }
    const response = jsonOutput(
      ["api", "-X", "POST", `repos/${params.repo}/pulls/${params.pr}/comments`],
      payload
    );
    commentId = getPostedCommentId(response);
    verifyComment(params.repo, commentId, body);
    verifyHead(params.repo, params.pr, params.head);
  }
  return {
    comment_id: commentId,
    verified: true,
    ...isV3 ? { replayed } : {}
  };
}
function reopenOccurrence(params) {
  const content = resolveContent(params);
  if (content === void 0) {
    fail("reopen-occurrence requires content or content-file");
  }
  const { marker, body } = buildFindingBody({
    engine: params.engine,
    round: params.round,
    head: params.head,
    fingerprint: params.fingerprint,
    occurrence: params.occurrence,
    severity: params.severity,
    lens: params.lens,
    content
  });
  verifyHead(params.repo, params.pr, params.head);
  const comments = getReviewComments(params.repo, params.pr);
  if (rowsHaveHistoricalMarkers(comments)) {
    verifyHistoricalThreads(params.repo, params.pr);
  }
  const records = findingRows(comments, params.fingerprint);
  const existing = findMatchingBody(
    comments,
    marker,
    body
  );
  if (params.occurrence < 2) {
    fail("reopen-occurrence requires occurrence 2 or later");
  }
  assertRootComment(records, params.commentId);
  const dispositions = comments.map((row) => matchDisposition(String(row.body ?? ""))).filter((d) => d !== null);
  for (const { match: finding } of records) {
    if (finding.occurrence >= params.occurrence) {
      continue;
    }
    const matches = dispositions.filter(
      (disp) => disp.engine === finding.engine && disp.round === finding.round && disp.fingerprint === finding.fingerprint && disp.occurrence === finding.occurrence
    );
    if (matches.length !== 1) {
      fail("every prior finding occurrence must have exactly one disposition");
    }
  }
  const occurrences = records.map((entry) => entry.match.occurrence).sort((a, b) => a - b);
  const expectedLength = params.occurrence - (existing === null ? 1 : 0);
  const matchSeq = occurrences.length === expectedLength && occurrences.every((v, i) => v === i + 1);
  if (!matchSeq) {
    fail("finding occurrences are missing, duplicated, or out of sequence");
  }
  const { commentId, replayed } = postReviewComment(
    params.repo,
    params.pr,
    params.head,
    marker,
    body,
    { replyTo: params.commentId }
  );
  const threadReplayed = setThreadState(
    params.threadId,
    false,
    params.commentId,
    { repo: params.repo, pr: params.pr }
  );
  verifyHead(params.repo, params.pr, params.head);
  return {
    comment_id: commentId,
    replayed,
    thread_replayed: threadReplayed,
    resolved: false,
    verified: true
  };
}
function dispose(params) {
  const content = resolveContent(params);
  if (content === void 0) {
    fail("dispose requires content or content-file");
  }
  const occurrence = params.occurrence ?? 1;
  const { marker, body } = buildDispositionBody({
    engine: params.engine,
    round: params.round,
    head: params.head,
    fingerprint: params.fingerprint,
    occurrence,
    outcome: params.outcome,
    content
  });
  verifyHead(params.repo, params.pr, params.head);
  const comments = getReviewComments(params.repo, params.pr);
  if (rowsHaveHistoricalMarkers(comments)) {
    verifyHistoricalThreads(params.repo, params.pr);
  }
  const records = findingRows(comments, params.fingerprint);
  assertRootComment(records, params.commentId);
  const matches = records.map((entry) => entry.match).filter(
    (match) => match.engine === params.engine && match.round === params.round && match.occurrence === occurrence
  );
  if (matches.length !== 1) {
    fail(
      "disposition does not identify exactly one existing finding occurrence"
    );
  }
  const finding = matches[0];
  if (finding.severity === "blocking" && params.outcome !== "fixed") {
    fail("blocking local-review findings must be fixed");
  }
  const dispMatches = comments.filter((row) => {
    const match = matchDisposition(String(row.body ?? ""));
    return match !== null && match.engine === params.engine && match.round === params.round && match.fingerprint === params.fingerprint && match.occurrence === occurrence;
  });
  if (dispMatches.length > 1) {
    fail("disposition identity is duplicated");
  }
  if (dispMatches.length === 1 && dispMatches[0]?.body !== body) {
    fail(
      "disposition identity already exists with conflicting content or outcome"
    );
  }
  const { commentId, replayed } = postReviewComment(
    params.repo,
    params.pr,
    params.head,
    marker,
    body,
    { replyTo: params.commentId }
  );
  const threadReplayed = setThreadState(
    params.threadId,
    true,
    params.commentId,
    { repo: params.repo, pr: params.pr }
  );
  verifyHead(params.repo, params.pr, params.head);
  return {
    comment_id: commentId,
    replayed,
    thread_replayed: threadReplayed,
    resolved: true,
    verified: true
  };
}
function reply(params) {
  const body = readLegacyBody(
    params.bodyFile ?? "-",
    DISPOSITION_V1,
    params.body
  );
  verifyHead(params.repo, params.pr, params.head);
  const response = jsonOutput(
    [
      "api",
      "-X",
      "POST",
      `repos/${params.repo}/pulls/${params.pr}/comments/${params.commentId}/replies`
    ],
    { body }
  );
  const commentId = getPostedCommentId(response);
  verifyComment(params.repo, commentId, body);
  verifyHead(params.repo, params.pr, params.head);
  return { comment_id: commentId, verified: true };
}
function postPrComment(params) {
  const body = readLegacyBody(
    params.bodyFile ?? "-",
    PR_V1_MARKERS,
    params.body
  );
  verifyHead(params.repo, params.pr, params.head);
  const response = jsonOutput(
    ["api", "-X", "POST", `repos/${params.repo}/issues/${params.pr}/comments`],
    { body }
  );
  const commentId = getPostedCommentId(response);
  verifyIssueComment(params.repo, commentId, body);
  verifyHead(params.repo, params.pr, params.head);
  return { comment_id: commentId, verified: true };
}
function attest(params) {
  const raw = readResultBytes(params.resultFile);
  const data = validateResultData(params, raw);
  assertActor(params.actor);
  const resultHash = sha256Bytes(raw);
  if (resultHash !== params.expectedResultSha256) {
    fail("review result changed before attestation");
  }
  if (data.status === "blocked") {
    fail("blocked review results cannot be attested as complete");
  }
  const threads = reviewThreads(
    params.repo,
    params.pr,
    params.threadsFile,
    params.expectedThreadsSha256
  );
  const historicalCommentIds = loadHistoricalCommentIds(
    params.historicalCommentIdsFile
  );
  verifyResultEvidence(params, threads, { data, historicalCommentIds });
  const content = resolveContent(params) ?? (data.status === "clean" ? "No new material findings." : "Review fixes completed and ledger dispositions verified.");
  let marker;
  if (data.status === "clean") {
    marker = `<!-- local-review-pass:v3 engine=${params.engine} round=${params.round} base=${params.base} head=${params.head} result-sha256=${resultHash} -->`;
  } else {
    const fingerprints = data.findingFingerprints.join(",");
    marker = `<!-- local-review-complete:v3 engine=${params.engine} round=${params.round} base=${params.base} before=${params.before} head=${params.head} classification=${data.classification} fingerprints=${fingerprints} result-sha256=${resultHash} -->`;
  }
  const body = `${marker}
${content}`;
  verifyHead(params.repo, params.pr, params.head);
  const existing = findMatchingAttestation(
    getIssueComments(params.repo, params.pr),
    params.engine,
    params.round,
    body
  );
  let commentId;
  let replayed = existing !== null;
  let created = existing === null;
  if (existing === null) {
    try {
      const response = jsonOutput(
        [
          "api",
          "-X",
          "POST",
          `repos/${params.repo}/issues/${params.pr}/comments`
        ],
        { body }
      );
      commentId = getPostedCommentId(response);
    } catch (error) {
      if (error instanceof LedgerError) {
        const recovered = findMatchingBody(
          getIssueComments(params.repo, params.pr),
          marker,
          body
        );
        if (recovered === null) throw error;
        commentId = recovered;
        replayed = true;
        created = false;
      } else {
        throw error;
      }
    }
  } else {
    commentId = existing;
  }
  try {
    verifyIssueComment(params.repo, commentId, body);
    verifyReviewBase(params.repo, params.pr, params.base, params.before);
    verifyHead(params.repo, params.pr, params.head);
  } catch (error) {
    if (created) {
      try {
        deleteIssueComment(params.repo, params.pr, commentId);
      } catch (rollbackError) {
        throw new LedgerError(
          `attestation verification failed and rollback could not be verified: ${rollbackError.message ?? String(rollbackError)}`,
          { cause: error }
        );
      }
    }
    throw error;
  }
  return {
    comment_id: commentId,
    replayed,
    result_sha256: resultHash,
    verified: true
  };
}
function resolve(params) {
  verifyHead(params.repo, params.pr, params.head);
  assertThreadIdsInScope(params.repo, params.pr, [params.threadId]);
  setThreadState(params.threadId, true, void 0, {
    repo: params.repo,
    pr: params.pr
  });
  verifyHead(params.repo, params.pr, params.head);
  return { thread_id: params.threadId, resolved: true };
}
function assertThreadIdsInScope(repo, pr, threadIds) {
  const scopedIds = new Set(reviewThreads(repo, pr).map((thread) => thread.id));
  if (threadIds.some((threadId) => !scopedIds.has(threadId))) {
    fail("review thread does not belong to the requested PR");
  }
}
function reconcile(params) {
  verifyHead(params.repo, params.pr, params.head);
  const comments = getReviewComments(params.repo, params.pr);
  if (rowsHaveHistoricalMarkers(comments)) {
    verifyHistoricalThreads(params.repo, params.pr);
  }
  const findingRows2 = [];
  const dispositionRows = [];
  for (const row of comments) {
    const body = String(row.body ?? "");
    const finding = matchFinding(body);
    const disposition = matchDisposition(body);
    if (finding && finding.fingerprint === params.fingerprint) {
      findingRows2.push({ id: row.databaseId ?? row.id, ...finding });
    }
    if (disposition && disposition.fingerprint === params.fingerprint) {
      dispositionRows.push({ id: row.databaseId ?? row.id, ...disposition });
    }
  }
  const occurrences = findingRows2.map((row) => row["occurrence"]).sort((a, b) => a - b);
  const sequenceValid = occurrences.every((val, idx) => val === idx + 1);
  const identity = (row) => `${String(row["engine"])}|${String(row["round"])}|${String(row["fingerprint"])}|${String(row["occurrence"])}`;
  const findingKeys = new Set(findingRows2.map(identity));
  const dispositionKeys = dispositionRows.map(identity);
  const disposed = new Set(
    dispositionRows.map((row) => row["occurrence"])
  );
  const ledgerValid = sequenceValid && new Set(dispositionKeys).size === dispositionKeys.length && dispositionKeys.every((key) => findingKeys.has(key));
  const undisposed = occurrences.filter((occ) => !disposed.has(occ));
  const nextAction = !ledgerValid ? "repair-sequence" : undisposed.length > 0 ? "dispose" : occurrences.length > 0 ? "reopen-occurrence" : "post-finding";
  let threadId = null;
  let threadResolved = null;
  const rootIds = findingRows2.filter((row) => row["occurrence"] === 1).map((row) => row["id"]).filter((id) => typeof id === "number");
  if (ledgerValid && rootIds.length === 1) {
    const candidate = findRootThread(params.repo, params.pr, rootIds[0]);
    if (candidate === null) {
      fail("could not identify exactly one root review thread");
    }
    if (typeof candidate.id !== "string" || typeof candidate.isResolved !== "boolean") {
      fail("root review thread has an unexpected shape");
    }
    threadId = candidate.id;
    threadResolved = candidate.isResolved;
  }
  return {
    findings: findingRows2,
    dispositions: dispositionRows,
    sequenceValid,
    ledgerValid,
    nextOccurrence: sequenceValid ? occurrences.length + 1 : null,
    undisposedOccurrences: undisposed,
    nextAction,
    threadId,
    threadResolved,
    verified: true
  };
}
function verifyLedger(params) {
  const actor = assertActor(params.actor);
  verifyHead(params.repo, params.pr, params.head);
  const threads = reviewThreads(
    params.repo,
    params.pr,
    params.threadsFile,
    params.expectedThreadsSha256
  );
  const historicalCommentIds = loadHistoricalCommentIds(
    params.historicalCommentIdsFile
  );
  const matched = verifyThreadDispositions(threads, historicalCommentIds, {
    repo: params.repo,
    pr: params.pr
  });
  if (params.resultFile !== void 0) {
    if (!params.engine || params.round === void 0 || !params.base || !params.before) {
      fail(
        "verify-ledger result evidence requires --engine, --round, --base, --before, and --result-file"
      );
    }
    verifyResultEvidence(
      {
        repo: params.repo,
        pr: params.pr,
        head: params.head,
        engine: params.engine,
        round: params.round,
        base: params.base,
        before: params.before,
        resultFile: params.resultFile,
        resultHead: params.resultHead,
        allowedHeadsFile: params.allowedHeadsFile
      },
      threads,
      { historicalCommentIds }
    );
  }
  verifyHead(params.repo, params.pr, params.head);
  return { actor, dispositions: matched.length, verified: true };
}

// src/roster.ts
function parseReviewers(raw, author) {
  if (raw === "none") {
    return [];
  }
  return validateReviewers(raw.split(","), author);
}
function validateReviewers(reviewers, author) {
  if (reviewers.length > 2) {
    fail("a roster may declare at most two reviewers");
  }
  const validated = [];
  for (const candidate of reviewers) {
    if (!SUPPORTED_ENGINES.includes(candidate)) {
      fail(`reviewer must be one of: ${SUPPORTED_ENGINES.join(", ")}`);
    }
    const engine = candidate;
    if (engine === author) {
      fail("the author engine cannot also be listed as a reviewer");
    }
    if (validated.includes(engine)) {
      fail("reviewers must be distinct");
    }
    validated.push(engine);
  }
  return validated;
}
function rosterDigestInput(fields) {
  return [
    "local-review-roster:v2",
    `author=${fields.author}`,
    `reviewers=${fields.reviewers}`,
    `head=${fields.head}`,
    `supersedes=${fields.supersedes === null ? "none" : fields.supersedes}`,
    "",
    fields.content
  ].join("\n");
}
function formatReviewers(reviewers) {
  return reviewers.length === 0 ? "none" : reviewers.join(",");
}
function matchRoster(body) {
  const occurrences = body.split(ROSTER_ANY_MARKER).length - 1;
  if (occurrences === 0) {
    return null;
  }
  if (occurrences > 1) {
    fail("a comment carries more than one local-review roster marker");
  }
  if (body.includes(ROSTER_V2_MARKER)) {
    return matchRosterV2(body);
  }
  if (body.includes(ROSTER_V1_MARKER)) {
    return matchRosterV1(body);
  }
  fail("local-review roster record is of an unsupported protocol version");
}
function matchRosterV2(body) {
  const matched = matchMarkerLine(body, ROSTER_V2_RE, ROSTER_V2_MARKER);
  if (matched === null) {
    fail("local-review roster record is malformed");
  }
  const groups = matched.match.groups;
  const author = groups["author"];
  const rawSupersedes = groups["supersedes"];
  const supersedes = rawSupersedes === "none" ? null : parseInt(rawSupersedes, 10);
  if (supersedes !== null && !Number.isSafeInteger(supersedes)) {
    fail("local-review roster supersedes must be a comment id");
  }
  const expected = sha256Text(
    rosterDigestInput({
      author,
      reviewers: groups["reviewers"],
      head: groups["head"],
      supersedes,
      content: matched.content
    })
  );
  if (expected !== groups["declaration_sha"]) {
    fail(
      "authenticated local-review-roster:v2 record has an invalid declaration hash"
    );
  }
  return {
    version: 2,
    author,
    reviewers: parseReviewers(groups["reviewers"], author),
    head: groups["head"],
    supersedes,
    digest: groups["declaration_sha"]
  };
}
function matchRosterV1(body) {
  const match = matchProtocol(body, ROSTER_V1_RE, ROSTER_V1_MARKER);
  if (!match?.groups) {
    fail("local-review roster record is malformed");
  }
  const author = match.groups["author"];
  return {
    version: 1,
    author,
    reviewers: parseReviewers(match.groups["reviewers"], author),
    head: null,
    supersedes: null,
    digest: match.groups["content_sha"]
  };
}
function buildRosterBody(params) {
  if (!SUPPORTED_ENGINES.includes(params.author)) {
    fail(`author must be one of: ${SUPPORTED_ENGINES.join(", ")}`);
  }
  validateContentString(params.content);
  validateReviewers(params.reviewers, params.author);
  requireSha(params.head, "head");
  if (params.supersedes !== null && (!Number.isSafeInteger(params.supersedes) || params.supersedes < 1)) {
    fail("supersedes must be a positive comment id");
  }
  const reviewers = formatReviewers(params.reviewers);
  const declarationSha = sha256Text(
    rosterDigestInput({
      author: params.author,
      reviewers,
      head: params.head,
      supersedes: params.supersedes,
      content: params.content
    })
  );
  const marker = `${ROSTER_V2_MARKER} author=${params.author} reviewers=${reviewers} head=${params.head} supersedes=${params.supersedes === null ? "none" : params.supersedes} declaration-sha256=${declarationSha} -->`;
  return { marker, body: `${marker}
${params.content}` };
}
function absentRoster() {
  return {
    present: false,
    version: null,
    author: null,
    reviewers: [],
    head: null,
    commentId: null,
    supersedes: null,
    chain: []
  };
}
function rosterCandidates(rows) {
  const candidates = [];
  for (const row of rows) {
    const body = String(row["body"] ?? "");
    if (!body.includes(ROSTER_ANY_MARKER)) {
      continue;
    }
    const match = matchRoster(body);
    if (match === null || typeof row["id"] !== "number") {
      fail("local-review roster record is malformed");
    }
    candidates.push({ id: row["id"], match });
  }
  return candidates.sort((a, b) => a.id - b.id);
}
function resolveRoster(rows) {
  const candidates = rosterCandidates(rows);
  if (candidates.length === 0) {
    return absentRoster();
  }
  const links = candidates.filter((row) => row.match.version === 2);
  if (links.length === 0) {
    return resolveLegacyRoster(candidates);
  }
  const byId = new Map(candidates.map((row) => [row.id, row]));
  const superseded = /* @__PURE__ */ new Set();
  let roots = 0;
  for (const link of links) {
    const predecessor = link.match.supersedes;
    if (predecessor === null) {
      roots += 1;
      continue;
    }
    const target = byId.get(predecessor);
    if (target === void 0) {
      fail(
        `local-review roster supersedes comment ${predecessor}, which is not a roster on this pull request`
      );
    }
    if (predecessor >= link.id) {
      fail("local-review roster supersedes a later declaration");
    }
    if (superseded.has(predecessor)) {
      fail("local-review roster supersession chain forks");
    }
    superseded.add(predecessor);
    if (target.match.version !== 2) {
      roots += 1;
    }
  }
  if (roots !== 1) {
    fail(
      roots === 0 ? "local-review roster supersession chain has no first declaration" : "local-review roster declares more than one supersession chain"
    );
  }
  const tips = links.filter((link) => !superseded.has(link.id));
  if (tips.length !== 1) {
    fail("local-review roster supersession chain does not resolve to one tip");
  }
  const tip = tips[0];
  const chain = [];
  let cursor = tip;
  while (cursor !== void 0) {
    chain.unshift(cursor.id);
    const predecessor = cursor.match.supersedes;
    cursor = predecessor === null ? void 0 : byId.get(predecessor);
  }
  if (chain.length !== candidates.length) {
    fail("local-review roster supersession chain does not cover every roster");
  }
  return {
    present: true,
    version: 2,
    author: tip.match.author,
    reviewers: tip.match.reviewers,
    head: tip.match.head,
    commentId: tip.id,
    supersedes: tip.match.supersedes,
    chain
  };
}
function resolveLegacyRoster(candidates) {
  if (candidates.length !== 1) {
    fail("local-review roster is declared more than once");
  }
  const only = candidates[0];
  return {
    present: true,
    version: 1,
    author: only.match.author,
    reviewers: only.match.reviewers,
    head: null,
    commentId: only.id,
    supersedes: null,
    chain: [only.id]
  };
}
function readRoster(params) {
  assertActor(params.actor);
  return resolveRoster(getIssueComments(params.repo, params.pr));
}
function reconcileConcurrentRoster(repo, pr, body, postedCommentId) {
  const rows = getIssueComments(repo, pr).filter(
    (row) => String(row["body"] ?? "") === body
  );
  if (rows.some((row) => typeof row["id"] !== "number")) {
    fail("local-review roster conflicts with concurrent evidence");
  }
  if (rows.length === 0) {
    fail("local-review roster conflicts with concurrent evidence");
  }
  const ids = rows.map((row) => row["id"]).sort((a, b) => a - b);
  const canonical = ids[0];
  for (const duplicate of ids.slice(1)) {
    deleteIssueComment(repo, pr, duplicate);
  }
  return {
    commentId: canonical,
    usedPostedComment: canonical === postedCommentId
  };
}
function postRoster(params) {
  assertActor(params.actor);
  requireSha(params.head, "head");
  verifyHead(params.repo, params.pr, params.head);
  const rows = getIssueComments(params.repo, params.pr);
  const existing = resolveRoster(rows);
  const replayCandidate = existing.present && existing.version === 2 ? buildRosterBody({
    author: params.author,
    reviewers: params.reviewers,
    head: params.head,
    supersedes: existing.supersedes,
    content: params.content
  }) : null;
  const effectiveBody = existing.commentId === null ? null : rows.find((row) => row["id"] === existing.commentId)?.["body"] ?? null;
  const isReplay = replayCandidate !== null && effectiveBody === replayCandidate.body;
  const supersedes = isReplay ? existing.supersedes : existing.commentId;
  const { marker, body } = isReplay ? replayCandidate : buildRosterBody({
    author: params.author,
    reviewers: params.reviewers,
    head: params.head,
    supersedes,
    content: params.content
  });
  let commentId;
  let replayed = false;
  let created = false;
  if (isReplay) {
    commentId = existing.commentId;
    replayed = true;
  } else {
    created = true;
    try {
      const response = jsonOutput(
        [
          "api",
          "-X",
          "POST",
          `repos/${params.repo}/issues/${params.pr}/comments`
        ],
        { body }
      );
      commentId = getPostedCommentId(response);
    } catch (error) {
      if (error instanceof LedgerError) {
        const recovered = findMatchingBody(
          getIssueComments(params.repo, params.pr),
          marker,
          body
        );
        if (recovered === null) throw error;
        commentId = recovered;
        replayed = true;
        created = false;
      } else {
        throw error;
      }
    }
  }
  let chain;
  try {
    if (created) {
      const reconciled = reconcileConcurrentRoster(
        params.repo,
        params.pr,
        body,
        commentId
      );
      commentId = reconciled.commentId;
      replayed = replayed || !reconciled.usedPostedComment;
      created = reconciled.usedPostedComment;
    }
    verifyIssueComment(params.repo, commentId, body);
    const roster = resolveRoster(getIssueComments(params.repo, params.pr));
    if (roster.commentId !== commentId) {
      fail("could not verify the effective local-review roster after posting");
    }
    chain = roster.chain;
    verifyHead(params.repo, params.pr, params.head);
  } catch (error) {
    if (created) {
      try {
        deleteIssueComment(params.repo, params.pr, commentId);
      } catch (rollbackError) {
        throw new LedgerError(
          `roster verification failed and rollback could not be verified: ${rollbackError.message ?? String(rollbackError)}`,
          { cause: error }
        );
      }
    }
    throw error;
  }
  return {
    comment_id: commentId,
    author: params.author,
    reviewers: [...params.reviewers],
    head: params.head,
    supersedes,
    superseded: supersedes !== null,
    chain,
    replayed,
    verified: true
  };
}
function matchAttestationMarker(body, pattern) {
  const match = pattern.exec(body);
  if (!match || !match.groups || match.index !== 0) {
    return null;
  }
  const matchEnd = match[0].length;
  if (!body.slice(matchEnd).startsWith("\n")) {
    return null;
  }
  if (!body.slice(matchEnd + 1).trim()) {
    return null;
  }
  return match;
}
function attestationsAtHead(rows, head) {
  const found = [];
  const identities = /* @__PURE__ */ new Set();
  for (const row of rows) {
    const body = String(row["body"] ?? "");
    const pass = matchAttestationMarker(body, PASS_V3_RE);
    const complete = matchAttestationMarker(body, COMPLETE_V3_RE);
    const match = pass ?? complete;
    if (!match?.groups) {
      continue;
    }
    const engine = match.groups["engine"];
    const round = parseInt(match.groups["round"], 10);
    const identity = `${engine}|${round}`;
    if (identities.has(identity)) {
      fail("local-review attestation identity is duplicated");
    }
    identities.add(identity);
    if (match.groups["head"] === head) {
      found.push({
        engine,
        round,
        status: pass === match ? "clean" : "changed"
      });
    }
  }
  return found;
}
function coverageTier(count) {
  if (count === 0) return "solo";
  if (count === 1) return "cross";
  return "full";
}
function coverage(params) {
  assertActor(params.actor);
  verifyHead(params.repo, params.pr, params.head);
  const rows = getIssueComments(params.repo, params.pr);
  const roster = resolveRoster(rows);
  const attested = attestationsAtHead(rows, params.head);
  const attestedEngines = [
    ...new Set(attested.map((row) => row.engine))
  ].sort();
  const nonAuthorAttested = roster.reviewers.filter(
    (engine) => attestedEngines.includes(engine)
  );
  const missingReviewers = roster.reviewers.filter(
    (engine) => !attestedEngines.includes(engine)
  );
  const authorAttested = roster.author !== null && attestedEngines.includes(roster.author);
  const rosterStale = roster.present && roster.head !== params.head;
  const soloDeclared = roster.present && roster.reviewers.length === 0;
  const soloAcknowledged = soloDeclared && roster.version === 2 && !rosterStale;
  const report = {
    head: params.head,
    rosterPresent: roster.present,
    rosterVersion: roster.version,
    rosterHead: roster.head,
    rosterStale,
    rosterChain: roster.chain,
    author: roster.author,
    reviewers: [...roster.reviewers],
    attestedAtHead: attestedEngines,
    nonAuthorAttested,
    missingReviewers,
    authorAttested,
    tier: coverageTier(nonAuthorAttested.length),
    soloDeclared,
    soloAcknowledged,
    roundComplete: roster.present && missingReviewers.length === 0 && (!soloDeclared || soloAcknowledged && authorAttested),
    verified: true
  };
  verifyHead(params.repo, params.pr, params.head);
  return report;
}
function verifyCoverage(params) {
  const report = coverage(params);
  if (!report.rosterPresent) {
    fail(
      "no local-review roster is declared on this pull request; run post-roster before claiming coverage"
    );
  }
  if (report.missingReviewers.length > 0) {
    fail(
      `declared reviewers have not attested this head: ${report.missingReviewers.join(", ")}`
    );
  }
  if (report.soloDeclared && report.rosterVersion !== 2) {
    fail(
      "this solo relay is declared in the roster:v1 grammar, whose declaration sits outside its own hash; re-post it with post-roster to record the same choice as roster:v2 evidence"
    );
  }
  if (report.soloDeclared && report.rosterStale) {
    fail(
      `this solo relay was declared at ${report.rosterHead ?? "<no head>"}, not at ${report.head}; re-post it with post-roster --head ${report.head} to declare the same choice over the code that is here now`
    );
  }
  if (report.soloAcknowledged && !report.authorAttested) {
    fail(
      "a declared solo relay still requires the author engine to attest this head"
    );
  }
  return report;
}

// src/format.ts
function formatFindings(findings) {
  if (findings.length === 0) {
    return "NO FINDINGS";
  }
  const lines = [
    `### Review Findings (${findings.length})`,
    "",
    "| Severity | Lens | File:Line | Description |",
    "| :--- | :--- | :--- | :--- |"
  ];
  for (const finding of findings) {
    const severity = finding.severity ?? "minor";
    const lens = finding.lens ?? "code-reviewer";
    const location = finding.fileLevel || finding.line === void 0 ? `\`${finding.path}\`` : `\`${finding.path}:${finding.line}\``;
    const desc = (finding.rootCause ?? finding.message ?? finding.rule ?? finding.content ?? "").trim().replace(/\r?\n/g, " ").replace(/\|/g, "\\|");
    lines.push(`| **${severity}** | \`${lens}\` | ${location} | ${desc} |`);
  }
  return lines.join("\n");
}

// src/cli.ts
function writeSortedJson(value) {
  const source = value;
  const sorted = {};
  for (const key of Object.keys(source).sort()) {
    sorted[key] = source[key];
  }
  process.stdout.write(JSON.stringify(sorted) + "\n");
}
function parsePositiveInteger(value, name) {
  if (!/^[1-9]\d*$/.test(value)) {
    fail(`${name} must be a positive integer`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed)) {
    fail(`${name} must be a positive safe integer`);
  }
  return parsed;
}
function parseEnum(arg, value, allowed) {
  if (!allowed.includes(value)) {
    fail(`${arg} must be one of: ${allowed.join(", ")}`);
  }
  return value;
}
function parseCliArgs(argv) {
  const args = {};
  let i = 0;
  while (i < argv.length) {
    const arg = argv[i];
    if (arg === "--protocol-version") {
      args.protocolVersion = true;
      i++;
      continue;
    }
    if (arg === "--version") {
      args.version = true;
      i++;
      continue;
    }
    if (!arg.startsWith("-") && args.command === void 0) {
      args.command = arg;
      i++;
      continue;
    }
    if (arg === "--file-level") {
      args.fileLevel = true;
      i++;
      continue;
    }
    const next = argv[i + 1];
    const parseVal = (name) => {
      if (next === void 0 || next.startsWith("--")) {
        fail(`missing argument for ${name}`);
      }
      i += 2;
      return next;
    };
    switch (arg) {
      case "--repo":
        args.repo = parseVal(arg);
        break;
      case "--pr":
        args.pr = parsePositiveInteger(parseVal(arg), arg);
        break;
      case "--head":
        args.head = parseVal(arg);
        break;
      case "--base":
        args.base = parseVal(arg);
        break;
      case "--before":
        args.before = parseVal(arg);
        break;
      case "--engine":
        args.engine = parseEnum(arg, parseVal(arg), SUPPORTED_ENGINES);
        break;
      case "--round":
        args.round = parsePositiveInteger(parseVal(arg), arg);
        break;
      case "--fingerprint":
        args.fingerprint = parseVal(arg);
        break;
      case "--occurrence":
        args.occurrence = parsePositiveInteger(parseVal(arg), arg);
        break;
      case "--severity":
        args.severity = parseEnum(arg, parseVal(arg), SUPPORTED_SEVERITIES);
        break;
      case "--lens":
        args.lens = parseVal(arg);
        break;
      case "--outcome":
        args.outcome = parseEnum(arg, parseVal(arg), SUPPORTED_OUTCOMES);
        break;
      case "--comment-id":
        args.commentId = parsePositiveInteger(parseVal(arg), arg);
        break;
      case "--thread-id":
        args.threadId = parseVal(arg);
        break;
      case "--path":
        args.path = parseVal(arg);
        break;
      case "--line":
        args.line = parsePositiveInteger(parseVal(arg), arg);
        break;
      case "--side":
        args.side = parseEnum(arg, parseVal(arg), SUPPORTED_SIDES);
        break;
      case "--content-file":
        args.contentFile = parseVal(arg);
        break;
      case "--body-file":
        args.bodyFile = parseVal(arg);
        break;
      case "--result-file":
        args.resultFile = parseVal(arg);
        break;
      case "--result-head":
        args.resultHead = parseVal(arg);
        break;
      case "--allowed-heads-file":
        args.allowedHeadsFile = parseVal(arg);
        break;
      case "--threads-file":
        args.threadsFile = parseVal(arg);
        break;
      case "--actor":
        args.actor = parseVal(arg);
        break;
      case "--historical-comment-ids-file":
        args.historicalCommentIdsFile = parseVal(arg);
        break;
      case "--expected-result-sha256":
        args.expectedResultSha256 = parseVal(arg);
        break;
      case "--expected-threads-sha256":
        args.expectedThreadsSha256 = parseVal(arg);
        break;
      case "--blocker-file":
        args.blockerFile = parseVal(arg);
        break;
      case "--classification":
        args.classification = parseEnum(
          arg,
          parseVal(arg),
          SUPPORTED_CLASSIFICATIONS
        );
        break;
      case "--file":
        args.file = parseVal(arg);
        break;
      case "--json-file":
        args.jsonFile = parseVal(arg);
        break;
      case "--author":
        args.author = parseEnum(arg, parseVal(arg), SUPPORTED_ENGINES);
        break;
      case "--reviewers": {
        args.reviewers = parseVal(arg);
        break;
      }
      default:
        fail(`unknown argument: ${arg}`);
    }
  }
  return args;
}
function validateArgs(args) {
  if (args.protocolVersion) {
    return;
  }
  if (!args.command) {
    fail("subcommand required");
  }
  for (const name of ["head", "base", "before", "resultHead"]) {
    const val = args[name];
    if (val !== void 0) {
      requireSha(val, name === "resultHead" ? "result-head" : name);
    }
  }
  if (args.expectedResultSha256 !== void 0 && !SHA_64_RE.test(args.expectedResultSha256)) {
    fail("--expected-result-sha256 must be a lowercase SHA-256 digest");
  }
  if (args.contentFile && (args.command === "post-finding" || args.command === "reopen-occurrence" || args.command === "dispose")) {
    const required = ["engine", "round", "fingerprint"];
    if (args.command === "post-finding") {
      required.push("severity", "lens");
    }
    const missing = required.filter((r) => args[r] === void 0);
    if (missing.length > 0) {
      const names = missing.map(
        (m) => `--${String(m).replace(/([A-Z])/g, "-$1").toLowerCase()}`
      );
      fail(`v3 content mode requires ${names.join(", ")}`);
    }
  }
  if (args.command === "verify-ledger") {
    const resultFields = [
      "engine",
      "round",
      "base",
      "before",
      "resultFile"
    ];
    const present = resultFields.map((f) => args[f] !== void 0);
    if (present.some(Boolean) && !present.every(Boolean)) {
      fail(
        "verify-ledger result evidence requires --engine, --round, --base, --before, and --result-file"
      );
    }
  }
}
function runCli(argv = process.argv.slice(2)) {
  resetGitHubRunner();
  const args = parseCliArgs(argv);
  if (args.version) {
    process.stdout.write(`${PACKAGE_VERSION}
`);
    return 0;
  }
  if (args.protocolVersion) {
    process.stdout.write(`${PROTOCOL_VERSION}
`);
    return 0;
  }
  validateArgs(args);
  switch (args.command) {
    case "preflight-anchor": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.path) {
        fail("preflight-anchor requires --repo, --pr, --head, and --path");
      }
      if (args.line === void 0 && !args.fileLevel) {
        fail("preflight-anchor requires --line or --file-level");
      }
      const out = preflightAnchor({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        path: args.path,
        line: args.line,
        fileLevel: args.fileLevel,
        side: args.side
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "post-finding": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.path) {
        fail("post-finding requires --repo, --pr, --head, and --path");
      }
      if (args.line === void 0 && !args.fileLevel) {
        fail("post-finding requires --line or --file-level");
      }
      if (!args.contentFile && !args.bodyFile) {
        fail("post-finding requires --content-file or --body-file");
      }
      const out = postFinding({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        path: args.path,
        line: args.line,
        fileLevel: args.fileLevel,
        side: args.side,
        contentFile: args.contentFile,
        bodyFile: args.bodyFile,
        engine: args.engine,
        round: args.round,
        fingerprint: args.fingerprint,
        occurrence: args.occurrence,
        severity: args.severity,
        lens: args.lens
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "reopen-occurrence": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.engine || args.round === void 0 || !args.fingerprint || args.occurrence === void 0 || !args.severity || !args.lens || args.commentId === void 0 || !args.threadId || !args.contentFile) {
        fail("reopen-occurrence missing required parameters");
      }
      const out = reopenOccurrence({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        engine: args.engine,
        round: args.round,
        fingerprint: args.fingerprint,
        occurrence: args.occurrence,
        severity: args.severity,
        lens: args.lens,
        commentId: args.commentId,
        threadId: args.threadId,
        contentFile: args.contentFile
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "dispose": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.engine || args.round === void 0 || !args.fingerprint || !args.outcome || args.commentId === void 0 || !args.threadId || !args.contentFile) {
        fail("dispose missing required parameters");
      }
      const out = dispose({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        engine: args.engine,
        round: args.round,
        fingerprint: args.fingerprint,
        occurrence: args.occurrence ?? 1,
        outcome: args.outcome,
        commentId: args.commentId,
        threadId: args.threadId,
        contentFile: args.contentFile
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "reply": {
      if (!args.repo || args.pr === void 0 || !args.head || args.commentId === void 0 || !args.bodyFile) {
        fail(
          "reply requires --repo, --pr, --head, --comment-id, and --body-file"
        );
      }
      const out = reply({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        commentId: args.commentId,
        bodyFile: args.bodyFile
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "post-pr-comment": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.bodyFile) {
        fail("post-pr-comment requires --repo, --pr, --head, and --body-file");
      }
      const out = postPrComment({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        bodyFile: args.bodyFile
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "validate-result": {
      if (!args.head || !args.engine || args.round === void 0 || !args.base || !args.before || !args.resultFile) {
        fail("validate-result missing required arguments");
      }
      const out = validateResult({
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        resultHead: args.resultHead
      });
      writeSortedJson(out);
      break;
    }
    case "write-result": {
      if (!args.head || !args.engine || args.round === void 0 || !args.base || !args.before || !args.resultFile || !args.repo || args.pr === void 0) {
        fail("write-result missing required arguments");
      }
      const out = writeResult({
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        repo: args.repo,
        pr: args.pr,
        allowedHeadsFile: args.allowedHeadsFile,
        actor: args.actor,
        historicalCommentIdsFile: args.historicalCommentIdsFile,
        classification: args.classification
      });
      writeSortedJson(out);
      break;
    }
    case "write-blocked-result": {
      if (!args.head || !args.engine || args.round === void 0 || !args.base || !args.before || !args.resultFile || !args.blockerFile) {
        fail("write-blocked-result missing required arguments");
      }
      const out = writeBlockedResult({
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        blockerFile: args.blockerFile
      });
      writeSortedJson(out);
      break;
    }
    case "attest": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.engine || args.round === void 0 || !args.base || !args.before || !args.resultFile || !args.expectedResultSha256) {
        fail(
          "attest requires --repo, --pr, --head, --engine, --round, --base, --before, --result-file, and --expected-result-sha256"
        );
      }
      const out = attest({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultFile: args.resultFile,
        threadsFile: args.threadsFile,
        allowedHeadsFile: args.allowedHeadsFile,
        actor: args.actor,
        historicalCommentIdsFile: args.historicalCommentIdsFile,
        expectedResultSha256: args.expectedResultSha256,
        expectedThreadsSha256: args.expectedThreadsSha256,
        contentFile: args.contentFile
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "resolve": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.threadId) {
        fail("resolve requires --repo, --pr, --head, and --thread-id");
      }
      const out = resolve({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        threadId: args.threadId
      });
      process.stdout.write(JSON.stringify(out) + "\n");
      break;
    }
    case "reconcile": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.fingerprint) {
        fail("reconcile requires --repo, --pr, --head, and --fingerprint");
      }
      const out = reconcile({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        fingerprint: args.fingerprint
      });
      writeSortedJson(out);
      break;
    }
    case "verify-ledger": {
      if (!args.repo || args.pr === void 0 || !args.head) {
        fail("verify-ledger requires --repo, --pr, and --head");
      }
      const out = verifyLedger({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        threadsFile: args.threadsFile,
        actor: args.actor,
        historicalCommentIdsFile: args.historicalCommentIdsFile,
        engine: args.engine,
        round: args.round,
        base: args.base,
        before: args.before,
        resultHead: args.resultHead,
        resultFile: args.resultFile,
        allowedHeadsFile: args.allowedHeadsFile,
        expectedThreadsSha256: args.expectedThreadsSha256
      });
      writeSortedJson(out);
      break;
    }
    case "post-roster": {
      if (!args.repo || args.pr === void 0 || !args.head || !args.author || args.reviewers === void 0 || !args.contentFile) {
        fail(
          "post-roster requires --repo, --pr, --head, --author, --reviewers, and --content-file"
        );
      }
      const out = postRoster({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        actor: args.actor,
        author: args.author,
        reviewers: parseReviewers(args.reviewers, args.author),
        content: readContent(args.contentFile)
      });
      writeSortedJson(out);
      break;
    }
    case "read-roster": {
      if (!args.repo || args.pr === void 0) {
        fail("read-roster requires --repo and --pr");
      }
      const out = readRoster({
        repo: args.repo,
        pr: args.pr,
        actor: args.actor
      });
      writeSortedJson(out);
      break;
    }
    case "coverage":
    case "verify-coverage": {
      if (!args.repo || args.pr === void 0 || !args.head) {
        fail(`${args.command} requires --repo, --pr, and --head`);
      }
      const run = args.command === "coverage" ? coverage : verifyCoverage;
      const out = run({
        repo: args.repo,
        pr: args.pr,
        head: args.head,
        actor: args.actor
      });
      writeSortedJson(out);
      break;
    }
    case "read-result": {
      const targetFile = args.file ?? args.resultFile;
      if (!targetFile) {
        fail("read-result requires --file or --result-file");
      }
      const out = readResult(targetFile);
      process.stdout.write(JSON.stringify(out, null, 2) + "\n");
      break;
    }
    case "format-findings": {
      const jsonPath = args.jsonFile ?? args.file;
      if (!jsonPath) {
        fail("format-findings requires --json-file or --file");
      }
      assertRegularFile(
        jsonPath,
        "findings file must be a regular non-symlink file"
      );
      const parsed = parseJsonOrFail(
        readFileSync4(jsonPath, "utf8"),
        "findings file must contain valid UTF-8 JSON"
      );
      if (!Array.isArray(parsed) || parsed.some(
        (row) => typeof row !== "object" || row === null || Array.isArray(row)
      )) {
        fail("findings file must contain a JSON array of finding objects");
      }
      const formatted = formatFindings(parsed);
      process.stdout.write(formatted + "\n");
      break;
    }
    default:
      fail(`unknown command: ${args.command}`);
  }
  return 0;
}

// src/bin.ts
try {
  const exitCode = runCli();
  if (exitCode !== 0) {
    process.exitCode = exitCode;
  }
} catch (error) {
  const err = error;
  const kind = err.name === "LedgerError" ? "" : "unexpected error: ";
  const cause = err.cause === void 0 ? "" : `
  caused by: ${err.cause?.message ?? String(err.cause)}`;
  console.error(
    `review-ledger: ${kind}${err.message ?? String(error)}${cause}`
  );
  process.exitCode = 1;
}
