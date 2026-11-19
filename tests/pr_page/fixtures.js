'use strict';
// Pull requests as the bridge sends them, shared by the page's node tests.
const URL = 'https://github.com/acme/shop/pull/596';
const CAN = { merge: false, update: false, ready: false, draft: false, rerun: false };

/** Only what the server always sends: all a loading or failed pull request may have. */
const bare = (fields) => ({ url: URL, repo: 'acme/shop', number: 596, loading: false, error: null, busy: null, ...fields });

/** An open pull request with every field, that can be merged and nothing else. */
const pull = (fields) =>
  bare({
    title: 'Add the cart page',
    state: 'open',
    draft: false,
    head: 'feat/cart',
    base: 'main',
    author: 'alice',
    updated: '2026-11-18T10:00:00Z',
    additions: 12,
    deletions: 3,
    files: 2,
    behind: 0,
    conflicts: false,
    mergeState: 'clean',
    review: { decision: 'none', reviews: [], requested: [] },
    checks: [],
    methods: ['squash', 'merge', 'rebase'],
    can: { ...CAN, merge: true },
    ...fields,
  });

const checks = (...statuses) => statuses.map((status, index) => ({ name: `check-${index}`, workflow: null, status, url: null }));

module.exports = { URL, CAN, bare, pull, checks };
