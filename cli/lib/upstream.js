'use strict';

/**
 * Fetches prompt and skill trees from a tag-pinned upstream GitHub tarball at runtime.
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

/** The repository content is fetched from. Overridable for forks. */
const DEFAULT_UPSTREAM =
  process.env.ACTIVELOOM_UPSTREAM || 'loomantix/activeloom';

/** Default ref tracked by migrated consumer sync workflows. */
const DEFAULT_REF = 'sync-v2';

/**
 * Download and unpack an upstream tree, returning the directory it landed in.
 *
 * @param {object} [options]
 * @param {string} [options.ref]          Git ref to fetch. Defaults to `sync-v2`.
 * @param {string} [options.repo]         `owner/name`. Defaults to the upstream.
 * @param {string} [options.upstreamDir]  Use this local checkout instead of
 *   downloading. This is how the equivalence test runs the CLI against the same
 *   tree the sync engine is run against, and how a fork developer tests an
 *   unpushed change. It is not a way to skip the content gate in normal use.
 * @returns {Promise<{dir: string, ref: string, source: string, cleanup: () => void}>}
 */
async function resolveUpstream(options = {}) {
  const ref = options.ref ?? DEFAULT_REF;
  const repo = options.repo ?? DEFAULT_UPSTREAM;

  if (options.upstreamDir) {
    const dir = path.resolve(options.upstreamDir);
    if (!fs.existsSync(path.join(dir, 'scripts', 'sync-targets.yml'))) {
      throw new Error(
        `--upstream-dir ${dir} does not look like an activeloom checkout ` +
          `(no scripts/sync-targets.yml).`,
      );
    }
    return { dir, ref: 'local', source: dir, cleanup: () => {} };
  }

  // Encode each path segment so slashes in branch or tag names are preserved.
  const encoded = ref.split('/').map(encodeURIComponent).join('/');
  const candidates = [
    `https://codeload.github.com/${repo}/tar.gz/refs/tags/${encoded}`,
    `https://codeload.github.com/${repo}/tar.gz/refs/heads/${encoded}`,
  ];
  const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'activeloom-'));
  const cleanup = () => {
    try {
      fs.rmSync(workdir, { recursive: true, force: true });
    } catch {
      // Ignore tmpdir removal errors on cleanup.
    }
  };

  try {
    let response = null;
    let url = candidates[0];
    for (const candidate of candidates) {
      url = candidate;
      const attempt = await fetch(url, {
        headers: { 'user-agent': 'activeloom-cli' },
        redirect: 'follow',
      });
      if (attempt.status === 404) continue;
      response = attempt;
      break;
    }

    if (response === null) {
      // Ref was not found as either a tag or branch.
      throw new Error(
        `no tag or branch \`${ref}\` in ${repo}.\n` +
          `  If \`${ref}\` has not been cut yet, pin an existing ref explicitly:\n` +
          `      npx activeloom <command> --ref main\n` +
          `  Available refs: https://github.com/${repo}/tags`,
      );
    }
    if (!response.ok) {
      throw new Error(
        `could not fetch ${url} — HTTP ${response.status} ${response.statusText}`,
      );
    }

    const tarball = path.join(workdir, 'upstream.tar.gz');
    fs.writeFileSync(tarball, Buffer.from(await response.arrayBuffer()));

    const dir = path.join(workdir, 'upstream');
    fs.mkdirSync(dir);
    // Strip repository root directory so files unpack directly into dir.
    try {
      execFileSync(
        'tar',
        ['-xzf', tarball, '-C', dir, '--strip-components=1'],
        { stdio: ['ignore', 'ignore', 'pipe'] },
      );
    } catch (err) {
      // Capture stderr for tar extraction diagnostics.
      const detail = String(err.stderr ?? '').trim() || err.message;
      throw new Error(
        `could not unpack the archive from ${url}` +
          (detail ? `\n  tar: ${detail}` : ''),
      );
    }

    return { dir, ref, source: url, cleanup };
  } catch (err) {
    cleanup();
    throw err;
  }
}

module.exports = { resolveUpstream, DEFAULT_REF, DEFAULT_UPSTREAM };
