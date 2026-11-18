/**
 * Per-subcommand usage for the review-ledger CLI.
 *
 * Every argument error names the subcommand's synopsis, and `--help` prints it,
 * so an engine can read the real flags instead of guessing them. Tests hold
 * this table to the parser and to the protocol's documented invocations.
 */

import {
  SUPPORTED_CLASSIFICATIONS,
  SUPPORTED_ENGINES,
  SUPPORTED_OUTCOMES,
  SUPPORTED_SEVERITIES,
  SUPPORTED_SIDES,
  TELEMETRY_PASS_TYPES,
  TELEMETRY_REVIEW_TIERS,
  TELEMETRY_STANCES,
  TELEMETRY_STATUSES,
  TELEMETRY_TOKEN_SOURCES,
  TELEMETRY_TRIGGERS,
} from './constants.js';
import { LedgerError } from './errors.js';

/** An argument error: the CLI appends the subcommand's usage to it. */
export class UsageError extends LedgerError {
  constructor(message: string) {
    super(message);
    this.name = 'LedgerError';
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Throw a UsageError, which the CLI reports with the subcommand's usage. */
export function failUsage(message: string): never {
  throw new UsageError(message);
}

interface CommandUsage {
  summary: string;
  /** Flag synopsis: `[...]` is optional, `(a | b)` is one of. */
  synopsis: string;
  notes?: readonly string[];
}

const oneOf = (values: readonly string[]): string => values.join('|');

const ENGINE = `<${oneOf(SUPPORTED_ENGINES)}>`;
const SHA = '<full-sha>';
const SCOPE = '--repo <owner/repo> --pr <number>';
const PASS = `--engine ${ENGINE} --round <n>`;
const RESULT_IDENTITY = `--head ${SHA} ${PASS} --base ${SHA} --before ${SHA}`;
const THREAD_SNAPSHOT =
  '[--threads-file <file> --expected-threads-sha256 <sha256>]';
const HISTORY = '[--historical-comment-ids-file <file>]';

const COMMANDS: Record<string, CommandUsage> = {
  'lifecycle-state': {
    summary: 'Print the PR-wide review-run lifecycle.',
    synopsis: SCOPE,
  },
  'preflight-anchor': {
    summary: 'Check that a finding can anchor on the PR diff.',
    synopsis: `${SCOPE} --head ${SHA} --path <file> (--line <n> | --file-level) [--side <${oneOf(SUPPORTED_SIDES)}>]`,
  },
  'post-finding': {
    summary: 'Post one verified finding inline, before any fix.',
    synopsis: `${SCOPE} --head ${SHA} --path <file> (--line <n> | --file-level) [--side <${oneOf(SUPPORTED_SIDES)}>] ${PASS} --fingerprint <stable-id> [--occurrence <n>] --severity <${oneOf(SUPPORTED_SEVERITIES)}> --lens <lens> --content-file <file>`,
    notes: [
      '--content-file holds the finding prose only; the helper writes the marker and its content-sha256. There is no hash flag.',
    ],
  },
  'reopen-occurrence': {
    summary: 'Reopen a disposed finding as a new occurrence on its thread.',
    synopsis: `${SCOPE} --head ${SHA} ${PASS} --fingerprint <stable-id> --occurrence <n> --severity <${oneOf(SUPPORTED_SEVERITIES)}> --lens <lens> --comment-id <root-comment-id> --thread-id <thread-node-id> --content-file <file>`,
  },
  'correct-severity': {
    summary: 'Record a corrected severity for a posted finding.',
    synopsis: `${SCOPE} --head ${SHA} ${PASS} --fingerprint <stable-id> --occurrence <n> [--from-severity <${oneOf(SUPPORTED_SEVERITIES)}>] --severity <${oneOf(SUPPORTED_SEVERITIES)}> --comment-id <root-comment-id> --thread-id <thread-node-id> --content-file <file>`,
  },
  'preflight-fix': {
    summary: 'Authorize a fix for a posted finding before editing.',
    synopsis: `${SCOPE} --head ${SHA} ${PASS} --fingerprint <stable-id> --occurrence <n>`,
  },
  'preflight-publish': {
    summary: 'Check a publish-fixes manifest before pushing.',
    synopsis: `${SCOPE} --head ${SHA} ${PASS} --findings-file <file>`,
  },
  dispose: {
    summary:
      'Reply with a finding disposition and resolve its thread; resumable, so retry the identical command.',
    synopsis: `${SCOPE} --head ${SHA} ${PASS} --fingerprint <stable-id> [--occurrence <n>] --outcome <${oneOf(SUPPORTED_OUTCOMES)}> --comment-id <root-comment-id> --thread-id <thread-node-id> --content-file <file>`,
    notes: [
      'Run it once per finding. The helper refuses a second disposition for the same occurrence.',
    ],
  },
  reply: {
    summary: 'Reply on a review thread.',
    synopsis: `${SCOPE} --head ${SHA} --comment-id <root-comment-id> (--content-file <file> | --body-file <file>) [${PASS}] [--fingerprint <stable-id>] [--outcome <${oneOf(SUPPORTED_OUTCOMES)}>]`,
  },
  resolve: {
    summary: 'Resolve a review thread.',
    synopsis: `${SCOPE} --head ${SHA} --thread-id <thread-node-id>`,
  },
  reconcile: {
    summary:
      'Show the occurrences, dispositions and next action for one fingerprint.',
    synopsis: `${SCOPE} --head ${SHA} --fingerprint <stable-id>`,
  },
  'post-pr-comment': {
    summary: 'Post a PR conversation comment.',
    synopsis: `${SCOPE} --head ${SHA} --body-file <file>`,
  },
  'classify-effect': {
    summary: 'Classify the effect of a commit range.',
    synopsis: `--base ${SHA} --head ${SHA}`,
  },
  'classify-changeset': {
    summary: 'Classify a range for the human-glance gate and tier.',
    synopsis: `(--base ${SHA} --head ${SHA} | --diff-file <file>) [--prompt-surface <glob>]... [--force-review]`,
  },
  'write-result': {
    summary:
      "Write this pass's canonical v3 result file for a clean or changed pass.",
    synopsis: `${SCOPE} ${RESULT_IDENTITY} --result-file <file> [--classification <${oneOf(SUPPORTED_CLASSIFICATIONS)}>] [--allowed-heads-file <file>] [--actor <login>] ${HISTORY}`,
    notes: [
      '--before is the PR head this pass started from; --head is the head it ends on.',
      '--classification is required only when the head moved, and only takes minor or material.',
      'Outside agent-loop, publish the written result with validate-result and then attest.',
    ],
  },
  'recover-result': {
    summary: 'Recover a saved result candidate after a finalization failure.',
    synopsis: `${SCOPE} ${RESULT_IDENTITY} --result-file <file> --expected-recovery-sha256 <sha256> [--allowed-heads-file <file>] [--actor <login>] ${HISTORY}`,
  },
  'write-blocked-result': {
    summary: 'Write a blocked v3 result naming one blocker.',
    synopsis: `${RESULT_IDENTITY} --result-file <file> --blocker-file <file>`,
  },
  'validate-result': {
    summary: 'Validate a result file and print its resultSha256.',
    synopsis: `${RESULT_IDENTITY} --result-file <file> [--result-head ${SHA}]`,
  },
  'export-threads': {
    summary:
      "Write the PR's complete review-thread snapshot for attest and print its sha256.",
    synopsis: `${SCOPE} --threads-file <new-file>`,
    notes: [
      'Pass the printed path and sha256 to attest as --threads-file and --expected-threads-sha256.',
    ],
  },
  attest: {
    summary: 'Verify the ledger and publish the pass/completion attestation.',
    synopsis: `${SCOPE} ${RESULT_IDENTITY} --result-file <file> --expected-result-sha256 <sha256> ${THREAD_SNAPSHOT} [--allowed-heads-file <file>] [--actor <login>] ${HISTORY} [--content-file <file>]`,
    notes: [
      '--expected-result-sha256 is the resultSha256 validate-result printed.',
    ],
  },
  finalize: {
    summary: 'Finish an interrupted pass from its saved result file.',
    synopsis: `${SCOPE} --result-file <file> ${THREAD_SNAPSHOT} [--allowed-heads-file <file>] [--actor <login>] ${HISTORY} [--content-file <file>]`,
  },
  'check-blocked-round': {
    summary: 'Check a blocked round for the controller recovery path.',
    synopsis: `${SCOPE} ${RESULT_IDENTITY} [--allowed-heads-file <file>] ${HISTORY}`,
  },
  'verify-ledger': {
    summary: "Verify every finding's disposition, optionally against a result.",
    synopsis: `${SCOPE} --head ${SHA} ${THREAD_SNAPSHOT} [--actor <login>] ${HISTORY} [${PASS} --base ${SHA} --before ${SHA} --result-file <file> [--result-head ${SHA}] [--allowed-heads-file <file>]]`,
  },
  'post-roster': {
    summary: 'Declare the review roster.',
    synopsis: `${SCOPE} --head ${SHA} --author ${ENGINE} --reviewers <engine[,engine]|none> --content-file <file> [--actor <login>]`,
  },
  'read-roster': {
    summary: 'Print the effective review roster.',
    synopsis: `${SCOPE} [--actor <login>]`,
  },
  coverage: {
    summary: 'Report attestation coverage at a head.',
    synopsis: `${SCOPE} --head ${SHA} [--actor <login>]`,
  },
  'verify-coverage': {
    summary: 'Require every declared reviewer to have attested a head.',
    synopsis: `${SCOPE} --head ${SHA} [--actor <login>]`,
  },
  'emit-telemetry': {
    summary: "Record this pass's telemetry; always exits 0.",
    synopsis: `${SCOPE} --engine <engine> --pass-type <${oneOf(TELEMETRY_PASS_TYPES)}> --trigger <${oneOf(TELEMETRY_TRIGGERS)}> --stance <${oneOf(TELEMETRY_STANCES)}> --status <${oneOf(TELEMETRY_STATUSES)}> --token-source <${oneOf(TELEMETRY_TOKEN_SOURCES)}> --round <n> --base ${SHA} --head ${SHA} --findings-file <file> [--review-tier <${oneOf(TELEMETRY_REVIEW_TIERS)}>] [--idempotency-key <key>] [--telemetry-run-id <id>] [--engine-version <v>] [--tokens-file <file>] [--lanes-file <file>] [--changeset-file <file> | --diff-file <file>] [--prompt-surface <glob>]... [--duration-seconds <n>] [--emitted-at <iso>] [--prompt-stack-sha256 <sha256>] [--prompt-stack-version <v>] [--repo-instructions-sha256 <sha256>] [--truncated] [--dry-run] [--actor <login>]`,
  },
  'enrich-telemetry-duration': {
    summary: 'Add a measured duration to an emitted telemetry record.',
    synopsis: `${SCOPE} --idempotency-key <key> --engine <engine> --round <n> --base ${SHA} --head ${SHA} --duration-seconds <n> [--actor <login>]`,
  },
  'replay-telemetry': {
    summary: 'Re-emit a saved telemetry recovery record.',
    synopsis: '--repo <owner/repo> --file <file> [--dry-run] [--actor <login>]',
  },
  'telemetry-recorded': {
    summary: 'Report whether a telemetry record exists.',
    synopsis: `${SCOPE} --idempotency-key <key> [--actor <login>]`,
  },
  'metrics-setup': {
    summary: 'Create the review metrics store.',
    synopsis: '--repo <owner/repo>',
  },
  'metrics-rotate': {
    summary: 'Rotate the review metrics store.',
    synopsis: '--repo <owner/repo>',
  },
  export: {
    summary: 'Export telemetry records as JSON lines.',
    synopsis:
      '--repo <owner/repo> (--pr <number> | --prs <n,n> | [--since <date>] [--until <date>]) [--allowed-logins <login,login>]',
  },
  report: {
    summary: 'Summarize telemetry records.',
    synopsis:
      '--repo <owner/repo> (--pr <number> | --prs <n,n> | [--since <date>] [--until <date>]) [--allowed-logins <login,login>]',
  },
  'read-result': {
    summary: 'Print a result file.',
    synopsis: '(--result-file <file> | --file <file>)',
  },
  'format-findings': {
    summary: 'Format a findings JSON array for a report.',
    synopsis: '(--json-file <file> | --file <file>)',
  },
  'silent-pin': { summary: 'Pin a PR to silent mode.', synopsis: SCOPE },
  'silent-status': {
    summary: 'Report whether a PR is silent.',
    synopsis: SCOPE,
  },
  summary: {
    summary: "Render a silent PR's end-of-loop summary.",
    synopsis: SCOPE,
  },
  'post-summary': {
    summary: "Post a silent PR's approved summary.",
    synopsis: `${SCOPE} --confirmed --expected-summary-sha256 <sha256>`,
  },
  'read-ledger': {
    summary: 'Print the PR ledger comments and threads.',
    synopsis: SCOPE,
  },
};

/** Every documented subcommand name, in table order. */
export const COMMAND_NAMES: readonly string[] = Object.keys(COMMANDS);

/** The synopsis for one subcommand, or undefined when it is not documented. */
export function commandSynopsis(command: string): string | undefined {
  return Object.hasOwn(COMMANDS, command)
    ? COMMANDS[command]!.synopsis
    : undefined;
}

/** Full help for one subcommand. */
export function commandHelp(command: string): string {
  const entry = COMMANDS[command]!;
  const lines = [
    `usage: review-ledger ${command} ${entry.synopsis}`,
    '',
    entry.summary,
  ];
  for (const note of entry.notes ?? []) lines.push(`- ${note}`);
  return lines.join('\n') + '\n';
}

/** The subcommand list printed by bare `--help`. */
export function overviewHelp(): string {
  const width = Math.max(...COMMAND_NAMES.map((name) => name.length));
  const lines = [
    'usage: review-ledger <subcommand> [flags]',
    '       review-ledger <subcommand> --help',
    '       review-ledger --version | --protocol-version',
    '',
    'Subcommands:',
    ...COMMAND_NAMES.map(
      (name) => `  ${name.padEnd(width)}  ${COMMANDS[name]!.summary}`,
    ),
  ];
  return lines.join('\n') + '\n';
}

/** The usage appended to an argument error for `command`. */
export function usageHint(command: string | undefined): string {
  if (command !== undefined && Object.hasOwn(COMMANDS, command)) {
    return `usage: review-ledger ${command} ${COMMANDS[command]!.synopsis}\nRun \`review-ledger ${command} --help\` for details; use only the flags it lists.`;
  }
  return `subcommands: ${COMMAND_NAMES.join(', ')}\nRun \`review-ledger <subcommand> --help\` for its flags.`;
}
