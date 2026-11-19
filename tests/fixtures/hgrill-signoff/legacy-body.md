<!-- hgrill-assumptions:start -->

> [!IMPORTANT]
> **Automated grill — needs a human check.** Engine A and Engine B grilled this issue on 2026-01-02 and accepted every recommendation. Remove `grill: hold` once you have checked this block.

**Refuted and not resolved — a decision still rests on this:**

- ❌ **The widget cache is per user** is false: it is shared per tenant (`cache.py:12`). Affects: decision 3.

**Product calls — yours to confirm:**

1. ⚠ **Who can export a `report` → CSV?** — the passes disagreed. Options: (a) only admins — alpha; (b) any member — beta. Applied now: (b). Recommended: (a), because exports carry #12 data. Evidence: none — the passes' judgment.
2. ⚠ **How long are drafts kept?** — the passes disagreed. Options: (a) 7 days — alpha; (b) 30 days — beta; (c) forever — gamma. Applied now: (c). Recommended: (b), because storage is bounded. Evidence: `retention.md`.
3. ⚠ **Which theme ships first?** — the passes disagreed. Options: (a) light — alpha; (b) dark — beta. Recommended: (a), because most users run light. Evidence: none — the passes' judgment.
4. **Undo is limited to $n \to 10$ steps** — alpha, beta. Evidence: none — the passes' judgment. If reversed: undo history grows without bound.
5. **Exports are queued, not instant** — alpha, beta. Evidence: none — the passes' judgment.

**Assumptions:**

1. **The `queue` worker retries `3` times** — alpha, beta. If wrong: failed exports are dropped silently.
2. ⚠ **Auto-selecting the single match is safe** — beta, against alpha. Reconciled: kept auto-select, because the digest pin limits it. If wrong: an edited row could be applied.
   It also assumes rows are immutable.

**Checked, no longer assumptions:**

- ✅ The export endpoint is rate limited (`api.py:40`).

Passes: [alpha](https://example.invalid/1) · [beta](https://example.invalid/2)

<!-- hgrill-assumptions:end -->

## Why

Synthetic fixture body.
