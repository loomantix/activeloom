/** A review engine that may own ledger records. */
export type SupportedEngine = 'codex' | 'claude' | 'gemini' | 'antigravity';
/** Severity level of a finding. */
export type SupportedSeverity = 'blocking' | 'major' | 'minor' | 'nit';
/** How a finding was closed. */
export type SupportedOutcome = 'fixed' | 'dismissed' | 'deferred';
/** The overall outcome of a review round. */
export type SupportedStatus = 'clean' | 'changed' | 'blocked';
/** Whether a changed round counts as a material change. */
export type SupportedClassification = 'minor' | 'material';
/** Which side of the diff an anchor refers to. */
export type SupportedSide = 'RIGHT' | 'LEFT';

/** A finding before it becomes a ledger record. */
export interface ReviewFinding {
  path: string;
  line?: number | undefined;
  fileLevel?: boolean | undefined;
  side?: SupportedSide | undefined;
  engine?: SupportedEngine | undefined;
  round?: number | undefined;
  fingerprint?: string | undefined;
  occurrence?: number | undefined;
  severity?: SupportedSeverity | undefined;
  lens?: string | undefined;
  rootCause?: string | undefined;
  message?: string | undefined;
  rule?: string | undefined;
  content?: string | undefined;
  contentFile?: string | undefined;
  bodyFile?: string | undefined;
}

/** Fields parsed from a v3 finding marker. */
export interface FindingV3Match {
  engine: SupportedEngine;
  round: number;
  head: string;
  fingerprint: string;
  occurrence: number;
  severity: SupportedSeverity;
  lens: string;
  contentSha: string;
}

/** Fields parsed from a v3 disposition marker. */
export interface DispositionV3Match {
  engine: SupportedEngine;
  round: number;
  head: string;
  fingerprint: string;
  occurrence: number;
  outcome: SupportedOutcome;
  contentSha: string;
}

/** Fields parsed from a legacy v1 finding marker. */
export interface FindingV1Match {
  engine: SupportedEngine;
  round: number;
  head: string;
  fingerprint: string;
}

/** Fields parsed from a legacy v1 disposition marker. */
export interface DispositionV1Match {
  engine: SupportedEngine;
  round: number;
  head: string;
  fingerprint: string;
  outcome: SupportedOutcome;
}

/** Fields parsed from a historical pseudo-v3 marker. */
export interface PseudoV3Match {
  fingerprint: string;
  outcome?: 'deferred' | undefined;
}

/** Serialized outcome of one review round. */
export interface LedgerResult {
  version: number;
  status: SupportedStatus;
  engine: SupportedEngine;
  round: number;
  baseSha: string;
  beforeSha: string;
  afterSha: string;
  classification: SupportedClassification | null;
  findingFingerprints: string[];
  finalLaneComplete: boolean;
  blocker?: string | undefined;
  resultSha256?: string | undefined;
  verified?: boolean | undefined;
}

/** Common identity shared by result-producing commands. */
export interface BaseResultParams {
  head: string;
  engine: SupportedEngine;
  round: number;
  base: string;
  before: string;
  resultFile: string;
}

/** Parameters for `writeResult`. */
export interface WriteResultParams extends BaseResultParams {
  repo: string;
  pr: number;
  allowedHeadsFile?: string | undefined;
  actor?: string | undefined;
  historicalCommentIdsFile?: string | undefined;
  classification?: SupportedClassification | undefined;
}

/** Parameters for recovering a completed, digest-pinned result candidate. */
export interface RecoverResultParams extends Omit<
  WriteResultParams,
  'classification'
> {
  expectedRecoverySha256: string;
}

/** Parameters for `writeBlockedResult`. */
export interface WriteBlockedParams extends BaseResultParams {
  blockerFile?: string | undefined;
  blocker?: string | undefined;
}

/** Parameters for `validateResult`. */
export interface ValidateResultParams extends BaseResultParams {
  resultHead?: string | undefined;
}

/** Parameters for `preflightAnchor`. */
export interface PreflightAnchorParams {
  repo: string;
  pr: number;
  head: string;
  path: string;
  line?: number | undefined;
  fileLevel?: boolean | undefined;
  side?: SupportedSide | undefined;
}

/** Result of `preflightAnchor`. */
export interface PreflightAnchorResult {
  anchor: string;
  path: string;
  verified: true;
}

/** Parameters for `postFinding`. */
export interface PostFindingParams {
  repo: string;
  pr: number;
  head: string;
  path: string;
  line?: number | undefined;
  fileLevel?: boolean | undefined;
  side?: SupportedSide | undefined;
  contentFile?: string | undefined;
  content?: string | undefined;
  bodyFile?: string | undefined;
  engine?: SupportedEngine | undefined;
  round?: number | undefined;
  fingerprint?: string | undefined;
  occurrence?: number | undefined;
  severity?: SupportedSeverity | undefined;
  lens?: string | undefined;
}

/** Result of `postFinding`. */
export interface PostFindingResult {
  comment_id: number;
  verified: true;
  replayed?: boolean | undefined;
}

/** Parameters for `reopenOccurrence`. */
export interface ReopenOccurrenceParams {
  repo: string;
  pr: number;
  head: string;
  engine: SupportedEngine;
  round: number;
  fingerprint: string;
  occurrence: number;
  severity: SupportedSeverity;
  lens: string;
  commentId: number;
  threadId: string;
  contentFile?: string | undefined;
  content?: string | undefined;
}

/** Result of `reopenOccurrence`. */
export interface ReopenOccurrenceResult {
  comment_id: number;
  replayed: boolean;
  thread_replayed: boolean;
  resolved: false;
  verified: true;
}

/** Parameters for `dispose`. */
export interface DisposeParams {
  repo: string;
  pr: number;
  head: string;
  engine: SupportedEngine;
  round: number;
  fingerprint: string;
  occurrence?: number | undefined;
  outcome: SupportedOutcome;
  commentId: number;
  threadId: string;
  contentFile?: string | undefined;
  content?: string | undefined;
}

/** Result of `dispose`. */
export interface DisposeResult {
  comment_id: number;
  replayed: boolean;
  thread_replayed: boolean;
  resolved: true;
  verified: true;
}

/** Parameters for `reply`. */
export interface ReplyParams {
  repo: string;
  pr: number;
  head: string;
  commentId: number;
  bodyFile?: string | undefined;
  body?: string | undefined;
  contentFile?: string | undefined;
  engine?: SupportedEngine | undefined;
  round?: number | undefined;
  fingerprint?: string | undefined;
  outcome?: SupportedOutcome | undefined;
}

/** Parameters for `postPrComment`. */
export interface PostPrCommentParams {
  repo: string;
  pr: number;
  head: string;
  bodyFile?: string | undefined;
  body?: string | undefined;
}

/** Parameters for `attest`. */
export interface AttestParams extends BaseResultParams {
  repo: string;
  pr: number;
  threadsFile?: string | undefined;
  allowedHeadsFile?: string | undefined;
  actor?: string | undefined;
  historicalCommentIdsFile?: string | undefined;
  expectedResultSha256: string;
  expectedThreadsSha256?: string | undefined;
  contentFile?: string | undefined;
  content?: string | undefined;
}

/** Parameters for `finalize`: `attest` with fields derived from saved result. */
export type FinalizeParams = Omit<
  AttestParams,
  'head' | 'engine' | 'round' | 'base' | 'before' | 'expectedResultSha256'
>;

/** Result of `attest`. */
export interface AttestResult {
  comment_id: number;
  replayed: boolean;
  result_sha256: string;
  verified: true;
}

/** Parameters for `resolve`. */
export interface ResolveParams {
  repo: string;
  pr: number;
  head: string;
  threadId: string;
}

/** Result of `resolve`. */
export interface ResolveResult {
  thread_id: string;
  resolved: true;
}

/** Parameters for `reconcile`. */
export interface ReconcileParams {
  repo: string;
  pr: number;
  head: string;
  fingerprint: string;
}

/** Result of `reconcile`. */
export interface ReconcileResult {
  findings: Array<Record<string, unknown>>;
  dispositions: Array<Record<string, unknown>>;
  sequenceValid: boolean;
  ledgerValid: boolean;
  nextOccurrence: number | null;
  undisposedOccurrences: number[];
  nextAction:
    | 'repair-sequence'
    | 'dispose'
    | 'reopen-occurrence'
    | 'post-finding';
  /** Review thread for occurrence 1, or null if unposted or ambiguous. */
  threadId: string | null;
  threadResolved: boolean | null;
  verified: true;
}

/** Parameters for `verifyLedger`. */
export interface VerifyLedgerParams {
  repo: string;
  pr: number;
  head: string;
  threadsFile?: string | undefined;
  actor?: string | undefined;
  historicalCommentIdsFile?: string | undefined;
  engine?: SupportedEngine | undefined;
  round?: number | undefined;
  base?: string | undefined;
  before?: string | undefined;
  resultHead?: string | undefined;
  resultFile?: string | undefined;
  allowedHeadsFile?: string | undefined;
  expectedThreadsSha256?: string | undefined;
}

/** Result of `verifyLedger`. */
export interface VerifyLedgerResult {
  actor: string;
  dispositions: number;
  verified: true;
}

/** Report returned by `threadResolution`. */
export interface ThreadResolutionReport {
  verified: boolean;
  threadsVerified: number;
  resultStatus: SupportedStatus | null;
}

/** Author identity returned by GitHub on a comment. */
export interface GitHubCommentAuthor {
  login: string;
}

/** Comment inside a review thread. */
export interface GitHubReviewCommentNode extends Record<string, unknown> {
  databaseId?: number | undefined;
  id?: number | undefined;
  body?: string | undefined;
  author?: GitHubCommentAuthor | undefined;
  user?: GitHubCommentAuthor | undefined;
  path?: string | undefined;
  line?: number | undefined;
  side?: string | undefined;
  commit_id?: string | undefined;
}

/** Review thread with PR scope and full comment list. */
export interface GitHubReviewThreadNode {
  id: string;
  isResolved: boolean;
  repository?: { nameWithOwner?: string } | null | undefined;
  pullRequest?: { number?: number } | null | undefined;
  comments: {
    nodes: GitHubReviewCommentNode[];
    pageInfo: { hasNextPage: boolean; endCursor?: string | null | undefined };
  };
}

/** Seam through which GitHub and git access flows. */
export interface GitHubRunner {
  runGh(args: string[], payload?: unknown): string;
  currentActor?(): string;
  liveActor?(): string;
  gitCompare?(repo: string, before: string, after: string): unknown;
  gitRevList?(before: string, head: string): string[];
  runGit?(args: string[]): string;
  isAncestor?(ancestor: string, descendant: string): boolean;
}

/** Distinct non-author engines reviewing head: solo, cross (recommended), or full. */
export type CoverageTier = 'solo' | 'cross' | 'full';

/** Which roster grammar a parsed marker was written in. */
export type RosterVersion = 1 | 2;

/** A parsed roster marker across protocol versions. */
export interface RosterMatch {
  version: RosterVersion;
  author: SupportedEngine;
  reviewers: SupportedEngine[];
  head: string | null;
  supersedes: number | null;
  /** `declaration-sha256` for v2, `content-sha256` for v1. */
  digest: string;
}

/** @deprecated Use {@link RosterMatch}. */
export type RosterV1Match = RosterMatch;

/** Effective roster declared on a pull request, or its declared absence. */
export interface RosterReport {
  present: boolean;
  version: RosterVersion | null;
  author: SupportedEngine | null;
  reviewers: SupportedEngine[];
  head: string | null;
  commentId: number | null;
  supersedes: number | null;
  chain: number[];
}

/** Parameters for `postRoster`. */
export interface PostRosterParams {
  repo: string;
  pr: number;
  head: string;
  /** Asserted against the live authenticated actor; never used to set it. */
  actor?: string | undefined;
  author: SupportedEngine;
  reviewers: readonly SupportedEngine[];
  content: string;
}

/** Result of `postRoster`. */
export interface PostRosterResult {
  comment_id: number;
  author: SupportedEngine;
  reviewers: SupportedEngine[];
  head: string;
  /** The comment id this declaration replaces, or `null` for the first one. */
  supersedes: number | null;
  /** True when this post replaced an earlier roster rather than opening one. */
  superseded: boolean;
  chain: number[];
  replayed: boolean;
  verified: true;
}

/** Actor-owned attestation naming the exact head under examination. */
export interface AttestationAtHead {
  engine: SupportedEngine;
  round: number;
  status: 'clean' | 'changed';
}

/** Parameters for `coverage` and `verifyCoverage`. */
export interface CoverageParams {
  repo: string;
  pr: number;
  head: string;
  /** Asserted against the live authenticated actor; never used to set it. */
  actor?: string | undefined;
}

/** Result of `coverage`. */
export interface CoverageResult {
  head: string;
  rosterPresent: boolean;
  rosterVersion: RosterVersion | null;
  /** The commit the effective roster was declared at; `null` for v1. */
  rosterHead: string | null;
  /** True when the roster names a commit other than the one being reported on. */
  rosterStale: boolean;
  rosterChain: number[];
  author: SupportedEngine | null;
  reviewers: SupportedEngine[];
  attestedAtHead: SupportedEngine[];
  nonAuthorAttested: SupportedEngine[];
  missingReviewers: SupportedEngine[];
  authorAttested: boolean;
  tier: CoverageTier;
  /** A roster declaring no reviewers is present, whatever its version or head. */
  soloDeclared: boolean;
  /** Whether the solo declaration is verified under v2 grammar at this head. */
  soloAcknowledged: boolean;
  roundComplete: boolean;
  verified: true;
}

/** The value a changed line represents, for the telemetry denominator. */
export type ChangesetClass = 'app' | 'test' | 'docsConfig' | 'generated';

/** Options that tune path classification for a repository. */
export interface ClassifyOptions {
  /** Path prefixes and exact paths treated as prompt surfaces. */
  promptSurfaces?: readonly string[] | undefined;
}

/** One changed file and its churn over the pinned review range. */
export interface ChangedFile {
  path: string;
  added: number;
  deleted: number;
  /** Blank-line churn; absent or null is conservatively counted as zero. */
  blank?: number | null | undefined;
}

/** How one path classified, on both the value axis and the review axis. */
export interface FileClassification {
  path: string;
  class: ChangesetClass;
  /** True when this file alone obliges a lane to run. */
  reviewSignificant: boolean;
  /** Key used in `linesByLanguage`, or null for an unmapped extension. */
  language: string | null;
}

/** Churn split by class. `comment` is null until a lexer ships. */
export interface ChangesetLines {
  app: number;
  test: number;
  comment: number | null;
  docsConfig: number;
  generated: number;
  blank: number;
}

/** The changeset a telemetry record carries. */
export interface Changeset {
  classifierVersion: number;
  /** Files whose classification requires a review lane to run. */
  reviewSignificantFiles: number;
  files: Record<ChangesetClass, number>;
  linesChanged: ChangesetLines;
  linesByLanguage: Record<string, number>;
}

/** The classifier's full answer: the record, the gate, and the per-file detail. */
export interface ChangesetReport {
  changeset: Changeset;
  classifications: FileClassification[];
  reviewSignificantFiles: number;
  /** True when nothing in the range obliges a lane to run. */
  skip: boolean;
}

/** What kind of pass spent the tokens. */
export type TelemetryPassType = 'review' | 'refactor' | 'hosted';

/** Which lens depth the pass ran at, or null when the pass has no tier. */
export type TelemetryReviewTier = 'lean' | 'deep';

/** Whether a human drove the pass or a loop did. */
export type TelemetryTrigger = 'autonomous' | 'interactive';

/** Adversarial rounds versus land-only convergence rounds. */
export type TelemetryStance = 'adversarial' | 'convergence';

/** How the pass ended, including the two outcomes that spend without finding. */
export type TelemetryStatus = 'clean' | 'changed' | 'blocked' | 'skipped';

/** Source and confidence of token measurements. */
export type TelemetryTokenSource =
  | 'session-log-delta'
  | 'stream-json'
  | 'unscoped-session'
  | 'unavailable';

/** Token counts for one model id; null indicates unmeasured buckets. */
export interface TelemetryTokenBucket {
  model: string;
  effort: string | null;
  input: number | null;
  output: number | null;
  cacheRead: number | null;
  cacheWrite: number | null;
  reasoning: number | null;
  /** Provider-specific integer buckets not mapped to canonical buckets. */
  providerBuckets: Record<string, number>;
}

/** Per-lens spend. Engine-specific, and never used for cross-engine rollups. */
export interface TelemetryLane {
  lens: string;
  model: string | null;
  input: number | null;
  output: number | null;
  cacheRead: number | null;
  cacheWrite: number | null;
  reasoning: number | null;
}

/** How one severity's findings were dispositioned. */
export interface TelemetryOutcomeCounts {
  validFixed: number;
  validDeferred: number;
  invalidDismissed: number;
}

/** Findings posted by the pass and what became of them. */
export interface TelemetryFindings {
  posted: number;
  bySeverityAndOutcome: Record<SupportedSeverity, TelemetryOutcomeCounts>;
  /** New defects introduced by the review chain itself. */
  chainInducedRegressions: number;
}

/** Caller input for one token bucket; only measured fields may be omitted. */
export interface TelemetryTokenBucketInput {
  model: string;
  effort?: string | null | undefined;
  input?: number | null | undefined;
  output?: number | null | undefined;
  cacheRead?: number | null | undefined;
  cacheWrite?: number | null | undefined;
  reasoning?: number | null | undefined;
  providerBuckets?: Record<string, number> | undefined;
}

/** Caller input for one lane; the lane identity is always required. */
export interface TelemetryLaneInput {
  lens: string;
  model?: string | null | undefined;
  input?: number | null | undefined;
  output?: number | null | undefined;
  cacheRead?: number | null | undefined;
  cacheWrite?: number | null | undefined;
  reasoning?: number | null | undefined;
}

/** Caller input for finding totals, whose omitted counts default to zero. */
export interface TelemetryFindingsInput {
  posted?: number | undefined;
  bySeverityAndOutcome?:
    | Partial<Record<SupportedSeverity, Partial<TelemetryOutcomeCounts>>>
    | undefined;
  chainInducedRegressions?: number | undefined;
}

/**
 * One review pass, measured. Structured identifiers and counts only;
 * callers must supply public-safe, non-sensitive identifiers.
 */
export interface TelemetryRecord {
  version: 1;
  emittedAt: string;
  repo: string;
  pr: number;
  idempotencyKey: string;

  engine: string;
  engineVersion: string | null;
  passType: TelemetryPassType;
  reviewTier: TelemetryReviewTier | null;
  trigger: TelemetryTrigger;
  round: number;
  stance: TelemetryStance;
  status: TelemetryStatus;

  baseSha: string;
  headSha: string;

  promptStackSha256: string | null;
  promptStackVersion: string | null;
  repoInstructionsSha256: string | null;

  tokenSource: TelemetryTokenSource;
  tokens: TelemetryTokenBucket[];
  /** Absent, never empty, when per-lane spend is unattributable. */
  lanes?: TelemetryLane[];

  truncated: boolean;
  durationSeconds: number | null;

  changeset: Changeset;
  findings: TelemetryFindings;
}

/** The fields `buildTelemetryRecord` needs to assemble a record. */
export interface BuildTelemetryParams {
  /** Controller run identity; separates restarted passes from retries. */
  runId?: string | undefined;
  emittedAt: string;
  repo: string;
  pr: number;
  idempotencyKey?: string | undefined;
  engine: string;
  engineVersion?: string | null | undefined;
  passType: TelemetryPassType;
  reviewTier?: TelemetryReviewTier | null | undefined;
  trigger: TelemetryTrigger;
  round: number;
  stance: TelemetryStance;
  status: TelemetryStatus;
  baseSha: string;
  headSha: string;
  promptStackSha256?: string | null | undefined;
  promptStackVersion?: string | null | undefined;
  repoInstructionsSha256?: string | null | undefined;
  tokenSource: TelemetryTokenSource;
  tokens?: readonly TelemetryTokenBucketInput[] | undefined;
  lanes?: readonly TelemetryLaneInput[] | undefined;
  truncated: boolean;
  durationSeconds?: number | null | undefined;
  changeset: Changeset;
  findings?: TelemetryFindingsInput | undefined;
}

/** Sink destination for emitted telemetry records. */
export interface TelemetrySink {
  /** Stable identifier for the sink, echoed in the emission result. */
  name: string;
  emit(input: { record: TelemetryRecord; body: string }): TelemetrySinkResult;
}

/** What a sink reports back about one emission. */
export interface TelemetrySinkResult {
  sink: string;
  /** Sink-specific handle for the written record, when it has one. */
  reference: string | null;
}

/** The outcome of an emission attempt. Never throws; never fails a review. */
export interface EmitTelemetryResult {
  emitted: boolean;
  sink: string | null;
  reference: string | null;
  idempotencyKey: string | null;
  /** Why emission was skipped, when it was. */
  error: string | null;
}
