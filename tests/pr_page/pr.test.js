'use strict';
// The page itself (pr.js) in a stand-in browser: what it draws, and what it sends to the bridge and when.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { openPage } = require('./fake-browser');
const { URL, CAN, bare, pull, checks } = require('./fixtures');

const PAGE = path.join(__dirname, '../../src/devin_switch/pr_page');
const ACTIONS = ['merge', 'update', 'ready', 'draft', 'rerun'];
const ALL = { merge: true, update: true, ready: true, draft: true, rerun: true };
const OTHER = 'https://github.com/acme/api/pull/3';
const snapshot = (...pulls) => ({ pulls, notice: null });
/** A page that already got one snapshot. */
function pageWith(...pulls) {
  const page = openPage();
  page.send(snapshot(...pulls));
  return page;
}
/** The action buttons a card shows: [label, can be pressed]. */
const buttons = (page, root) => page.seen(root).filter((node) => ACTIONS.includes(node.dataset.key)).map((node) => [page.text(node), !node.disabled]);
const texts = (page, className, root) => page.all(className, root).map((node) => page.text(node));
const bodies = (page) => page.requests.map(({ body }) => body);
const focusedKey = (page) => page.document.activeElement.dataset.key;

test('the page builds itself in the empty body and listens to the stream of its chat', () => {
  const page = openPage({ session: 'chat 1', token: 't&k' });
  assert.equal(page.source.url, '/pr/events?session=chat%201&t=t%26k');
  assert.equal(page.text(page.one('toolbar')), 'Pull requestsRefresh', 'no time before the first snapshot');
  assert.deepEqual(page.all('empty'), [], 'nothing is known yet, so nothing is said to be missing');
  assert.deepEqual(page.all('card'), []);
  assert.equal(page.requests.length, 0);

  page.send(snapshot());
  assert.deepEqual(page.one('empty').children.map((node) => node.textContent), [
    'No pull requests in this chat yet.',
    'Paste a PR link in the chat, or type /pr <link or number>.',
  ]);
  assert.match(page.text(page.one('updated')), /^Updated \d\d:\d\d:\d\d$/);

  page.send(snapshot(pull({})));
  assert.deepEqual(page.all('empty'), []);
  assert.equal(page.all('card').length, 1);
});

test('a card shows the pull request as text, whatever GitHub put in it', () => {
  const title = '<img src=x onerror=alert(1)> & <b>bold</b>';
  const page = pageWith(
    pull({
      title,
      author: '<i>eve</i>',
      behind: 2,
      conflicts: true,
      checks: [
        { name: '<script>build</script>', workflow: 'CI', status: 'fail', url: null },
        { name: 'lint', workflow: null, status: 'pass', url: null },
      ],
      review: { decision: 'approved', reviews: [{ author: 'bob', state: 'approved' }, { author: 'carol', state: 'commented' }], requested: ['dave', 'team-x'] },
    }),
  );
  const card = page.one('card');
  assert.equal(page.text(page.one('repo', card)), 'acme/shop #596');
  assert.equal(page.text(page.one('pill', card)), 'Open');
  assert.equal(page.one('title', card).textContent, title);
  assert.equal(page.one('title', card).title, title, 'the tooltip keeps a clamped title readable');
  assert.deepEqual(page.one('meta', card).children.map((node) => node.textContent), ['feat/cart → main', '<i>eve</i>', '+12 −3', '2 files']);
  // Every status row leads with a glyph, so it never reads by colour alone.
  assert.deepEqual(
    page.all('status', card).map((row) => [page.text(page.one('icon', row.children[0])), page.text(page.one('status-line', row))]),
    [['!', '2 commits behind main'], ['✕', 'Has merge conflicts'], ['✕', '1 failed, 1 passedHide checks'], ['✓', 'Approved']],
  );
  assert.deepEqual(texts(page, 'check', card), ['✕<script>build</script> CIFailed', '✓lintPassed']);
  assert.deepEqual(texts(page, 'sub', card), ['bob approved · carol commented', 'Waiting on dave, team-x']);
  assert.deepEqual(page.all('icon', card).map((node) => node.ariaHidden), Array(6).fill('true'));
  for (const node of page.seen()) assert.ok(!['img', 'script', 'b', 'i'].includes(node.tagName), node.tagName);
});

test('loading, failed, merged and closed cards show their heading, the link out and no buttons', () => {
  const page = pageWith(
    bare({ url: `${URL}1`, number: 1, loading: true }),
    bare({ url: `${URL}2`, number: 2, error: 'gh: <not found>' }),
    pull({ url: `${URL}3`, number: 3, state: 'merged', can: ALL }),
    pull({ url: `${URL}4`, number: 4, state: 'closed', can: ALL, draft: true }),
  );
  const [loading, failed, merged, closed] = page.all('card');
  assert.equal(page.text(page.one('card-body', loading)), 'acme/shop #1×Loading…Open on GitHub');
  assert.equal(page.text(page.one('card-body', failed)), 'acme/shop #2×✕gh: <not found>Open on GitHub');
  assert.equal(page.text(page.one('card-body', merged)), 'acme/shop #3Merged×Add the cart pagefeat/cart → mainalice+12 −32 filesOpen on GitHub');
  assert.equal(page.text(page.one('pill', closed)), 'Closed');
  for (const card of [loading, failed, merged, closed]) {
    assert.deepEqual(buttons(page, card), []);
    assert.deepEqual(page.all('statuses', card), []);
    assert.equal(page.control('dismiss', card).ariaLabel, 'Remove from this tab');
    assert.equal(page.text(page.control('open', card)), 'Open on GitHub');
  }
});

test('cards follow the order of the snapshot and leave when their pull request does', () => {
  const second = pull({ url: OTHER, repo: 'acme/api', number: 3 });
  const page = pageWith(pull({}), second);
  assert.deepEqual(texts(page, 'repo'), ['acme/shop #596', 'acme/api #3']);
  page.send(snapshot(second, pull({})));
  assert.deepEqual(texts(page, 'repo'), ['acme/api #3', 'acme/shop #596']);
  page.send(snapshot(second));
  assert.deepEqual(texts(page, 'repo'), ['acme/api #3']);
  page.send(snapshot());
  assert.deepEqual(page.all('card'), []);
  assert.equal(page.all('empty').length, 1);
});

test('Merge asks first: only Confirm merge sends the merge, with the chosen method', async () => {
  const open = pull({ can: { ...CAN, merge: true, update: true }, checks: checks('fail', 'pending', 'pass') });
  const page = pageWith(open);
  assert.deepEqual(buttons(page), [['Merge', true], ['Update branch', true]]);

  page.control('merge').click();
  assert.equal(page.requests.length, 0, 'the first click sends nothing');
  assert.deepEqual(buttons(page), [], 'the confirm step takes the place of the buttons');
  const step = page.control('confirm');
  assert.equal(step.ariaLabel, 'Confirm merge');
  assert.equal(focusedKey(page), 'confirm', 'the step takes the focus, so a held Enter cannot confirm');
  assert.deepEqual(page.one('methods', step).children.map((label) => [page.text(label), label.children[0].checked]), [
    ['Squash and merge', true],
    ['Merge commit', false],
    ['Rebase and merge', false],
  ]);
  assert.equal(page.text(page.one('warning', step)), '!2 checks are not green');
  assert.deepEqual(texts(page, 'button', step), ['Confirm merge', 'Cancel']);

  page.control('method-rebase').click();
  assert.equal(page.requests.length, 0, 'choosing a method sends nothing');
  assert.equal(focusedKey(page), 'method-rebase');
  // A snapshot in between keeps the step open on the chosen method.
  page.send(snapshot({ ...open, title: 'Renamed', checks: checks('fail', 'pass', 'pass') }));
  assert.deepEqual(page.one('methods').children.map((label) => label.children[0].checked), [false, false, true]);
  assert.equal(page.text(page.one('warning')), '!1 check is not green');

  page.control('confirm-merge').click();
  assert.equal(page.requests.length, 1);
  const [request] = page.requests;
  assert.equal(request.url, '/pr/act?t=t%26k');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['Content-Type'], 'application/json');
  assert.deepEqual(request.body, { session: 'chat 1', url: URL, action: 'merge', method: 'rebase' });
  // Until the answer, the card is locked and says what runs.
  assert.equal(page.has('confirm'), false);
  assert.deepEqual(buttons(page), [['Merging…', false], ['Update branch', false]]);
  assert.equal(page.control('dismiss').disabled, true);

  await page.answer({ ok: true });
  assert.deepEqual(buttons(page), [['Merge', true], ['Update branch', true]]);
  assert.deepEqual(page.all('problem'), []);
  assert.equal(page.requests.length, 1);
});

test('Cancel closes the confirm step, sends nothing and gives the focus back to Merge', () => {
  const page = pageWith(pull({}));
  page.control('merge').click();
  page.control('method-merge').click();
  page.control('cancel').click();
  assert.equal(page.requests.length, 0);
  assert.equal(page.has('confirm'), false);
  assert.deepEqual(buttons(page), [['Merge', true]]);
  assert.equal(focusedKey(page), 'merge');
  // Opened again, the step remembers the method.
  page.control('merge').click();
  assert.deepEqual(page.one('methods').children.map((label) => label.children[0].checked), [false, true, false]);
});

test('a repository with one merge method gets no chooser, and that method is the one sent', () => {
  const page = pageWith(pull({ methods: ['squash'] }));
  page.control('merge').click();
  const step = page.control('confirm');
  assert.deepEqual(page.all('methods', step), []);
  assert.deepEqual(texts(page, 'sub', step), ['Squash and merge'], 'it still says how it will merge');
  assert.deepEqual(page.all('warning', step), []);
  page.control('confirm-merge').click();
  assert.deepEqual(bodies(page), [{ session: 'chat 1', url: URL, action: 'merge', method: 'squash' }]);
});

test('the confirm step closes when a snapshot says the pull request can no longer be merged', () => {
  const page = pageWith(pull({}));
  page.control('merge').click();
  assert.equal(page.has('confirm-merge'), true);

  page.send(snapshot(pull({ can: CAN, why: { merge: 'Needs 1 approving review' } })));
  assert.equal(page.has('confirm-merge'), false);
  assert.deepEqual(buttons(page), [['Merge', false]]);
  const reason = page.one('reason');
  assert.equal(page.text(reason), 'Needs 1 approving review');
  assert.equal(page.control('merge').attributes['aria-describedby'], reason.id);
  assert.equal(focusedKey(page), undefined, 'the focus falls back to the card, not to the top of the page');
  assert.equal(page.document.activeElement, page.one('card'));
  page.control('merge').click();
  assert.equal(page.has('confirm-merge'), false, 'a Merge that is off does nothing');

  page.send(snapshot(pull({})));
  assert.deepEqual(buttons(page), [['Merge', true]]);
  assert.equal(page.has('confirm-merge'), false, 'it does not open again by itself');
  assert.deepEqual(page.all('reason'), []);
  assert.equal(page.requests.length, 0);
});

test('the other buttons come from `can` and each sends its own action', async () => {
  const page = pageWith(pull({ can: ALL }));
  assert.deepEqual(buttons(page), [['Merge', true], ['Update branch', true], ['Mark ready', true], ['Convert to draft', true], ['Re-run failed checks', true]]);
  const running = { update: 'Updating…', ready: 'Marking ready…', draft: 'Converting…', rerun: 'Re-running…' };
  for (const [action, label] of Object.entries(running)) {
    page.control(action).click();
    assert.deepEqual(page.requests.at(-1).body, { session: 'chat 1', url: URL, action });
    const shown = buttons(page);
    assert.deepEqual(shown.map(([, enabled]) => enabled), [false, false, false, false, false], action);
    assert.equal(shown[ACTIONS.indexOf(action)][0], label);
    page.control('merge').click();
    page.control(action).click();
    assert.equal(page.requests.length, ACTIONS.indexOf(action), 'a locked card sends nothing more');
    await page.answer({ ok: true });
    assert.deepEqual(buttons(page).map(([, enabled]) => enabled), [true, true, true, true, true], action);
  }
  page.send(snapshot(pull({ can: { ...CAN, rerun: true } })));
  assert.deepEqual(buttons(page), [['Merge', false], ['Re-run failed checks', true]]);
});

test('an action the server reports as running locks the card too', () => {
  const page = pageWith(pull({ can: CAN, busy: 'update' }), pull({ url: OTHER, number: 3, can: ALL }));
  const [busy, idle] = page.all('card');
  assert.deepEqual(buttons(page, busy), [['Merge', false], ['Updating…', false]]);
  assert.equal(page.control('dismiss', busy).disabled, false, 'removing it from the tab is the server\'s to refuse');
  assert.deepEqual(buttons(page, idle).map(([, enabled]) => enabled), [true, true, true, true, true], 'the other card is not locked');
});

test('an error answer shows on its card until the next action works or the reader hides it', async () => {
  const page = pageWith(pull({ can: ALL }), pull({ url: OTHER, number: 3, can: ALL }));
  const [card, other] = page.all('card');
  assert.deepEqual(page.all('problem'), []);

  page.control('update', card).click();
  await page.answer({ error: 'The branch is <protected>' });
  const problem = page.one('problem', card);
  assert.equal(page.text(page.one('problem-text', problem)), 'The branch is <protected>');
  assert.equal(page.one('problem-text', problem).ariaLive, 'polite');
  assert.deepEqual(page.all('problem', other), [], 'only the card it belongs to');
  assert.deepEqual(buttons(page, card).map(([, enabled]) => enabled), [true, true, true, true, true], 'the card is unlocked again');

  // It stays over snapshots and while the next try is out, and goes when that one works.
  page.send(snapshot(pull({ can: ALL, title: 'Renamed' }), pull({ url: OTHER, number: 3, can: ALL })));
  assert.equal(page.one('problem', card), problem, 'the same element, so a screen reader hears only changes');
  page.control('update', card).click();
  assert.equal(page.text(page.one('problem-text', card)), 'The branch is <protected>');
  await page.answer({ ok: true });
  assert.deepEqual(page.all('problem'), []);

  page.control('rerun', card).click();
  await page.answer({ error: 'Nothing to re-run' });
  assert.equal(page.text(page.one('problem-text', card)), 'Nothing to re-run');
  const hide = page.one('close', page.one('problem', card));
  assert.equal(hide.ariaLabel, 'Hide this error');
  hide.click();
  assert.deepEqual(page.all('problem'), []);
  assert.equal(page.requests.length, 3, 'hiding sends nothing');
});

test('an answer that is not a 200 with JSON, or no answer at all, shows a short generic line', async () => {
  const page = pageWith(pull({ can: ALL }));
  const generic = "That didn't work. Try again.";
  page.control('ready').click();
  await page.answer({ error: 'Internal detail' }, { status: 500 });
  assert.equal(page.text(page.one('problem-text')), generic);
  assert.deepEqual(buttons(page).map(([, enabled]) => enabled), [true, true, true, true, true]);

  page.control('ready').click();
  await page.answer({ ok: true });
  assert.deepEqual(page.all('problem'), []);

  page.control('draft').click();
  await page.fail();
  assert.equal(page.text(page.one('problem-text')), generic);
  assert.deepEqual(buttons(page).map(([, enabled]) => enabled), [true, true, true, true, true], 'a lost request unlocks the card');
});

test('outside links open through the bridge and never lock the card', async () => {
  const link = 'https://github.com/acme/shop/actions/runs/1/job/2';
  const list = [
    { name: 'build', workflow: 'CI', status: 'fail', url: link },
    { name: 'lint', workflow: null, status: 'fail', url: null },
  ];
  const page = pageWith(pull({ can: ALL, checks: list }));
  const [linked, plain] = page.all('check');
  assert.equal(linked.tagName, 'button');
  assert.equal(plain.tagName, 'div', 'a check without a link is not a control');
  assert.equal(plain.dataset.key, undefined);

  linked.click();
  page.control('open').click();
  assert.deepEqual(bodies(page), [
    { session: 'chat 1', url: URL, action: 'open', target: link },
    { session: 'chat 1', url: URL, action: 'open' },
  ]);
  assert.deepEqual(buttons(page).map(([, enabled]) => enabled), [true, true, true, true, true]);
  assert.equal(page.control('dismiss').disabled, false);
  await page.answer({ ok: true });
  await page.answer({ error: 'Could not open the link' });
  assert.equal(page.text(page.one('problem-text')), 'Could not open the link');
  // A link that opens leaves that line alone: it changed nothing on the pull request.
  page.control('open').click();
  await page.answer({ ok: true });
  assert.equal(page.text(page.one('problem-text')), 'Could not open the link');
});

test('× asks the bridge to remove the pull request, and a late answer for a card that left is ignored', async () => {
  const page = pageWith(pull({ can: ALL }), bare({ url: OTHER, number: 3, error: 'gh failed' }));
  const [card, failed] = page.all('card');
  page.control('dismiss', card).click();
  assert.deepEqual(bodies(page), [{ session: 'chat 1', url: URL, action: 'dismiss' }]);
  assert.deepEqual(buttons(page, card), [['Merge', false], ['Update branch', false], ['Mark ready', false], ['Convert to draft', false], ['Re-run failed checks', false]]);
  assert.equal(page.control('dismiss', card).disabled, true);
  assert.equal(page.control('dismiss', failed).disabled, false);

  page.send(snapshot(bare({ url: OTHER, number: 3, error: 'gh failed' })));
  assert.deepEqual(texts(page, 'repo'), ['acme/shop #3']);
  await page.answer({ error: 'Already removed' });
  assert.deepEqual(page.all('problem'), []);

  // A failed remove shows on the card that stays.
  page.control('dismiss').click();
  await page.answer({ error: 'Could not remove it' });
  assert.equal(page.text(page.one('problem-text')), 'Could not remove it');
  assert.equal(page.control('dismiss').disabled, false);
});

test('Refresh asks the bridge once at a time and shows a failure under the top bar', async () => {
  const page = pageWith(pull({}));
  const refresh = page.seen(page.one('toolbar')).find((node) => node.tagName === 'button');
  assert.equal(page.text(refresh), 'Refresh');
  refresh.click();
  assert.deepEqual(bodies(page), [{ session: 'chat 1', action: 'refresh' }]);
  assert.equal(page.text(refresh), 'Refreshing…');
  assert.equal(refresh.ariaDisabled, 'true');
  assert.equal(refresh.disabled, false, 'it keeps the keyboard focus while the request is out');
  refresh.click();
  assert.equal(page.requests.length, 1);

  await page.answer({ ok: true });
  assert.equal(page.text(refresh), 'Refresh');
  assert.equal(refresh.ariaDisabled, 'false');
  assert.deepEqual(page.all('problem'), []);

  refresh.click();
  await page.answer({ error: 'gh is not signed in' });
  const problem = page.one('problem');
  assert.equal(page.one('card').contains(problem), false, 'it belongs to no card');
  assert.equal(page.text(page.one('problem-text', problem)), 'gh is not signed in');
  refresh.click();
  await page.answer({ ok: true });
  assert.deepEqual(page.all('problem'), []);
  refresh.click();
  await page.fail();
  page.one('close', page.one('problem')).click();
  assert.deepEqual(page.all('problem'), []);
});

test('the notice of a snapshot shows as one line, and a broken stream says so until the next snapshot', () => {
  const page = openPage();
  const banners = () => texts(page, 'banner');
  assert.deepEqual(banners(), []);
  page.send({ pulls: [], notice: 'gh is not installed <on this Mac>' });
  assert.deepEqual(banners(), ['gh is not installed <on this Mac>']);
  assert.equal(page.one('notice').ariaLive, 'polite');
  page.send(snapshot());
  assert.deepEqual(banners(), []);

  page.source.readyState = 0;
  page.source.onerror();
  assert.deepEqual(banners(), ['Reconnecting…']);
  page.source.readyState = 2;
  page.source.onerror();
  assert.deepEqual(banners(), ['Disconnected. Type /pr in the chat to open this page again.']);
  page.send({ pulls: [pull({})], notice: 'Showing the 20 newest' });
  assert.deepEqual(banners(), ['Showing the 20 newest']);
});

test('a card is drawn again only when it changed, and then the keyboard focus stays on its control', () => {
  const open = pull({ can: ALL, checks: checks('pass', 'pass') });
  const second = pull({ url: OTHER, number: 3 });
  const page = pageWith(open, second);
  const [card, other] = page.all('card');
  const title = page.one('title', card);
  const otherTitle = page.one('title', other);

  page.send(snapshot(structuredClone(open), structuredClone(second)));
  assert.equal(page.one('title', card), title, 'the same snapshot again leaves the elements, and a selection in them, alone');

  page.control('checks', card).click();
  assert.deepEqual(texts(page, 'check', card), ['✓check-0Passed', '✓check-1Passed']);
  assert.equal(page.control('checks', card).ariaExpanded, 'true');
  assert.equal(focusedKey(page), 'checks');
  assert.equal(page.one('title', other), otherTitle, 'only the card that changed is drawn again');
  assert.deepEqual(page.all('card'), [card, other], 'the cards themselves stay');

  // A snapshot that changes the card keeps the list open and the focus on its toggle.
  page.send(snapshot({ ...open, behind: 4 }, second));
  assert.notEqual(page.one('title', card), title);
  assert.equal(page.text(page.control('checks', card)), 'Hide checks');
  assert.equal(focusedKey(page), 'checks');
  assert.equal(page.document.activeElement, page.control('checks', card));
  page.control('checks', card).click();
  assert.deepEqual(page.all('check', card), []);
  assert.equal(page.control('checks', card).ariaExpanded, 'false');

  // When the focused control goes away, the focus stays in the card instead of jumping to the top.
  page.control('update', card).focus();
  page.send(snapshot({ ...open, can: { ...ALL, update: false } }, second));
  assert.equal(page.document.activeElement, card);
  assert.equal(page.requests.length, 0);
});

test('the page keeps to the bridge\'s content policy: no HTML parsing, no inline styles, nothing from outside', () => {
  const read = (name) => fs.readFileSync(path.join(PAGE, name), 'utf8');
  const banned = {
    'pr.js': [/innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser/, /\beval\(|new Function/, /\.style\b|style=|cssText/, /https?:\/\//, /location\.|window\.open|\.href\b/],
    'pr.css': [/url\(/, /@import/, /https?:\/\//],
  };
  banned['pr-view.js'] = banned['pr.js'];
  for (const [name, patterns] of Object.entries(banned)) {
    // Three dots after a word are a text that should end in the … character.
    for (const pattern of [...patterns, /\w\.\.\.(\W|$)/m]) assert.equal(pattern.test(read(name)), false, `${name} has ${pattern}`);
  }
});
