'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const view = require('../../src/devin_switch/pr_page/pr-view');
const { URL, CAN, bare, pull, checks } = require('./fixtures');

const local = (fields) => ({ ...view.fresh(), ...fields });
const labels = (actions) => actions.map(({ label }) => label);

test('the state pill reads Open, Draft, Merged or Closed', () => {
  assert.deepEqual(view.pillOf(pull({ state: 'open' })), { kind: 'open', text: 'Open' });
  assert.deepEqual(view.pillOf(pull({ state: 'open', draft: true })), { kind: 'draft', text: 'Draft' });
  assert.deepEqual(view.pillOf(pull({ state: 'merged' })), { kind: 'merged', text: 'Merged' });
  assert.deepEqual(view.pillOf(pull({ state: 'closed' })), { kind: 'closed', text: 'Closed' });
  // Only an open pull request reads as a draft.
  assert.deepEqual(view.pillOf(pull({ state: 'merged', draft: true })), { kind: 'merged', text: 'Merged' });
  assert.deepEqual(view.pillOf(pull({ state: 'closed', draft: true })), { kind: 'closed', text: 'Closed' });
  assert.equal(view.pillOf(bare({})), null, 'no state yet');
  assert.equal(view.pillOf(pull({ state: 'constructor' })), null, 'a state the page does not know');
});

test('the heading names the repository and number, the meta line the branches, author, diff and files', () => {
  assert.equal(view.headingOf(pull({})), 'acme/shop #596');
  assert.equal(view.headingOf(bare({ repo: 'o/r', number: 7 })), 'o/r #7');
  assert.deepEqual(view.metaOf(pull({})), [
    { kind: 'branch', text: 'feat/cart → main' },
    { kind: 'author', text: 'alice' },
    { kind: 'added', text: '+12' },
    { kind: 'removed', text: '−3' },
    { kind: 'files', text: '2 files' },
  ]);
  assert.deepEqual(
    view.metaOf(pull({ head: 'fix', base: 'release', author: 'bob', additions: 0, deletions: 0, files: 1 })).map(({ text }) => text),
    ['fix → release', 'bob', '+0', '−0', '1 file'],
  );
});

test('one file is singular, every other count plural', () => {
  for (const [count, text] of [[0, '0 files'], [1, '1 file'], [2, '2 files'], [11, '11 files'], [100, '100 files']]) {
    assert.equal(view.filesLabel(count), text, `filesLabel(${count})`);
  }
});

test('the up-to-date row counts the commits behind the base, or says it cannot tell', () => {
  const cases = [
    [0, 'good', 'Up to date with main'],
    [1, 'warn', '1 commit behind main'],
    [2, 'warn', '2 commits behind main'],
    [37, 'warn', '37 commits behind main'],
    [null, 'unknown', "Can't tell if it is behind main"],
  ];
  for (const [behind, tone, text] of cases) assert.deepEqual(view.behindRow(pull({ behind })), { tone, text }, `behind ${behind}`);
  assert.equal(view.behindRow(pull({ behind: 0, base: 'release/2' })).text, 'Up to date with release/2');
  assert.equal(view.behindRow(pull({ behind: 1, base: 'dev' })).text, '1 commit behind dev');
  assert.equal(view.behindRow(pull({ behind: null, base: 'dev' })).text, "Can't tell if it is behind dev");
});

test('the conflicts row says none, some, or still checking', () => {
  assert.deepEqual(view.conflictsRow(pull({ conflicts: false })), { tone: 'good', text: 'No conflicts' });
  assert.deepEqual(view.conflictsRow(pull({ conflicts: true })), { tone: 'bad', text: 'Has merge conflicts' });
  assert.deepEqual(view.conflictsRow(pull({ conflicts: null })), { tone: 'wait', text: 'Checking for conflicts…' });
});

test('the checks summary counts failed, running, passed, skipped in that order and leaves out zeros', () => {
  const cases = [
    [[], 'No checks'],
    [['pass'], '1 passed'],
    [['pass', 'pass', 'pass'], '3 passed'],
    [['pass', 'fail', 'pass'], '1 failed, 2 passed'],
    [['pass', 'pending', 'pass'], '1 running, 2 passed'],
    [['fail'], '1 failed'],
    [['fail', 'fail'], '2 failed'],
    [['pending'], '1 running'],
    [['pending', 'pending'], '2 running'],
    [['skipped'], '1 skipped'],
    [['skipped', 'pass', 'skipped'], '1 passed, 2 skipped'],
    [['skipped', 'pass', 'pending', 'fail'], '1 failed, 1 running, 1 passed, 1 skipped'],
    [['pass', 'skipped', 'pending', 'pending', 'fail', 'fail', 'fail', 'pass'], '3 failed, 2 running, 2 passed, 1 skipped'],
    [['pending', 'skipped'], '1 running, 1 skipped'],
  ];
  for (const [statuses, text] of cases) assert.equal(view.checksSummary(checks(...statuses)), text, statuses.join(','));
});

test('checks that failed or still run are the ones that are not green', () => {
  const cases = [
    [[], 0],
    [['pass', 'skipped'], 0],
    [['fail'], 1],
    [['pending'], 1],
    [['fail', 'pending', 'pass'], 2],
    [['fail', 'fail', 'pending', 'pending', 'pending', 'skipped'], 5],
  ];
  for (const [statuses, count] of cases) assert.equal(view.notGreen(checks(...statuses)), count, statuses.join(','));
});

test('the checks row takes the tone of its worst check', () => {
  const tone = (...statuses) => view.checksRow(pull({ checks: checks(...statuses) }), view.fresh()).tone;
  assert.equal(tone(), 'quiet');
  assert.equal(tone('pass'), 'good');
  assert.equal(tone('skipped'), 'quiet');
  assert.equal(tone('skipped', 'pass'), 'good');
  assert.equal(tone('pass', 'pending'), 'wait');
  assert.equal(tone('pass', 'pending', 'fail'), 'bad');
  assert.equal(tone('fail', 'skipped'), 'bad');
});

test('each check is listed with its name, workflow, status word and link', () => {
  const list = [
    { name: 'build', workflow: 'CI', status: 'fail', url: 'https://github.com/acme/shop/actions/runs/1/job/2' },
    { name: 'lint', workflow: null, status: 'pass', url: null },
    { name: 'deploy', workflow: 'Release', status: 'pending', url: 'https://ci.example/3' },
    { name: 'docs', workflow: 'CI', status: 'skipped', url: null },
  ];
  const row = view.checksRow(pull({ checks: list }), view.fresh());
  assert.equal(row.text, '1 failed, 1 running, 1 passed, 1 skipped');
  assert.deepEqual(row.list, [
    { name: 'build', workflow: 'CI', tone: 'bad', word: 'Failed', url: 'https://github.com/acme/shop/actions/runs/1/job/2' },
    { name: 'lint', workflow: '', tone: 'good', word: 'Passed', url: null },
    { name: 'deploy', workflow: 'Release', tone: 'wait', word: 'Running', url: 'https://ci.example/3' },
    { name: 'docs', workflow: 'CI', tone: 'quiet', word: 'Skipped', url: null },
  ]);
  // A check that leaves the optional fields out, and a status the page does not know, still get a line.
  const odd = view.checksRow(pull({ checks: [{ name: 'x', status: 'fail' }, { name: 'y', status: 'neutral' }] }), view.fresh());
  assert.deepEqual(odd.list, [
    { name: 'x', workflow: '', tone: 'bad', word: 'Failed', url: null },
    { name: 'y', workflow: '', tone: 'unknown', word: 'Unknown', url: null },
  ]);
});

test('the check list is open while a check is not green, until the reader opens or closes it', () => {
  const row = (statuses, state = {}) => view.checksRow(pull({ checks: checks(...statuses) }), local(state));
  const shape = ({ open, toggle, list }) => [open, toggle, list.length];
  assert.deepEqual(shape(row(['pass', 'pass'])), [false, 'Show checks', 0], 'all green: closed');
  assert.deepEqual(shape(row(['pass', 'skipped'])), [false, 'Show checks', 0]);
  assert.deepEqual(shape(row(['pass', 'fail'])), [true, 'Hide checks', 2], 'a failure: open');
  assert.deepEqual(shape(row(['pending', 'pass', 'pass'])), [true, 'Hide checks', 3], 'one still running: open');
  // The reader's choice wins either way.
  assert.deepEqual(shape(row(['pass', 'pass'], { checksOpen: true })), [true, 'Hide checks', 2]);
  assert.deepEqual(shape(row(['pass', 'fail'], { checksOpen: false })), [false, 'Show checks', 0]);
  // Without checks there is nothing to open, whatever was chosen before.
  assert.deepEqual(shape(row([])), [false, '', 0]);
  assert.deepEqual(shape(row([], { checksOpen: true })), [false, '', 0]);
});

test('the review row gives the decision, each reviewer and who is still asked', () => {
  const review = (fields) => view.reviewRow(pull({ review: { decision: 'none', reviews: [], requested: [], ...fields } }));
  assert.deepEqual(review({ decision: 'approved' }), { tone: 'good', text: 'Approved', reviewers: [], waiting: '' });
  assert.deepEqual(review({ decision: 'changes_requested' }), { tone: 'bad', text: 'Changes requested', reviewers: [], waiting: '' });
  assert.deepEqual(review({ decision: 'review_required' }), { tone: 'warn', text: 'Review required', reviewers: [], waiting: '' });
  assert.deepEqual(review({ decision: 'none' }), { tone: 'quiet', text: 'No review yet', reviewers: [], waiting: '' });

  const reviews = [
    { author: 'alice', state: 'approved' },
    { author: 'bob', state: 'changes_requested' },
    { author: 'carol', state: 'commented' },
    { author: 'erin', state: 'dismissed' },
    { author: 'frank', state: 'pending' },
  ];
  assert.deepEqual(review({ decision: 'changes_requested', reviews, requested: ['dave', 'team-x'] }), {
    tone: 'bad',
    text: 'Changes requested',
    reviewers: ['alice approved', 'bob requested changes', 'carol commented', "erin's review was dismissed", 'frank has a pending review'],
    waiting: 'Waiting on dave, team-x',
  });
  assert.equal(review({ requested: ['dave'] }).waiting, 'Waiting on dave');
  assert.equal(review({ decision: 'review_required', requested: ['a', 'b', 'c'] }).waiting, 'Waiting on a, b, c');
});

test('an open pull request always has Merge and gets the other buttons only from `can`', () => {
  const actions = (can) => view.actionsOf(pull({ can: { ...CAN, ...can } }), view.fresh());
  assert.deepEqual(actions({}), [{ action: 'merge', label: 'Merge', enabled: false }]);
  assert.deepEqual(actions({ merge: true }), [{ action: 'merge', label: 'Merge', enabled: true }]);
  assert.deepEqual(actions({ update: true }), [
    { action: 'merge', label: 'Merge', enabled: false },
    { action: 'update', label: 'Update branch', enabled: true },
  ]);
  assert.deepEqual(labels(actions({ ready: true })), ['Merge', 'Mark ready']);
  assert.deepEqual(labels(actions({ draft: true })), ['Merge', 'Convert to draft']);
  assert.deepEqual(labels(actions({ rerun: true })), ['Merge', 'Re-run failed checks']);
  assert.deepEqual(actions({ merge: true, update: true, ready: true, draft: true, rerun: true }), [
    { action: 'merge', label: 'Merge', enabled: true },
    { action: 'update', label: 'Update branch', enabled: true },
    { action: 'ready', label: 'Mark ready', enabled: true },
    { action: 'draft', label: 'Convert to draft', enabled: true },
    { action: 'rerun', label: 'Re-run failed checks', enabled: true },
  ]);
});

test('a Merge the server refuses is off and carries the server\'s reason', () => {
  const blocked = pull({ can: { ...CAN, update: true }, why: { merge: 'Needs 1 approving review' } });
  const card = view.cardOf(blocked, view.fresh());
  assert.deepEqual(card.actions[0], { action: 'merge', label: 'Merge', enabled: false });
  assert.equal(card.reason, 'Needs 1 approving review');
  assert.equal(view.reasonOf(blocked), 'Needs 1 approving review');
  // No reason sent, or a Merge that is allowed: no reason shown.
  assert.equal(view.reasonOf(pull({ can: CAN })), '');
  assert.equal(view.reasonOf(pull({ can: { ...CAN, merge: true }, why: { merge: 'stale text' } })), '');
  assert.equal(view.cardOf(pull({}), view.fresh()).reason, '');
});

test('while an action runs every button is off and the running one says so', () => {
  const all = { merge: true, update: true, ready: true, draft: true, rerun: true };
  const cases = [
    ['merge', 'Merging…'],
    ['update', 'Updating…'],
    ['ready', 'Marking ready…'],
    ['draft', 'Converting…'],
    ['rerun', 'Re-running…'],
  ];
  const idle = ['Merge', 'Update branch', 'Mark ready', 'Convert to draft', 'Re-run failed checks'];
  for (const [busy, text] of cases) {
    const expected = idle.map((label, index) => (index === cases.findIndex(([name]) => name === busy) ? text : label));
    for (const [where, actions] of [
      ['server', view.actionsOf(pull({ can: all, busy }), view.fresh())],
      ['page', view.actionsOf(pull({ can: all }), local({ pending: busy }))],
    ]) {
      assert.deepEqual(labels(actions), expected, `${busy} running on the ${where}`);
      assert.deepEqual(actions.map(({ enabled }) => enabled), [false, false, false, false, false], `${busy} running on the ${where}`);
    }
  }
  assert.deepEqual(view.actionsOf(pull({ can: all }), view.fresh()).map(({ enabled }) => enabled), [true, true, true, true, true]);
});

test('the running button stays on the card even when the server no longer allows the action', () => {
  const updating = view.actionsOf(pull({ can: CAN, busy: 'update' }), view.fresh());
  assert.deepEqual(updating, [
    { action: 'merge', label: 'Merge', enabled: false },
    { action: 'update', label: 'Updating…', enabled: false },
  ]);
  assert.deepEqual(labels(view.actionsOf(pull({ can: CAN }), local({ pending: 'rerun' }))), ['Merge', 'Re-running…']);
  // The server's word on what runs comes before the page's own request.
  assert.deepEqual(labels(view.actionsOf(pull({ can: CAN, busy: 'ready' }), local({ pending: 'update' }))), ['Merge', 'Marking ready…']);
});

test('removing a pull request from the tab locks its card without a running label', () => {
  const card = view.cardOf(pull({ can: { ...CAN, merge: true, update: true } }), local({ pending: 'dismiss' }));
  assert.deepEqual(card.actions, [
    { action: 'merge', label: 'Merge', enabled: false },
    { action: 'update', label: 'Update branch', enabled: false },
  ]);
  assert.equal(card.locked, true);
  assert.equal(view.cardOf(pull({}), view.fresh()).locked, false);
  // Only the page's own request locks the remove button; the server's action is the server's to refuse.
  assert.equal(view.cardOf(pull({ busy: 'merge' }), view.fresh()).locked, false);
  assert.equal(view.cardOf(bare({ loading: true }), local({ pending: 'dismiss' })).locked, true);
});

test('the confirm step offers the repository\'s merge methods with the first one chosen', () => {
  const confirming = view.startMerge(view.fresh());
  assert.deepEqual(view.confirmOf(pull({ methods: ['squash', 'merge', 'rebase'] }), confirming), {
    method: 'squash',
    methods: [
      { value: 'squash', label: 'Squash and merge', chosen: true },
      { value: 'merge', label: 'Merge commit', chosen: false },
      { value: 'rebase', label: 'Rebase and merge', chosen: false },
    ],
    chooser: true,
    warning: '',
    enabled: true,
  });
  const other = view.confirmOf(pull({ methods: ['merge', 'rebase'] }), confirming);
  assert.equal(other.method, 'merge');
  assert.deepEqual(other.methods.map(({ label, chosen }) => [label, chosen]), [['Merge commit', true], ['Rebase and merge', false]]);
  assert.equal(other.chooser, true);
});

test('the method the reader chose stays chosen while the repository allows it', () => {
  const chosen = view.chooseMethod(view.startMerge(view.fresh()), 'rebase');
  const confirm = view.confirmOf(pull({ methods: ['squash', 'merge', 'rebase'] }), chosen);
  assert.equal(confirm.method, 'rebase');
  assert.deepEqual(confirm.methods.map(({ chosen: picked }) => picked), [false, false, true]);
  // A method the repository does not offer falls back to its default.
  const fallback = view.confirmOf(pull({ methods: ['squash', 'merge'] }), chosen);
  assert.equal(fallback.method, 'squash');
  assert.deepEqual(fallback.methods.map(({ chosen: picked }) => picked), [true, false]);
});

test('one merge method needs no chooser, and none leaves the method to the server', () => {
  const confirming = view.startMerge(view.fresh());
  const single = view.confirmOf(pull({ methods: ['rebase'] }), confirming);
  assert.equal(single.chooser, false);
  assert.equal(single.method, 'rebase');
  assert.deepEqual(single.methods, [{ value: 'rebase', label: 'Rebase and merge', chosen: true }]);
  const none = view.confirmOf(pull({ methods: [] }), confirming);
  assert.equal(none.chooser, false);
  assert.equal(none.method, null);
  assert.deepEqual(none.methods, []);
});

test('the confirm step warns about every check that failed or still runs', () => {
  const warning = (...statuses) => view.confirmOf(pull({ checks: checks(...statuses) }), view.startMerge(view.fresh())).warning;
  assert.equal(warning(), '');
  assert.equal(warning('pass', 'skipped', 'pass'), '');
  assert.equal(warning('fail'), '1 check is not green');
  assert.equal(warning('pending', 'pass'), '1 check is not green');
  assert.equal(warning('fail', 'pending'), '2 checks are not green');
  assert.equal(warning('fail', 'fail', 'pending', 'pass', 'skipped', 'pending'), '4 checks are not green');
});

test('the confirm step shows only when the reader opened it on a pull request that can be merged', () => {
  const confirming = view.startMerge(view.fresh());
  assert.equal(view.confirmOf(pull({}), view.fresh()), null, 'not opened');
  assert.equal(view.confirmOf(pull({ can: CAN }), confirming), null, 'the server says no');
  assert.equal(view.confirmOf(pull({ state: 'merged' }), confirming), null);
  assert.equal(view.confirmOf(pull({ state: 'closed' }), confirming), null);
  assert.equal(view.confirmOf(bare({ loading: true }), confirming), null);
  assert.equal(view.confirmOf(bare({ error: 'gh failed' }), confirming), null);

  // It takes the place of the buttons, so nothing else can be pressed by accident next to Confirm merge.
  const all = { merge: true, update: true, ready: false, draft: true, rerun: true };
  const card = view.cardOf(pull({ can: all }), confirming);
  assert.notEqual(card.confirm, null);
  assert.deepEqual(card.actions, []);
  assert.equal(card.reason, '');
  const closed = view.cardOf(pull({ can: all }), view.cancelMerge(confirming));
  assert.equal(closed.confirm, null);
  assert.deepEqual(labels(closed.actions), ['Merge', 'Update branch', 'Convert to draft', 'Re-run failed checks']);

  // Confirm merge is off while the card is locked by a request of the page.
  assert.equal(view.confirmOf(pull({}), confirming).enabled, true);
  assert.equal(view.confirmOf(pull({}), { ...confirming, pending: 'dismiss' }).enabled, false);
  assert.equal(view.confirmOf(pull({ busy: 'rerun' }), confirming).enabled, false);
});

test('a loading card shows only its heading and never reads the missing fields', () => {
  const loading = bare({ loading: true });
  assert.equal(view.kindOf(loading), 'loading');
  const used = local({ confirming: true, method: 'squash', checksOpen: true, pending: null });
  assert.deepEqual(view.cardOf(loading, used), {
    kind: 'loading',
    heading: 'acme/shop #596',
    title: '',
    pill: null,
    note: 'Loading…',
    meta: [],
    status: null,
    actions: [],
    reason: '',
    confirm: null,
    error: '',
    locked: false,
  });
  // A refresh may mark a pull request that already has its details as loading: still only the heading.
  const again = view.cardOf(pull({ loading: true }), view.fresh());
  assert.deepEqual([again.kind, again.note, again.title, again.pill, again.status, again.actions], ['loading', 'Loading…', '', null, null, []]);
  assert.deepEqual(view.cardOf(loading), view.cardOf(loading, view.fresh()), 'the page state is optional');
});

test('a failed card shows its heading and the error and never reads the missing fields', () => {
  const failed = bare({ error: 'gh: Could not resolve to a PullRequest' });
  assert.equal(view.kindOf(failed), 'error');
  assert.deepEqual(view.cardOf(failed, local({ confirming: true, method: 'rebase' })), {
    kind: 'error',
    heading: 'acme/shop #596',
    title: '',
    pill: null,
    note: 'gh: Could not resolve to a PullRequest',
    meta: [],
    status: null,
    actions: [],
    reason: '',
    confirm: null,
    error: '',
    locked: false,
  });
  // While the server tries again the card reads Loading…, not the old error.
  assert.equal(view.cardOf(bare({ loading: true, error: 'gh failed' }), view.fresh()).note, 'Loading…');
  // An error wins over details that came along with it.
  const stale = view.cardOf(pull({ error: 'gh timed out' }), view.fresh());
  assert.deepEqual([stale.kind, stale.note, stale.status, stale.actions], ['error', 'gh timed out', null, []]);
});

test('merged and closed cards keep the heading, title, pill and meta line and have no status or actions', () => {
  const all = { merge: true, update: true, ready: true, draft: true, rerun: true };
  for (const [state, text] of [['merged', 'Merged'], ['closed', 'Closed']]) {
    const done = pull({ state, can: all, busy: 'merge', why: { merge: 'x' }, checks: checks('fail') });
    assert.equal(view.kindOf(done), 'done');
    const card = view.cardOf(done, local({ confirming: true, checksOpen: true }));
    assert.deepEqual(card, {
      kind: 'done',
      heading: 'acme/shop #596',
      title: 'Add the cart page',
      pill: { kind: state, text },
      note: '',
      meta: view.metaOf(done),
      status: null,
      actions: [],
      reason: '',
      confirm: null,
      error: '',
      locked: false,
    });
    assert.equal(card.meta.length, 5);
  }
});

test('an open card carries the title, pill, meta line, the four status rows and its buttons', () => {
  const open = pull({
    draft: true,
    behind: 2,
    conflicts: null,
    checks: checks('pass', 'pass'),
    review: { decision: 'review_required', reviews: [], requested: ['dave'] },
    can: { ...CAN, ready: true },
    why: { merge: 'This pull request is a draft' },
  });
  assert.equal(view.kindOf(open), 'open');
  assert.deepEqual(view.cardOf(open, view.fresh()), {
    kind: 'open',
    heading: 'acme/shop #596',
    title: 'Add the cart page',
    pill: { kind: 'draft', text: 'Draft' },
    note: '',
    meta: view.metaOf(open),
    status: {
      behind: { tone: 'warn', text: '2 commits behind main' },
      conflicts: { tone: 'wait', text: 'Checking for conflicts…' },
      checks: { tone: 'good', text: '2 passed', toggle: 'Show checks', open: false, list: [] },
      review: { tone: 'warn', text: 'Review required', reviewers: [], waiting: 'Waiting on dave' },
    },
    actions: [
      { action: 'merge', label: 'Merge', enabled: false },
      { action: 'ready', label: 'Mark ready', enabled: true },
    ],
    reason: 'This pull request is a draft',
    confirm: null,
    error: '',
    locked: false,
  });
});

test('a card is plain data that reads the same for the same input, so an unchanged card is not drawn again', () => {
  const open = pull({ checks: checks('fail', 'pass'), can: { ...CAN, merge: true, rerun: true } });
  for (const state of [view.fresh(), view.startMerge(view.fresh()), local({ error: 'no', pending: 'merge' })]) {
    const card = view.cardOf(open, state);
    assert.deepEqual(JSON.parse(JSON.stringify(card)), card);
    assert.equal(JSON.stringify(view.cardOf(structuredClone(open), { ...state })), JSON.stringify(card));
  }
  assert.notEqual(JSON.stringify(view.cardOf(open, view.fresh())), JSON.stringify(view.cardOf(open, view.startMerge(view.fresh()))));
  // Every tone a card can carry has a glyph, so no status reads by colour alone.
  const tones = ['good', 'bad', 'warn', 'wait', 'unknown', 'quiet'];
  assert.deepEqual(Object.keys(view.ICONS).sort(), [...tones].sort());
  assert.equal(new Set(Object.values(view.ICONS)).size, tones.length);
});

test('the error of an action shows on any kind of card until it is hidden', () => {
  const failed = view.ended(view.began(view.fresh(), 'merge'), 'Merge failed: required check is pending');
  for (const shown of [pull({}), pull({ state: 'merged' }), bare({ loading: true }), bare({ error: 'gh failed' })]) {
    assert.equal(view.cardOf(shown, failed).error, 'Merge failed: required check is pending', view.kindOf(shown));
    assert.equal(view.cardOf(shown, view.hideError(failed)).error, '', view.kindOf(shown));
  }
});

test('a snapshot keeps what the reader did to a pull request', () => {
  const used = local({ confirming: true, method: 'rebase', checksOpen: false, error: 'Merge failed', pending: null });
  const before = structuredClone(used);
  assert.deepEqual(view.reconcile(used, pull({})), used);
  assert.deepEqual(view.reconcile(used, pull({ title: 'Renamed', behind: 3, checks: checks('fail'), can: { ...CAN, merge: true, update: true } })), used);
  assert.deepEqual(used, before, 'the state is not changed in place');
  // The request of the page that is still out survives too.
  assert.equal(view.reconcile(local({ pending: 'update' }), pull({ busy: 'update' })).pending, 'update');
  assert.equal(view.reconcile(local({ pending: 'dismiss' }), pull({})).pending, 'dismiss');
});

test('the confirm step closes when the pull request can no longer be merged, and stays closed', () => {
  const confirming = local({ confirming: true, method: 'merge', checksOpen: true, error: 'Earlier error' });
  const blocked = view.reconcile(confirming, pull({ can: CAN, why: { merge: 'Checks are failing' } }));
  assert.deepEqual(blocked, { ...confirming, confirming: false }, 'only the confirm step is dropped');
  const again = view.reconcile(blocked, pull({}));
  assert.equal(again.confirming, false, 'it does not open again by itself');
  assert.equal(view.cardOf(pull({}), again).confirm, null);

  assert.equal(view.reconcile(confirming, pull({ state: 'merged', can: CAN })).confirming, false);
  assert.equal(view.reconcile(confirming, pull({ state: 'closed', can: CAN })).confirming, false);
  assert.equal(view.reconcile(confirming, pull({ state: 'merged' })).confirming, false, 'merged, whatever `can` says');
  assert.equal(view.reconcile(confirming, pull({ busy: 'update' })).confirming, false, 'something else runs on it');
  assert.equal(view.reconcile(confirming, bare({ error: 'gh failed' })).confirming, false, 'it could not be loaded');
});

test('a pull request that is loading keeps its whole page state, without reading the missing fields', () => {
  const used = local({ confirming: true, method: 'rebase', checksOpen: true, error: 'Earlier error' });
  assert.deepEqual(view.reconcile(used, bare({ loading: true })), used);
  // Back with its details and still mergeable: the confirm step is where the reader left it.
  const back = view.reconcile(view.reconcile(used, bare({ loading: true })), pull({}));
  assert.equal(view.cardOf(pull({}), back).confirm.method, 'rebase');
  // A failed one keeps everything but the confirm step.
  assert.deepEqual(view.reconcile(used, bare({ error: 'gh failed' })), { ...used, confirming: false });
});

test('the chosen merge method is dropped once the repository no longer offers it', () => {
  const chosen = local({ confirming: true, method: 'rebase' });
  assert.equal(view.reconcile(chosen, pull({ methods: ['squash', 'merge', 'rebase'] })).method, 'rebase');
  const dropped = view.reconcile(chosen, pull({ methods: ['squash', 'merge'] }));
  assert.equal(dropped.method, null);
  assert.equal(dropped.confirming, true, 'the confirm step itself stays, on the default method');
  assert.equal(view.confirmOf(pull({ methods: ['squash', 'merge'] }), dropped).method, 'squash');
  // Offered again later: the default stays, the old choice does not come back.
  assert.equal(view.confirmOf(pull({}), view.reconcile(dropped, pull({}))).method, 'squash');
  assert.equal(view.reconcile(view.fresh(), pull({})).method, null);
});

test('page state is kept per pull request url and forgotten when the pull request leaves the tab', () => {
  const other = 'https://github.com/acme/shop/pull/600';
  const fresh = 'https://github.com/acme/api/pull/3';
  const states = new Map([
    [URL, local({ confirming: true, method: 'merge' })],
    [other, local({ error: 'Update failed', checksOpen: true })],
    ['https://github.com/acme/shop/pull/1', local({ error: 'gone' })],
  ]);
  const next = view.reconcileAll(states, [pull({ can: CAN }), pull({ url: other, number: 600 }), bare({ url: fresh, repo: 'acme/api', number: 3, loading: true })]);
  assert.deepEqual([...next.keys()], [URL, other, fresh]);
  assert.deepEqual(next.get(URL), local({ confirming: false, method: 'merge' }));
  assert.deepEqual(next.get(other), local({ error: 'Update failed', checksOpen: true }));
  assert.deepEqual(next.get(fresh), view.fresh());
  assert.equal(states.size, 3, 'the earlier map is left alone');
  assert.equal(states.get(URL).confirming, true);
  // Removed and added again: it starts over.
  const back = view.reconcileAll(view.reconcileAll(next, []), [pull({ url: other, number: 600 })]);
  assert.deepEqual(back.get(other), view.fresh());
  assert.deepEqual([...view.reconcileAll(new Map(), []).keys()], []);
});

test('Merge opens the confirm step, Cancel closes it, and neither sends anything', () => {
  const start = view.fresh();
  assert.deepEqual(start, { confirming: false, method: null, checksOpen: null, error: null, pending: null });
  const confirming = view.startMerge(start);
  assert.deepEqual(confirming, { ...start, confirming: true });
  assert.equal(start.confirming, false, 'the earlier state is left alone');
  assert.equal(confirming.pending, null, 'opening the step is not a merge');
  assert.deepEqual(view.chooseMethod(confirming, 'rebase'), { ...confirming, method: 'rebase' });
  assert.deepEqual(view.cancelMerge(view.chooseMethod(confirming, 'rebase')), { ...start, method: 'rebase' }, 'the chosen method is remembered');
  assert.deepEqual(view.showChecks(start, true), { ...start, checksOpen: true });
  assert.deepEqual(view.showChecks(view.showChecks(start, true), false), { ...start, checksOpen: false });
});

test('an action locks the card until its answer, which sets or clears the error line', () => {
  const confirming = view.chooseMethod(view.startMerge(view.fresh()), 'squash');
  const merging = view.began(confirming, 'merge');
  assert.deepEqual(merging, { ...confirming, confirming: false, pending: 'merge' });
  assert.deepEqual(labels(view.cardOf(pull({}), merging).actions), ['Merging…']);

  const failed = view.ended(merging, 'Merge failed');
  assert.deepEqual(failed, { ...merging, pending: null, error: 'Merge failed' });
  // The line stays while the next try is out, and goes when that one works.
  const retry = view.began(failed, 'merge');
  assert.equal(retry.error, 'Merge failed');
  assert.equal(view.ended(retry, null).error, null);
  assert.equal(view.ended(retry, 'Still failing').error, 'Still failing');
  assert.deepEqual(view.hideError(failed), { ...failed, error: null });
});

test('opening a link neither locks the card nor clears an earlier error', () => {
  const failed = view.ended(view.began(view.fresh(), 'update'), 'Update failed');
  assert.equal(view.opened(failed, null), failed);
  assert.deepEqual(view.opened(failed, 'Could not open the link'), { ...failed, error: 'Could not open the link' });
  // A request that is still out stays out.
  const merging = view.began(view.fresh(), 'merge');
  assert.equal(view.opened(merging, null).pending, 'merge');
  assert.equal(view.opened(merging, 'Could not open the link').pending, 'merge');
});

test('an action request carries the chat, the action and only the fields that action has', () => {
  assert.deepEqual(view.actBody('chat-1', 'merge', { url: URL, method: 'squash' }), { session: 'chat-1', url: URL, action: 'merge', method: 'squash' });
  assert.deepEqual(view.actBody('chat-1', 'update', { url: URL }), { session: 'chat-1', url: URL, action: 'update' });
  assert.deepEqual(view.actBody('chat-1', 'dismiss', { url: URL }), { session: 'chat-1', url: URL, action: 'dismiss' });
  assert.deepEqual(view.actBody('chat-1', 'refresh'), { session: 'chat-1', action: 'refresh' });
  assert.deepEqual(view.actBody('chat-1', 'open', { url: URL }), { session: 'chat-1', url: URL, action: 'open' });
  const check = 'https://github.com/acme/shop/actions/runs/1/job/2';
  assert.deepEqual(view.actBody('chat-1', 'open', { url: URL, target: check }), { session: 'chat-1', url: URL, action: 'open', target: check });
  // A check without a link and a repository without merge methods leave the field out instead of sending null.
  assert.deepEqual(view.actBody('chat-1', 'open', { url: URL, target: null }), { session: 'chat-1', url: URL, action: 'open' });
  assert.deepEqual(view.actBody('chat-1', 'merge', { url: URL, method: null }), { session: 'chat-1', url: URL, action: 'merge' });
});

test('only {"ok": true} is a success; everything else gives a line to show', () => {
  assert.equal(view.replyError({ ok: true }), null);
  assert.equal(view.replyError({ error: 'Merge failed: base branch was modified' }), 'Merge failed: base branch was modified');
  assert.equal(view.replyError({ error: '  padded  ' }), 'padded');
  const generic = view.replyError(null);
  assert.equal(generic, "That didn't work. Try again.");
  // A reply that is not 200, not JSON or not in the agreed shape gets the generic line.
  for (const reply of [undefined, {}, { ok: false }, { ok: 'true' }, { ok: 1 }, { error: '' }, { error: '   ' }, { error: 42 }, { error: null }, [], 'ok', 0]) {
    assert.equal(view.replyError(reply), generic, JSON.stringify(reply));
  }
  assert.equal(view.replyError({ ok: false, error: 'No such pull request' }), 'No such pull request');
});

test('the updated label is the local time with two digits per part', () => {
  assert.equal(view.updatedLabel(new Date(2026, 10, 18, 12, 34, 56)), 'Updated 12:34:56');
  assert.equal(view.updatedLabel(new Date(2026, 0, 1, 9, 5, 7)), 'Updated 09:05:07');
  assert.equal(view.updatedLabel(new Date(2026, 0, 1, 0, 0, 0)), 'Updated 00:00:00');
  assert.equal(view.updatedLabel(new Date(2026, 0, 1, 23, 59, 59)), 'Updated 23:59:59');
});
