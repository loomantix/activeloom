#!/usr/bin/env node
// This distribution has no Agy pass-scoped usage adapter. Record the pass
// with unavailable tokens rather than inventing counts or omitting the pass.
import { resolveGates } from './review-telemetry-gates.js';

const gates = resolveGates();
const mode = process.argv[2];
process.stdout.write(
  JSON.stringify({
    mode: mode ?? null,
    enabled: gates.extraction.enabled,
    emit: gates.emission.enabled,
    emitReason: gates.emission.reason,
    reason: gates.extraction.reason ?? 'pass-scoped usage source unavailable',
    tokenSource: 'unavailable',
    tokensFile: null,
    lanesFile: null,
    engineVersion: null,
    durationSeconds: null,
    snapshotFile: null,
    scoped: false,
    error: gates.extraction.error ?? gates.emission.error ?? null,
  }) + '\n',
);
