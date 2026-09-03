#!/usr/bin/env node

// src/cli.ts
import { readFileSync as readFileSync4 } from "fs";

// src/constants.ts
var PROTOCOL_VERSION = 3;
var PACKAGE_VERSION = true ? "1.3.0" : "0.0.0-dev";
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
var TELEMETRY_VERSION = 1;
var TELEMETRY_MARKER_PREFIX = "<!-- local-review-telemetry:";
var TELEMETRY_V1_MARKER = "<!-- local-review-telemetry:v1 -->";
var OPEN_TOKEN_RE = /^[a-z0-9-]+$/;
var PROVIDER_BUCKET_KEY_RE = /^[a-z0-9_]+$/;
var UTC_TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
var REPO_RE = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;
var TELEMETRY_PASS_TYPES = [
  "review",
  "refactor",
  "hosted"
];
var TELEMETRY_REVIEW_TIERS = [
  "lean",
  "deep"
];
var TELEMETRY_TRIGGERS = [
  "autonomous",
  "interactive"
];
var TELEMETRY_STANCES = [
  "adversarial",
  "convergence"
];
var TELEMETRY_STATUSES = [
  "clean",
  "changed",
  "blocked",
  "skipped"
];
var TELEMETRY_TOKEN_SOURCES = [
  "session-log-delta",
  "stream-json",
  "unscoped-session",
  "unavailable"
];
var CANONICAL_TOKEN_BUCKETS = [
  "input",
  "output",
  "cacheRead",
  "cacheWrite",
  "reasoning"
];

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
  actorOverride = null;
  cachedActor = null;
  constructor(customActor) {
    this.setActor(customActor ?? null);
  }
  setActor(actor) {
    this.actorOverride = actor ? requireToken(actor, "actor") : null;
    this.cachedActor = null;
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
    this.cachedActor ??= this.liveActor();
    return this.cachedActor;
  }
  /**
   * Resolve the actor bypassing the cache.
   *
   * An explicit pin outranks the session, so an `actorOverride` is returned
   * without a lookup. That is deliberate, and it means a runner constructed
   * with an actor performs no live check at all — seeding an actor disables
   * rotation detection.
   */
  liveActor() {
    if (this.actorOverride !== null) {
      return this.actorOverride;
    }
    return resolveLoginOrFail(this.runGh(["api", "user"]));
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
  return assertActorPins(login);
}
function assertActorPins(login, expected) {
  if (!login) {
    fail("GitHub returned an empty authenticated user");
  }
  const rawEnvironmentActor = process.env[EXPECTED_ACTOR_ENV];
  if (rawEnvironmentActor !== void 0) {
    const environmentActor = requireToken(rawEnvironmentActor, "actor");
    if (login !== environmentActor) {
      fail(
        `authenticated GitHub actor changed: expected ${environmentActor}, found ${login}`
      );
    }
  }
  if (expected !== void 0 && requireToken(expected, "actor") !== login) {
    fail(
      `authenticated GitHub actor changed: expected ${expected}, found ${login}`
    );
  }
  return login;
}
function assertActor(expected) {
  return assertActorPins(currentActor(), expected);
}
function assertLiveActor(expected) {
  if (!activeRunner.liveActor && activeRunner.currentActor) {
    fail("GitHub runner cannot re-resolve the live authenticated actor");
  }
  const actor = activeRunner.liveActor ? activeRunner.liveActor() : resolveLoginOrFail(runGh(["api", "user"]));
  return assertActorPins(actor, expected);
}
function authenticatedRows(rows, options) {
  const actor = options?.actor === void 0 ? currentActor() : assertLiveActor(options.actor);
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
function getIssueComments(repo, pr, expectedActor) {
  if (expectedActor === void 0) {
    return authenticatedRows(getAllIssueComments(repo, pr));
  }
  const actor = assertLiveActor(expectedActor);
  const rows = getAllIssueComments(repo, pr);
  return authenticatedRows(rows, { actor });
}
function getAllIssueComments(repo, pr) {
  const pages = jsonOutput([
    "api",
    "--paginate",
    "--slurp",
    `repos/${repo}/issues/${pr}/comments?per_page=100`
  ]);
  return flattenPages(pages, "PR-comments");
}
function verifyComment(repo, commentId, expectedBody) {
  verifyOwnedComment(
    `repos/${repo}/pulls/comments/${commentId}`,
    commentId,
    expectedBody,
    "review comment"
  );
}
function verifyIssueComment(repo, commentId, expectedBody, expectedActor) {
  verifyOwnedComment(
    `repos/${repo}/issues/comments/${commentId}`,
    commentId,
    expectedBody,
    "PR comment",
    expectedActor
  );
}
function verifyOwnedComment(endpoint, commentId, expectedBody, label, expectedActor) {
  const actor = expectedActor === void 0 ? assertActor() : assertLiveActor(expectedActor);
  const response = jsonOutput(["api", endpoint]);
  if (expectedActor !== void 0) {
    assertLiveActor(actor);
  }
  const user = response["user"] ?? response["author"];
  if (typeof response !== "object" || response === null || response["body"] !== expectedBody || typeof user !== "object" || user === null || user.login !== actor) {
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
  return getAllIssueComments(repo, pr).some((row) => row["id"] === commentId);
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

// src/effect.ts
var DOCS_CONFIG_EXTENSIONS = /* @__PURE__ */ new Set([
  ".md",
  ".mdx",
  ".txt",
  ".rst",
  ".yml",
  ".yaml",
  ".json",
  ".toml",
  ".ini",
  ".csv"
]);
var DOCS_CONFIG_BASENAMES = /* @__PURE__ */ new Set([
  "LICENSE",
  "NOTICE",
  "CHANGELOG",
  "README",
  ".gitignore",
  ".gitattributes",
  ".env.example"
]);
var PROMPT_SURFACE_RE = /(^|\/)\.(claude|codex|agents)\//;
var EXECUTING_PATH_RES = [
  /(^|\/)\.github\/workflows\//,
  /(^|\/)\.github\/actions\//,
  /(^|\/)CODEOWNERS$/,
  /(^|\/)package\.json$/,
  /(^|\/)(pnpm-lock\.yaml|package-lock\.json|yarn\.lock)$/
];
var DOCS_DIR_RE = /(^|\/)docs\//;
var TEST_PATH_RES = [
  /(^|\/)__tests__\//,
  /(^|\/)tests?\//,
  /(^|\/)spec\//,
  /\.(test|spec)\.[cm]?[jt]sx?$/,
  /(^|\/)test_[^/]+\.py$/,
  /_test\.(go|py|rb)$/
];
var COMMENT_SYNTAX = /* @__PURE__ */ new Map([
  [".ts", { line: ["//"], block: [["/*", "*/"]] }],
  [".tsx", { line: ["//"], block: [["/*", "*/"]] }],
  [".js", { line: ["//"], block: [["/*", "*/"]] }],
  [".jsx", { line: ["//"], block: [["/*", "*/"]] }],
  [".mjs", { line: ["//"], block: [["/*", "*/"]] }],
  [".cjs", { line: ["//"], block: [["/*", "*/"]] }],
  [".go", { line: ["//"], block: [["/*", "*/"]] }],
  [".rs", { line: ["//"], block: [["/*", "*/"]] }],
  [".java", { line: ["//"], block: [["/*", "*/"]] }],
  [".kt", { line: ["//"], block: [["/*", "*/"]] }],
  [".swift", { line: ["//"], block: [["/*", "*/"]] }],
  [".c", { line: ["//"], block: [["/*", "*/"]] }],
  [".h", { line: ["//"], block: [["/*", "*/"]] }],
  [".cpp", { line: ["//"], block: [["/*", "*/"]] }],
  [".cs", { line: ["//"], block: [["/*", "*/"]] }],
  [".tf", { line: ["#", "//"], block: [["/*", "*/"]] }],
  [".tfvars", { line: ["#", "//"], block: [["/*", "*/"]] }],
  [".hcl", { line: ["#", "//"], block: [["/*", "*/"]] }],
  [".py", { line: ["#"], block: [] }],
  [".rb", { line: ["#"], block: [] }],
  [".sh", { line: ["#"], block: [] }],
  [".bash", { line: ["#"], block: [] }],
  [".sql", { line: ["--"], block: [["/*", "*/"]] }]
]);
function extensionOf(path) {
  const base = path.slice(path.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  return dot <= 0 ? "" : base.slice(dot).toLowerCase();
}
function basenameOf(path) {
  return path.slice(path.lastIndexOf("/") + 1);
}
function isPromptSurface(path) {
  return PROMPT_SURFACE_RE.test(`/${path}`);
}
function isExecutingPath(path) {
  return EXECUTING_PATH_RES.some((pattern) => pattern.test(`/${path}`));
}
function isDocsOrConfig(path) {
  if (isPromptSurface(path) || isExecutingPath(path)) {
    return false;
  }
  if (DOCS_DIR_RE.test(`/${path}`) && !COMMENT_SYNTAX.has(extensionOf(path))) {
    return true;
  }
  const base = basenameOf(path);
  if (DOCS_CONFIG_BASENAMES.has(base)) {
    return true;
  }
  return DOCS_CONFIG_EXTENSIONS.has(extensionOf(path));
}
function isTestPath(path) {
  if (isPromptSurface(path) || isExecutingPath(path)) {
    return false;
  }
  return TEST_PATH_RES.some((pattern) => pattern.test(`/${path}`));
}
function classifyLine(text, syntax, inBlock) {
  let rest = text.trim();
  if (inBlock) {
    const close = syntax.block[0]?.[1];
    if (close === void 0) {
      return { inert: false, inBlock: false };
    }
    const end = rest.indexOf(close);
    if (end === -1) {
      return { inert: true, inBlock: true };
    }
    rest = rest.slice(end + close.length).trim();
    if (rest === "") {
      return { inert: true, inBlock: false };
    }
    return { inert: false, inBlock: false };
  }
  if (rest === "") {
    return { inert: true, inBlock: false };
  }
  if (syntax.line.some((marker) => rest.startsWith(marker))) {
    return { inert: true, inBlock: false };
  }
  for (const [open, close] of syntax.block) {
    if (!rest.startsWith(open)) {
      continue;
    }
    const end = rest.indexOf(close, open.length);
    if (end === -1) {
      return { inert: true, inBlock: true };
    }
    const tail = rest.slice(end + close.length).trim();
    return { inert: tail === "", inBlock: false };
  }
  return { inert: false, inBlock: false };
}
function isCommentOnlyPatch(patch, syntax) {
  let leftBlock = false;
  let rightBlock = false;
  let inHunk = false;
  for (const raw of patch.split(/\r?\n/)) {
    if (raw.startsWith("@@")) {
      inHunk = true;
      leftBlock = false;
      rightBlock = false;
      continue;
    }
    if (!inHunk || raw.startsWith("\\ No newline")) {
      continue;
    }
    const prefix = raw.slice(0, 1);
    const text = raw.slice(1);
    if (prefix === "-") {
      const result = classifyLine(text, syntax, leftBlock);
      leftBlock = result.inBlock;
      if (!result.inert) {
        return false;
      }
    } else if (prefix === "+") {
      const result = classifyLine(text, syntax, rightBlock);
      rightBlock = result.inBlock;
      if (!result.inert) {
        return false;
      }
    }
  }
  return true;
}
function classifyRangeEffect(before, after) {
  if (before === after) {
    return "non-behavioral";
  }
  const runner = getGitHubRunner();
  if (!runner.runGit) {
    return "behavioral";
  }
  const status = runner.runGit(["diff", "--name-status", "--no-renames", `${before}..${after}`]).trim();
  if (status === "") {
    return "non-behavioral";
  }
  const paths = [];
  for (const row of status.split(/\r?\n/)) {
    if (row === "") {
      continue;
    }
    const [code, path] = row.split("	", 2);
    if (code === void 0 || path === void 0 || !/^[AMD]$/.test(code)) {
      return "behavioral";
    }
    paths.push(path);
  }
  for (const path of paths) {
    if (isDocsOrConfig(path) || isTestPath(path)) {
      continue;
    }
    const syntax = COMMENT_SYNTAX.get(extensionOf(path));
    if (syntax === void 0) {
      return "behavioral";
    }
    const patch = runner.runGit([
      "diff",
      "--unified=0",
      "--no-renames",
      `${before}..${after}`,
      "--",
      path
    ]);
    if (!isCommentOnlyPatch(patch, syntax)) {
      return "behavioral";
    }
  }
  return "non-behavioral";
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
  if (data.classification === "minor" && classifyRangeEffect(args.before, resultHead(args)) === "behavioral") {
    fail("minor classification requires a non-behavioral change range");
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
  if (changed && params.classification === "minor" && classifyRangeEffect(params.before, params.head) === "behavioral") {
    fail("minor classification requires a non-behavioral change range");
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

// src/changeset.ts
var CHANGESET_CLASSIFIER_VERSION = 1;
var DEFAULT_PROMPT_SURFACES = [
  ".claude/",
  ".codex/",
  ".agents/",
  ".gemini/",
  ".github/copilot-instructions.md",
  "AGENTS.md",
  "CLAUDE.md",
  "GEMINI.md"
];
var GENERATED_BASENAMES = /* @__PURE__ */ new Set([
  "pnpm-lock.yaml",
  "package-lock.json",
  "npm-shrinkwrap.json",
  "yarn.lock",
  "bun.lockb",
  "cargo.lock",
  "poetry.lock",
  "uv.lock",
  "gemfile.lock",
  "composer.lock",
  "go.sum",
  "flake.lock",
  "podfile.lock",
  "pubspec.lock"
]);
var GENERATED_PREFIXES = [
  "dist/",
  "build/",
  "vendor/",
  "node_modules/",
  "target/debug/",
  "target/release/"
];
var GENERATED_SEGMENTS = ["/dist/", "/build/", "/vendor/"];
var CONFIG_BASENAMES = /* @__PURE__ */ new Set([
  "package.json",
  "pnpm-workspace.yaml",
  "lerna.json",
  "turbo.json",
  "nx.json",
  "pyproject.toml",
  "setup.py",
  "setup.cfg",
  "cargo.toml",
  "go.mod",
  "gemfile",
  "podfile",
  "pubspec.yaml",
  "pom.xml",
  "build.gradle",
  "build.gradle.kts",
  "composer.json",
  "dockerfile",
  "docker-compose.yml",
  "docker-compose.yaml",
  "makefile",
  "justfile",
  "procfile",
  ".gitlab-ci.yml",
  ".platform-config.yml"
]);
var CONFIG_PREFIXES = [
  ".github/workflows/",
  ".github/actions/",
  "helm/",
  "charts/",
  "k8s/",
  "kubernetes/",
  "deploy/",
  "terraform/",
  "infra/",
  "migrations/",
  "db/migrations/",
  "prisma/"
];
var CONFIG_SEGMENTS = ["/migrations/", "/helm/"];
var CONFIG_EXTENSIONS = /* @__PURE__ */ new Set([
  "tf",
  "tfvars",
  "sql",
  "prisma",
  "graphql",
  "gql",
  "proto"
]);
var DOCS_BASENAMES = /* @__PURE__ */ new Set([
  "license",
  "license.md",
  "license.txt",
  "notice",
  "changelog",
  "changelog.md",
  "readme",
  "readme.md",
  "authors",
  "codeowners",
  ".gitignore",
  ".gitattributes",
  ".prettierignore",
  ".npmignore",
  ".editorconfig",
  ".env.example"
]);
var DOCS_EXTENSIONS = /* @__PURE__ */ new Set([
  "md",
  "mdx",
  "txt",
  "rst",
  "adoc"
]);
var DOCS_PREFIXES = ["docs/", "handbook/"];
var DOCS_SEGMENTS = ["/docs/", "/__snapshots__/"];
var TEST_SEGMENTS = [
  "/__tests__/",
  "/tests/",
  "/test/",
  "/spec/"
];
var TEST_PREFIXES = [
  "__tests__/",
  "tests/",
  "test/",
  "spec/",
  "e2e/"
];
var TEST_BASENAMES = /* @__PURE__ */ new Set(["conftest.py"]);
var LANGUAGE_BY_EXTENSION = {
  ts: "ts",
  tsx: "tsx",
  js: "js",
  jsx: "jsx",
  mjs: "js",
  cjs: "js",
  py: "py",
  rs: "rs",
  go: "go",
  java: "java",
  kt: "kt",
  kts: "kt",
  swift: "swift",
  rb: "rb",
  cs: "cs",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  sh: "sh",
  bash: "sh",
  sql: "sql",
  tf: "tf",
  tfvars: "tf",
  prisma: "prisma",
  graphql: "graphql",
  gql: "graphql",
  proto: "proto",
  yml: "yaml",
  yaml: "yaml",
  json: "json",
  toml: "toml",
  md: "md",
  mdx: "md",
  vue: "vue",
  svelte: "svelte",
  dart: "dart",
  php: "php",
  scala: "scala"
};
function normalizePath(path) {
  return path.replace(/\\/g, "/").replace(/^\.\//, "");
}
function basename(path) {
  return path.slice(path.lastIndexOf("/") + 1);
}
function extensionOf2(path) {
  const name = basename(path);
  const dot = name.lastIndexOf(".");
  if (dot <= 0) {
    return "";
  }
  return name.slice(dot + 1).toLowerCase();
}
function hasPrefix(path, prefixes) {
  return prefixes.some((prefix) => path.startsWith(prefix));
}
function hasSegment(path, segments) {
  return segments.some((segment) => path.includes(segment));
}
function isGenerated(path, name) {
  return GENERATED_BASENAMES.has(name) || hasPrefix(path, GENERATED_PREFIXES) || hasSegment(path, GENERATED_SEGMENTS) || /\.min\.(js|css)$/.test(name) || /\.bundle\.(js|mjs|cjs)$/.test(name) || /\.generated\.[^.]+$/.test(name) || /\.snap$/.test(name) || /\.pb\.go$/.test(name) || /_pb2(_grpc)?\.py$/.test(name) || /\.g\.dart$/.test(name);
}
function isTest(path, name) {
  return TEST_BASENAMES.has(name) || hasPrefix(path, TEST_PREFIXES) || hasSegment(path, TEST_SEGMENTS) || /\.(test|spec)\.[^.]+$/.test(name) || /_test\.(go|py|rb)$/.test(name);
}
function isFixture(path, name) {
  return /\.fixture\.[^.]+$/.test(name) || path.startsWith("fixtures/") || path.includes("/fixtures/");
}
function isConfig(path, name, extension) {
  return CONFIG_BASENAMES.has(name) || CONFIG_EXTENSIONS.has(extension) || hasPrefix(path, CONFIG_PREFIXES) || hasSegment(path, CONFIG_SEGMENTS) || /^dockerfile(\.|$)/.test(name) || /^tsconfig(\.[^.]+)?\.json$/.test(name) || /^requirements(-[^.]+)?\.txt$/.test(name);
}
function isDocs(path, name, extension) {
  return DOCS_BASENAMES.has(name) || DOCS_EXTENSIONS.has(extension) || hasPrefix(path, DOCS_PREFIXES) || hasSegment(path, DOCS_SEGMENTS);
}
function classifyPath(rawPath, options) {
  const path = normalizePath(rawPath);
  const name = basename(path).toLowerCase();
  const extension = extensionOf2(path);
  const promptSurfaces = options?.promptSurfaces ?? DEFAULT_PROMPT_SURFACES;
  const language = LANGUAGE_BY_EXTENSION[extension] ?? null;
  const classify = (cls, reviewSignificant) => ({ path, class: cls, reviewSignificant, language });
  if (isGenerated(path, name)) {
    return classify("generated", GENERATED_BASENAMES.has(name));
  }
  if (isTest(path, name)) {
    return classify("test", true);
  }
  if (promptSurfaces.some(
    (surface) => path === surface || surface.endsWith("/") && path.startsWith(surface) || path.endsWith(`/${surface}`)
  )) {
    return classify("app", true);
  }
  if (isFixture(path, name)) {
    return classify("docsConfig", false);
  }
  if (isConfig(path, name, extension)) {
    return classify("app", true);
  }
  if (isDocs(path, name, extension)) {
    return classify("docsConfig", false);
  }
  return classify("app", true);
}
function emptyChangeset() {
  return {
    classifierVersion: CHANGESET_CLASSIFIER_VERSION,
    reviewSignificantFiles: 0,
    files: { app: 0, test: 0, docsConfig: 0, generated: 0 },
    linesChanged: {
      app: 0,
      test: 0,
      comment: null,
      docsConfig: 0,
      generated: 0,
      blank: 0
    },
    linesByLanguage: {}
  };
}
function classifyFiles(files, options) {
  const changeset = emptyChangeset();
  const classifications = [];
  for (const file of files) {
    const classification = classifyPath(file.path, options);
    classifications.push(classification);
    if (classification.reviewSignificant) {
      changeset.reviewSignificantFiles += 1;
    }
    changeset.files[classification.class] += 1;
    if (!Number.isSafeInteger(file.added) || file.added < 0 || !Number.isSafeInteger(file.deleted) || file.deleted < 0) {
      fail(`changed file ${file.path} reports an invalid churn count`);
    }
    const churn = file.added + file.deleted;
    if (!Number.isSafeInteger(churn)) {
      fail(`changed file ${file.path} reports an invalid churn total`);
    }
    const blank = file.blank ?? 0;
    if (!Number.isSafeInteger(blank) || blank < 0 || blank > churn) {
      fail(`changed file ${file.path} reports an invalid blank count`);
    }
    const counted = churn - blank;
    changeset.linesChanged.blank += blank;
    changeset.linesChanged[classification.class] += counted;
    if (classification.class !== "generated" && classification.language) {
      const key = classification.language;
      changeset.linesByLanguage[key] = (changeset.linesByLanguage[key] ?? 0) + counted;
    }
  }
  return {
    changeset,
    classifications,
    reviewSignificantFiles: changeset.reviewSignificantFiles,
    skip: changeset.reviewSignificantFiles === 0
  };
}
function unquotePath(raw) {
  if (!raw.startsWith('"') || !raw.endsWith('"')) {
    return raw;
  }
  const inner = raw.slice(1, -1);
  const bytes = [];
  const simple = {
    n: 10,
    t: 9,
    r: 13,
    '"': 34,
    "\\": 92
  };
  for (let i = 0; i < inner.length; i++) {
    const char = inner[i];
    if (char !== "\\") {
      bytes.push(...Buffer.from(char, "utf8"));
      continue;
    }
    const next = inner[i + 1];
    if (next === void 0) {
      bytes.push(92);
      continue;
    }
    const octal = inner.slice(i + 1, i + 4);
    if (/^[0-7]{3}$/.test(octal)) {
      bytes.push(parseInt(octal, 8));
      i += 3;
      continue;
    }
    const mapped = simple[next];
    if (mapped === void 0) {
      bytes.push(...Buffer.from(next, "utf8"));
    } else {
      bytes.push(mapped);
    }
    i += 1;
  }
  return Buffer.from(bytes).toString("utf8");
}
function headerPath(rest) {
  const quoted = /^("(?:[^"\\]|\\.)*") ("(?:[^"\\]|\\.)*")$/.exec(rest);
  if (quoted) {
    return stripSide(quoted[2]);
  }
  const splits = [];
  let split = rest.indexOf(" b/");
  while (split !== -1) {
    splits.push(split);
    split = rest.indexOf(" b/", split + 1);
  }
  if (splits.length !== 1) {
    return null;
  }
  return stripSide(rest.slice(splits[0] + 1));
}
function stripSide(raw) {
  if (raw === "/dev/null") {
    return null;
  }
  const unquoted = unquotePath(raw);
  return unquoted.replace(/^[ab]\//, "");
}
function markerPath(raw) {
  const trimmed = raw.trim();
  if (trimmed.startsWith('"')) {
    const quoted = /^("(?:[^"\\]|\\.)*")/.exec(trimmed);
    return quoted ? stripSide(quoted[1]) : null;
  }
  return stripSide(trimmed.split("	", 1)[0]);
}
function parseDiffPatch(patch) {
  const files = [];
  let current = null;
  let inHunk = false;
  let headerComplete = false;
  let currentFromGit = false;
  let oldLinesRemaining = 0;
  let newLinesRemaining = 0;
  const push = () => {
    if (current !== null) {
      if (!headerComplete) {
        fail("unified diff contains an incomplete file header");
      }
      files.push(current);
    }
  };
  const lines = patch.split(/\r?\n/);
  for (let index = 0; index < lines.length; index++) {
    const rawLine = lines[index];
    if (rawLine.startsWith("diff --git ")) {
      push();
      current = {
        path: headerPath(rawLine.slice("diff --git ".length).trim()) ?? "",
        added: 0,
        deleted: 0,
        blank: 0
      };
      inHunk = false;
      headerComplete = true;
      currentFromGit = true;
      continue;
    }
    if (!inHunk && !currentFromGit && rawLine.startsWith("--- ") && lines[index + 1]?.startsWith("+++ ")) {
      if (current !== null) {
        push();
      }
      const left = markerPath(rawLine.slice(4));
      const right = markerPath(lines[index + 1].slice(4));
      current = { path: right ?? left ?? "", added: 0, deleted: 0, blank: 0 };
      headerComplete = true;
      currentFromGit = false;
      index += 1;
      continue;
    }
    if (current === null) {
      continue;
    }
    if (!inHunk && rawLine.startsWith("--- ")) {
      const left = stripSide(rawLine.slice(4).trim());
      if (left !== null) {
        current.path = left;
      }
      continue;
    }
    if (!inHunk && rawLine.startsWith("+++ ")) {
      const right = markerPath(rawLine.slice(4));
      if (right !== null) {
        current.path = right;
      }
      headerComplete = true;
      continue;
    }
    if (rawLine.startsWith("@@")) {
      const range = /^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@/.exec(rawLine);
      if (!range) {
        fail("unified diff contains an unreadable hunk header");
      }
      oldLinesRemaining = Number(range[1] ?? "1");
      newLinesRemaining = Number(range[2] ?? "1");
      inHunk = true;
      continue;
    }
    if (!inHunk || rawLine.startsWith("\\ No newline")) {
      continue;
    }
    const prefix = rawLine.slice(0, 1);
    if (prefix === " ") {
      oldLinesRemaining -= 1;
      newLinesRemaining -= 1;
    } else if (prefix === "+") {
      newLinesRemaining -= 1;
    } else if (prefix === "-") {
      oldLinesRemaining -= 1;
    } else {
      continue;
    }
    if (prefix === "+") {
      current.added += 1;
    } else if (prefix === "-") {
      current.deleted += 1;
    }
    if ((prefix === "+" || prefix === "-") && rawLine.slice(1).trim() === "") {
      current.blank = (current.blank ?? 0) + 1;
    }
    if (oldLinesRemaining < 0 || newLinesRemaining < 0) {
      fail("unified diff hunk contains more lines than its header declares");
    }
    inHunk = oldLinesRemaining > 0 || newLinesRemaining > 0;
  }
  if (inHunk) {
    fail("unified diff hunk ended before its declared line counts");
  }
  push();
  if (files.length === 0 && patch.trim() !== "") {
    fail("unified diff contains no readable file records");
  }
  const named = files.filter((file) => file.path !== "");
  if (named.length !== files.length) {
    fail("unified diff contains a file header with no readable path");
  }
  return named;
}
function classifyRange(params) {
  requireSha(params.base, "base");
  requireSha(params.head, "head");
  const patch = runGit([
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--find-renames",
    `${params.base}..${params.head}`
  ]);
  return classifyFiles(parseDiffPatch(patch), params.options);
}

// src/telemetry.ts
function isTelemetryComment(body) {
  return body.includes(TELEMETRY_MARKER_PREFIX);
}
function requireEnum(value, allowed, field) {
  if (typeof value !== "string" || !allowed.includes(value)) {
    fail(`telemetry ${field} must be one of: ${allowed.join(", ")}`);
  }
  return value;
}
function requireCount(value, field) {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0 || Object.is(value, -0)) {
    fail(`telemetry ${field} must be a non-negative integer`);
  }
  return value;
}
function requireNullableCount(value, field) {
  if (value === null) {
    return null;
  }
  return requireCount(value, field);
}
function requireNullableToken(value, field) {
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !TOKEN_RE.test(value)) {
    fail(`telemetry ${field} must be a protocol token or null`);
  }
  return value;
}
function requireNullableSha256(value, field) {
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !SHA_64_RE.test(value)) {
    fail(`telemetry ${field} must be a lowercase SHA-256 digest or null`);
  }
  return value;
}
function requireObject(value, field) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail(`telemetry ${field} must be an object`);
  }
  return value;
}
function validateProviderBuckets(value) {
  const source = requireObject(value, "providerBuckets");
  const buckets = {};
  for (const [key, raw] of Object.entries(source)) {
    if (!PROVIDER_BUCKET_KEY_RE.test(key)) {
      fail(
        `telemetry providerBuckets key must match ${PROVIDER_BUCKET_KEY_RE}`
      );
    }
    if (CANONICAL_TOKEN_BUCKETS.includes(key)) {
      fail(
        `telemetry providerBuckets must not restate the canonical bucket ${key}`
      );
    }
    buckets[key] = requireCount(raw, `providerBuckets.${key}`);
  }
  return buckets;
}
function validateTokenBucket(value) {
  const source = requireObject(value, "tokens[]");
  const model = source["model"];
  if (typeof model !== "string" || !TOKEN_RE.test(model)) {
    fail("telemetry tokens[].model must be a protocol token");
  }
  return {
    model,
    effort: requireNullableToken(source["effort"], "tokens[].effort"),
    input: requireNullableCount(source["input"], "tokens[].input"),
    output: requireNullableCount(source["output"], "tokens[].output"),
    cacheRead: requireNullableCount(source["cacheRead"], "tokens[].cacheRead"),
    cacheWrite: requireNullableCount(
      source["cacheWrite"],
      "tokens[].cacheWrite"
    ),
    reasoning: requireNullableCount(source["reasoning"], "tokens[].reasoning"),
    providerBuckets: validateProviderBuckets(source["providerBuckets"])
  };
}
function validateLane(value) {
  const source = requireObject(value, "lanes[]");
  const lens = source["lens"];
  if (typeof lens !== "string" || !TOKEN_RE.test(lens)) {
    fail("telemetry lanes[].lens must be a protocol token");
  }
  return {
    lens,
    model: requireNullableToken(source["model"], "lanes[].model"),
    input: requireNullableCount(source["input"], "lanes[].input"),
    output: requireNullableCount(source["output"], "lanes[].output"),
    cacheRead: requireNullableCount(source["cacheRead"], "lanes[].cacheRead"),
    cacheWrite: requireNullableCount(
      source["cacheWrite"],
      "lanes[].cacheWrite"
    ),
    reasoning: requireNullableCount(source["reasoning"], "lanes[].reasoning")
  };
}
function validateChangeset(value) {
  const source = requireObject(value, "changeset");
  const files = requireObject(source["files"], "changeset.files");
  const lines = requireObject(source["linesChanged"], "changeset.linesChanged");
  const byLanguage = requireObject(
    source["linesByLanguage"],
    "changeset.linesByLanguage"
  );
  const linesChanged = {
    app: requireCount(lines["app"], "changeset.linesChanged.app"),
    test: requireCount(lines["test"], "changeset.linesChanged.test"),
    comment: requireNullableCount(
      lines["comment"],
      "changeset.linesChanged.comment"
    ),
    docsConfig: requireCount(
      lines["docsConfig"],
      "changeset.linesChanged.docsConfig"
    ),
    generated: requireCount(
      lines["generated"],
      "changeset.linesChanged.generated"
    ),
    blank: requireCount(lines["blank"], "changeset.linesChanged.blank")
  };
  const languages = {};
  for (const [key, raw] of Object.entries(byLanguage)) {
    if (!OPEN_TOKEN_RE.test(key)) {
      fail(
        `telemetry changeset.linesByLanguage key must match ${OPEN_TOKEN_RE}`
      );
    }
    languages[key] = requireCount(raw, `changeset.linesByLanguage.${key}`);
  }
  const validated = {
    classifierVersion: requireCount(
      source["classifierVersion"],
      "changeset.classifierVersion"
    ),
    reviewSignificantFiles: requireCount(
      source["reviewSignificantFiles"],
      "changeset.reviewSignificantFiles"
    ),
    files: {
      app: requireCount(files["app"], "changeset.files.app"),
      test: requireCount(files["test"], "changeset.files.test"),
      docsConfig: requireCount(
        files["docsConfig"],
        "changeset.files.docsConfig"
      ),
      generated: requireCount(files["generated"], "changeset.files.generated")
    },
    linesChanged,
    linesByLanguage: languages
  };
  const totalFiles = Object.values(validated.files).reduce(
    (total, count) => total + count,
    0
  );
  const requiredReviewFiles = validated.files.app + validated.files.test;
  if (!Number.isSafeInteger(totalFiles) || !Number.isSafeInteger(requiredReviewFiles)) {
    fail("telemetry changeset file totals must be safe integers");
  }
  if (validated.reviewSignificantFiles < requiredReviewFiles || validated.reviewSignificantFiles > totalFiles) {
    fail(
      "telemetry changeset.reviewSignificantFiles must cover app/test files and not exceed total files"
    );
  }
  return validated;
}
function validateFindings(value) {
  const source = requireObject(value, "findings");
  const ladder = requireObject(
    source["bySeverityAndOutcome"],
    "findings.bySeverityAndOutcome"
  );
  const unknown = Object.keys(ladder).filter(
    (key) => !SUPPORTED_SEVERITIES.includes(key)
  );
  if (unknown.length > 0) {
    fail(
      `telemetry findings.bySeverityAndOutcome must use the severity ladder: ${SUPPORTED_SEVERITIES.join(", ")}`
    );
  }
  const bySeverityAndOutcome = {};
  let counted = 0;
  for (const severity of SUPPORTED_SEVERITIES) {
    const row = requireObject(
      ladder[severity],
      `findings.bySeverityAndOutcome.${severity}`
    );
    const outcomes = {
      validFixed: requireCount(
        row["validFixed"],
        `findings.bySeverityAndOutcome.${severity}.validFixed`
      ),
      validDeferred: requireCount(
        row["validDeferred"],
        `findings.bySeverityAndOutcome.${severity}.validDeferred`
      ),
      invalidDismissed: requireCount(
        row["invalidDismissed"],
        `findings.bySeverityAndOutcome.${severity}.invalidDismissed`
      )
    };
    counted += outcomes.validFixed + outcomes.validDeferred + outcomes.invalidDismissed;
    bySeverityAndOutcome[severity] = outcomes;
  }
  const posted = requireCount(source["posted"], "findings.posted");
  if (counted > posted) {
    fail("telemetry findings dispositions exceed the findings posted");
  }
  return {
    posted,
    bySeverityAndOutcome,
    chainInducedRegressions: requireCount(
      source["chainInducedRegressions"],
      "findings.chainInducedRegressions"
    )
  };
}
function telemetryIdempotencyKey(fields) {
  return [
    fields.repo,
    String(fields.pr),
    fields.engine,
    fields.passType,
    String(fields.round),
    fields.headSha
  ].join(":");
}
function validateTelemetryRecord(value) {
  const source = requireObject(value, "record");
  if (source["version"] !== TELEMETRY_VERSION) {
    fail(`telemetry record version must be ${TELEMETRY_VERSION}`);
  }
  const emittedAt = source["emittedAt"];
  if (typeof emittedAt !== "string" || !UTC_TIMESTAMP_RE.test(emittedAt) || Number.isNaN(Date.parse(emittedAt)) || new Date(emittedAt).toISOString().replace(".000Z", "Z") !== emittedAt) {
    fail("telemetry emittedAt must be an RFC 3339 UTC timestamp");
  }
  const repo = source["repo"];
  if (typeof repo !== "string" || !REPO_RE.test(repo)) {
    fail("telemetry repo must be owner/name");
  }
  const pr = source["pr"];
  if (typeof pr !== "number" || !Number.isSafeInteger(pr) || pr < 1) {
    fail("telemetry pr must be a positive integer");
  }
  const engine = source["engine"];
  if (typeof engine !== "string" || !OPEN_TOKEN_RE.test(engine)) {
    fail(`telemetry engine must match ${OPEN_TOKEN_RE}`);
  }
  const round = source["round"];
  if (typeof round !== "number" || !Number.isSafeInteger(round) || round < 1) {
    fail("telemetry round must be a positive integer");
  }
  const baseSha = source["baseSha"];
  const headSha = source["headSha"];
  if (typeof baseSha !== "string" || !SHA_RE.test(baseSha) || typeof headSha !== "string" || !SHA_RE.test(headSha)) {
    fail("telemetry baseSha and headSha must be full commit SHAs");
  }
  const truncated = source["truncated"];
  if (typeof truncated !== "boolean") {
    fail("telemetry truncated must be a boolean");
  }
  const durationSeconds = source["durationSeconds"];
  if (durationSeconds !== null && (typeof durationSeconds !== "number" || !Number.isFinite(durationSeconds) || durationSeconds < 0)) {
    fail("telemetry durationSeconds must be a non-negative number or null");
  }
  const passType = requireEnum(
    source["passType"],
    TELEMETRY_PASS_TYPES,
    "passType"
  );
  const status = requireEnum(source["status"], TELEMETRY_STATUSES, "status");
  const tokenSource = requireEnum(
    source["tokenSource"],
    TELEMETRY_TOKEN_SOURCES,
    "tokenSource"
  );
  const rawTokens = source["tokens"];
  if (!Array.isArray(rawTokens)) {
    fail("telemetry tokens must be an array");
  }
  const tokens = rawTokens.map(validateTokenBucket);
  const models = tokens.map(
    (bucket) => JSON.stringify([bucket.model, bucket.effort])
  );
  if (new Set(models).size !== models.length) {
    fail("telemetry tokens must carry one bucket per model and effort");
  }
  if (tokenSource === "unavailable" && tokens.length > 0) {
    fail("telemetry tokenSource unavailable cannot carry token buckets");
  }
  if (tokenSource !== "unavailable" && tokens.length === 0) {
    fail("telemetry with no token buckets must record tokenSource unavailable");
  }
  const rawLanes = source["lanes"];
  if (rawLanes !== void 0 && !Array.isArray(rawLanes)) {
    fail("telemetry lanes must be an array when present");
  }
  if (Array.isArray(rawLanes) && rawLanes.length === 0) {
    fail("telemetry lanes must be absent rather than empty");
  }
  const lanes = Array.isArray(rawLanes) ? rawLanes.map(validateLane) : void 0;
  const changeset = validateChangeset(source["changeset"]);
  if (status === "skipped" && changeset.reviewSignificantFiles > 0) {
    fail("a skipped pass cannot carry review-significant files");
  }
  const idempotencyKey = source["idempotencyKey"];
  if (typeof idempotencyKey !== "string" || !TOKEN_RE.test(idempotencyKey)) {
    fail("telemetry idempotencyKey must be a protocol token");
  }
  const validated = {
    ...source,
    version: TELEMETRY_VERSION,
    emittedAt,
    repo,
    pr,
    idempotencyKey,
    engine,
    engineVersion: requireNullableToken(
      source["engineVersion"],
      "engineVersion"
    ),
    passType,
    reviewTier: source["reviewTier"] === null ? null : requireEnum(
      source["reviewTier"],
      TELEMETRY_REVIEW_TIERS,
      "reviewTier"
    ),
    trigger: requireEnum(source["trigger"], TELEMETRY_TRIGGERS, "trigger"),
    round,
    stance: requireEnum(source["stance"], TELEMETRY_STANCES, "stance"),
    status,
    baseSha,
    headSha,
    promptStackSha256: requireNullableSha256(
      source["promptStackSha256"],
      "promptStackSha256"
    ),
    promptStackVersion: requireNullableToken(
      source["promptStackVersion"],
      "promptStackVersion"
    ),
    repoInstructionsSha256: requireNullableSha256(
      source["repoInstructionsSha256"],
      "repoInstructionsSha256"
    ),
    tokenSource,
    tokens,
    ...lanes === void 0 ? {} : { lanes },
    truncated,
    durationSeconds,
    changeset,
    findings: validateFindings(source["findings"])
  };
  return validated;
}
function tokenBucketFrom(bucket) {
  return validateTokenBucket({
    model: bucket.model,
    effort: bucket.effort ?? null,
    input: bucket.input ?? null,
    output: bucket.output ?? null,
    cacheRead: bucket.cacheRead ?? null,
    cacheWrite: bucket.cacheWrite ?? null,
    reasoning: bucket.reasoning ?? null,
    providerBuckets: bucket.providerBuckets ?? {}
  });
}
function laneFrom(lane) {
  return validateLane({
    lens: lane.lens,
    model: lane.model ?? null,
    input: lane.input ?? null,
    output: lane.output ?? null,
    cacheRead: lane.cacheRead ?? null,
    cacheWrite: lane.cacheWrite ?? null,
    reasoning: lane.reasoning ?? null
  });
}
function findingsFrom(findings) {
  const ladder = {};
  for (const severity of SUPPORTED_SEVERITIES) {
    const row = findings?.bySeverityAndOutcome?.[severity];
    ladder[severity] = {
      validFixed: row?.validFixed ?? 0,
      validDeferred: row?.validDeferred ?? 0,
      invalidDismissed: row?.invalidDismissed ?? 0
    };
  }
  return validateFindings({
    posted: findings?.posted ?? 0,
    bySeverityAndOutcome: ladder,
    chainInducedRegressions: findings?.chainInducedRegressions ?? 0
  });
}
function buildTelemetryRecord(params) {
  const tokens = (params.tokens ?? []).map(tokenBucketFrom);
  const lanes = params.lanes?.map(laneFrom);
  const idempotencyKey = params.idempotencyKey ?? telemetryIdempotencyKey({
    repo: params.repo,
    pr: params.pr,
    engine: params.engine,
    passType: params.passType,
    round: params.round,
    headSha: params.headSha
  });
  return validateTelemetryRecord({
    version: TELEMETRY_VERSION,
    emittedAt: params.emittedAt,
    repo: params.repo,
    pr: params.pr,
    idempotencyKey,
    engine: params.engine,
    engineVersion: params.engineVersion ?? null,
    passType: params.passType,
    reviewTier: params.reviewTier ?? null,
    trigger: params.trigger,
    round: params.round,
    stance: params.stance,
    status: params.status,
    baseSha: params.baseSha,
    headSha: params.headSha,
    promptStackSha256: params.promptStackSha256 ?? null,
    promptStackVersion: params.promptStackVersion ?? null,
    repoInstructionsSha256: params.repoInstructionsSha256 ?? null,
    tokenSource: params.tokenSource,
    tokens,
    ...lanes === void 0 ? {} : { lanes },
    truncated: params.truncated,
    durationSeconds: params.durationSeconds ?? null,
    changeset: params.changeset,
    findings: findingsFrom(params.findings)
  });
}
function knownTelemetryRecord(value) {
  const record = validateTelemetryRecord(value);
  return {
    version: record.version,
    emittedAt: record.emittedAt,
    repo: record.repo,
    pr: record.pr,
    idempotencyKey: record.idempotencyKey,
    engine: record.engine,
    engineVersion: record.engineVersion,
    passType: record.passType,
    reviewTier: record.reviewTier,
    trigger: record.trigger,
    round: record.round,
    stance: record.stance,
    status: record.status,
    baseSha: record.baseSha,
    headSha: record.headSha,
    promptStackSha256: record.promptStackSha256,
    promptStackVersion: record.promptStackVersion,
    repoInstructionsSha256: record.repoInstructionsSha256,
    tokenSource: record.tokenSource,
    tokens: record.tokens,
    ...!record.lanes ? {} : { lanes: record.lanes },
    truncated: record.truncated,
    durationSeconds: record.durationSeconds,
    changeset: record.changeset,
    findings: record.findings
  };
}
function buildTelemetryBody(record) {
  const safeRecord = knownTelemetryRecord(record);
  return [
    TELEMETRY_V1_MARKER,
    "",
    "```json",
    JSON.stringify(safeRecord, null, 2),
    "```"
  ].join("\n");
}
function matchTelemetry(body) {
  if (!isTelemetryComment(body)) {
    return null;
  }
  const prefixIndex = body.indexOf(TELEMETRY_MARKER_PREFIX);
  if (prefixIndex !== body.lastIndexOf(TELEMETRY_MARKER_PREFIX)) {
    fail("a comment carries more than one local-review telemetry marker");
  }
  const markerIndex = body.indexOf(TELEMETRY_V1_MARKER);
  if (markerIndex === -1) {
    fail("local-review telemetry record is of an unsupported version");
  }
  const payload = body.slice(markerIndex + TELEMETRY_V1_MARKER.length).replace(/^\s*```(?:json)?\s*\n/, "").replace(/\n```\s*$/, "").trim();
  if (payload === "") {
    fail("local-review telemetry record carries no payload");
  }
  return validateTelemetryRecord(
    parseJsonOrFail(
      payload,
      "local-review telemetry payload is not valid JSON"
    )
  );
}
function canonicalJson(value) {
  if (value === void 0) {
    return "undefined";
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (typeof value === "object" && value !== null) {
    const source = value;
    return `{${Object.keys(source).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(source[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
function replayFingerprint(record) {
  const known = knownTelemetryRecord(record);
  return canonicalJson({
    ...known,
    emittedAt: null,
    tokens: [...known.tokens].sort(
      (left, right) => canonicalJson(left).localeCompare(canonicalJson(right))
    ),
    lanes: known.lanes ? [...known.lanes].sort(
      (left, right) => canonicalJson(left).localeCompare(canonicalJson(right))
    ) : void 0
  });
}
function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
function prCommentSink(target) {
  return {
    name: "pr-comment",
    emit({ record, body }) {
      const actor = assertActor(target.actor);
      const rows = getIssueComments(target.repo, target.pr, actor);
      for (const row of rows) {
        const existing = String(row["body"] ?? "");
        if (!existing.includes(TELEMETRY_V1_MARKER)) {
          continue;
        }
        let parsed;
        try {
          parsed = matchTelemetry(existing);
        } catch {
          continue;
        }
        if (parsed?.idempotencyKey === record.idempotencyKey) {
          if (replayFingerprint(parsed) !== replayFingerprint(record)) {
            fail("telemetry idempotency key conflicts with an existing record");
          }
          return { sink: "pr-comment", reference: String(row["id"] ?? "") };
        }
      }
      assertLiveActor(actor);
      const response = jsonOutput(
        [
          "api",
          "-X",
          "POST",
          `repos/${target.repo}/issues/${target.pr}/comments`
        ],
        { body }
      );
      const commentId = getPostedCommentId(response);
      try {
        verifyIssueComment(target.repo, commentId, body, actor);
      } catch (error) {
        try {
          deleteIssueComment(target.repo, target.pr, commentId);
        } catch (rollbackError) {
          throw new LedgerError(
            `telemetry verification failed and rollback could not be verified: ${errorMessage(rollbackError)}; comment ${commentId} on ${target.repo}#${target.pr} may remain on the pull request; verification failed with: ${errorMessage(error)}`,
            { cause: error }
          );
        }
        throw error;
      }
      return { sink: "pr-comment", reference: String(commentId) };
    }
  };
}
function emitTelemetry(params) {
  const { record, sink } = params;
  try {
    const body = buildTelemetryBody(record);
    const result = sink.emit({ record, body });
    return {
      emitted: true,
      sink: result.sink,
      reference: result.reference,
      idempotencyKey: record.idempotencyKey,
      error: null
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return {
      emitted: false,
      sink: sink.name,
      reference: null,
      idempotencyKey: record.idempotencyKey,
      error: message
    };
  }
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
function readJsonFile(pathValue, label) {
  assertRegularFile(pathValue, `${label} must be a regular non-symlink file`);
  return parseJsonOrFail(
    readFileSync4(pathValue, "utf8"),
    `${label} must contain valid UTF-8 JSON`
  );
}
function readJsonArray(pathValue, label) {
  const parsed = readJsonFile(pathValue, label);
  if (!Array.isArray(parsed)) {
    fail(`${label} must contain a JSON array`);
  }
  return parsed;
}
function resolveChangesetReport(args) {
  const options = args.promptSurfaces === void 0 ? void 0 : { promptSurfaces: args.promptSurfaces };
  if (args.diffFile) {
    assertRegularFile(
      args.diffFile,
      "diff file must be a regular non-symlink file"
    );
    return classifyFiles(
      parseDiffPatch(readFileSync4(args.diffFile, "utf8")),
      options
    );
  }
  if (!args.base || !args.head) {
    fail("changeset classification requires --diff-file or --base and --head");
  }
  return classifyRange({ base: args.base, head: args.head, options });
}
function parseDurationSeconds(raw) {
  if (raw === void 0) {
    return null;
  }
  if (!/^\d+(\.\d+)?$/.test(raw)) {
    fail("--duration-seconds must be a non-negative number");
  }
  return Number(raw);
}
function normalizeChangesetInput(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail("changeset file must contain a JSON object");
  }
  const source = value;
  const nested = source["changeset"];
  if (typeof nested === "object" && nested !== null && !Array.isArray(nested)) {
    return nested;
  }
  return source;
}
function nowUtcSecond() {
  return (/* @__PURE__ */ new Date()).toISOString().replace(/\.\d{3}Z$/, "Z");
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
    if (arg === "--truncated") {
      args.truncated = true;
      i++;
      continue;
    }
    if (arg === "--dry-run") {
      args.dryRun = true;
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
        args.engineRaw = parseVal(arg);
        break;
      case "--engine-version":
        args.engineVersion = parseVal(arg);
        break;
      case "--pass-type":
        args.passType = parseEnum(arg, parseVal(arg), TELEMETRY_PASS_TYPES);
        break;
      case "--review-tier":
        args.reviewTier = parseEnum(arg, parseVal(arg), TELEMETRY_REVIEW_TIERS);
        break;
      case "--trigger":
        args.trigger = parseEnum(arg, parseVal(arg), TELEMETRY_TRIGGERS);
        break;
      case "--stance":
        args.stance = parseEnum(arg, parseVal(arg), TELEMETRY_STANCES);
        break;
      case "--status":
        args.status = parseEnum(arg, parseVal(arg), TELEMETRY_STATUSES);
        break;
      case "--token-source":
        args.tokenSource = parseEnum(
          arg,
          parseVal(arg),
          TELEMETRY_TOKEN_SOURCES
        );
        break;
      case "--tokens-file":
        args.tokensFile = parseVal(arg);
        break;
      case "--lanes-file":
        args.lanesFile = parseVal(arg);
        break;
      case "--findings-file":
        args.findingsFile = parseVal(arg);
        break;
      case "--changeset-file":
        args.changesetFile = parseVal(arg);
        break;
      case "--diff-file":
        args.diffFile = parseVal(arg);
        break;
      case "--prompt-stack-sha256":
        args.promptStackSha256 = parseVal(arg);
        break;
      case "--prompt-stack-version":
        args.promptStackVersion = parseVal(arg);
        break;
      case "--repo-instructions-sha256":
        args.repoInstructionsSha256 = parseVal(arg);
        break;
      case "--duration-seconds":
        args.durationSeconds = parseVal(arg);
        break;
      case "--emitted-at":
        args.emittedAt = parseVal(arg);
        break;
      case "--idempotency-key":
        args.idempotencyKey = parseVal(arg);
        break;
      case "--prompt-surface":
        args.promptSurfaces = [...args.promptSurfaces ?? [], parseVal(arg)];
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
  if (args.engineRaw !== void 0 && args.command !== "emit-telemetry") {
    args.engine = parseEnum("--engine", args.engineRaw, SUPPORTED_ENGINES);
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
function telemetryFailure(error) {
  return {
    emitted: false,
    sink: null,
    reference: null,
    idempotencyKey: null,
    error: error instanceof Error ? error.message : String(error)
  };
}
function runCliCommand(argv) {
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
    case "classify-changeset": {
      const report = resolveChangesetReport(args);
      writeSortedJson({
        ...report.changeset,
        skip: report.skip,
        reviewSignificantFiles: report.reviewSignificantFiles,
        classifications: report.classifications
      });
      break;
    }
    case "emit-telemetry": {
      let outcome;
      try {
        if (!args.repo || args.pr === void 0 || !args.engineRaw) {
          fail("emit-telemetry requires --repo, --pr, and --engine");
        }
        if (!args.passType || !args.trigger || !args.stance || !args.status || !args.tokenSource || args.round === void 0 || !args.base || !args.head) {
          fail(
            "emit-telemetry requires --pass-type, --trigger, --stance, --status, --token-source, --round, --base, and --head"
          );
        }
        const changeset = args.changesetFile ? readJsonFile(args.changesetFile, "changeset file") : resolveChangesetReport(args).changeset;
        const record = buildTelemetryRecord({
          emittedAt: args.emittedAt ?? nowUtcSecond(),
          repo: args.repo,
          pr: args.pr,
          idempotencyKey: args.idempotencyKey,
          engine: args.engineRaw,
          engineVersion: args.engineVersion ?? null,
          passType: args.passType,
          reviewTier: args.reviewTier ?? null,
          trigger: args.trigger,
          round: args.round,
          stance: args.stance,
          status: args.status,
          baseSha: args.base,
          headSha: args.head,
          promptStackSha256: args.promptStackSha256 ?? null,
          promptStackVersion: args.promptStackVersion ?? null,
          repoInstructionsSha256: args.repoInstructionsSha256 ?? null,
          tokenSource: args.tokenSource,
          tokens: args.tokensFile ? readJsonArray(
            args.tokensFile,
            "tokens file"
          ) : [],
          lanes: args.lanesFile ? readJsonArray(
            args.lanesFile,
            "lanes file"
          ) : void 0,
          truncated: args.truncated === true,
          durationSeconds: parseDurationSeconds(args.durationSeconds),
          changeset: normalizeChangesetInput(changeset),
          findings: args.findingsFile ? readJsonFile(
            args.findingsFile,
            "findings file"
          ) : void 0
        });
        if (args.dryRun) {
          process.stdout.write(buildTelemetryBody(record) + "\n");
          return 0;
        }
        outcome = emitTelemetry({
          record,
          sink: prCommentSink({
            repo: args.repo,
            pr: args.pr,
            actor: args.actor
          })
        });
      } catch (error) {
        outcome = telemetryFailure(error);
      }
      writeSortedJson(outcome);
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
function commandFromArgv(argv) {
  const booleanFlags = /* @__PURE__ */ new Set([
    "--protocol-version",
    "--version",
    "--file-level",
    "--truncated",
    "--dry-run"
  ]);
  for (let i = 0; i < argv.length; ) {
    const arg = argv[i];
    if (!arg.startsWith("-")) {
      return arg;
    }
    i += booleanFlags.has(arg) ? 1 : 2;
  }
  return void 0;
}
function runCli(argv = process.argv.slice(2)) {
  resetGitHubRunner();
  if (commandFromArgv(argv) !== "emit-telemetry") {
    return runCliCommand(argv);
  }
  try {
    return runCliCommand(argv);
  } catch (error) {
    writeSortedJson(telemetryFailure(error));
    return 0;
  }
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
