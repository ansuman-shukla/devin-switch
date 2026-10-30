'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { Helper } = require('../../src/devin_switch/desktop_extension/helper');

// Stands in for `ds side view`: answers by op, streaming and pushing like the real helper.
const FAKE = `
const lines = require('node:readline').createInterface({ input: process.stdin });
const say = (m) => process.stdout.write(JSON.stringify(m) + '\\n');
lines.on('line', (line) => {
  const { id, op, ...fields } = JSON.parse(line);
  if (op === 'stream') {
    say({ event: 'ask', text: 'pushed before the reply' });
    say({ id, event: 'update', n: 1 });
    process.stdout.write(JSON.stringify({ id, event: 'update', n: 2 }) + '\\n' + 'not json\\n');
    say({ id, ok: true, stopReason: 'end_turn', echo: fields });
  } else if (op === 'fail') say({ id, ok: false, error: 'No Switch chat is open' });
  else if (op === 'exit') process.exit(0);
});
lines.on('close', () => process.exit(0));
`;

/** A fake helper that is shut down after the test, even when an assertion fails. */
function fakeHelper(t) {
  const folder = mkdtempSync(path.join(tmpdir(), 'helper-'));
  const script = path.join(folder, 'fake-ds.js');
  writeFileSync(script, FAKE);
  const helper = new Helper([process.execPath, script], ['side', 'view']);
  t.after(() => {
    for (const call of helper.pending.values()) clearTimeout(call.timer);
    helper.process?.kill();
  });
  return helper;
}

test('routes streamed events to the asking request and pushes to listeners', { timeout: 10_000 }, async (t) => {
  const helper = fakeHelper(t);
  const pushed = [];
  const updates = [];
  const subscription = helper.onEvent((event) => pushed.push(event));
  const reply = await helper.ask('stream', { sessionId: 's1' }, { onEvent: (e) => updates.push(e.n), timeoutMs: 0 });
  assert.deepEqual(reply, { id: 1, ok: true, stopReason: 'end_turn', echo: { sessionId: 's1' } });
  assert.deepEqual(updates, [1, 2]);
  assert.deepEqual(pushed, [{ event: 'ask', text: 'pushed before the reply' }]);
  subscription.dispose();
  await helper.ask('stream');
  assert.equal(pushed.length, 1);
  helper.dispose();
});

test('rejects failed requests with the helper error and every pending one when it exits', { timeout: 10_000 }, async (t) => {
  const helper = fakeHelper(t);
  await assert.rejects(helper.ask('fail'), { message: 'No Switch chat is open' });
  const waiting = helper.ask('never-answered', {}, { timeoutMs: 0 });
  const exiting = helper.ask('exit');
  await assert.rejects(waiting, { message: 'The ds side helper exited.' });
  await assert.rejects(exiting, { message: 'The ds side helper exited.' });
  // The next request starts a fresh helper.
  assert.equal((await helper.ask('stream')).ok, true);
  helper.dispose();
});

test('times out unanswered requests unless asked to wait', { timeout: 10_000 }, async (t) => {
  const helper = fakeHelper(t);
  await assert.rejects(helper.ask('never-answered', {}, { timeoutMs: 50 }), { message: 'ds side never-answered timed out.' });
  assert.equal(helper.pending.size, 0);
  helper.dispose();
});

test('a disposed helper still settles the requests it was answering', { timeout: 10_000 }, async (t) => {
  const helper = fakeHelper(t);
  const waiting = helper.ask('never-answered', {}, { timeoutMs: 0 });
  helper.dispose();
  // Asked before the old helper finished exiting: its exit must not fail the new helper's work.
  const next = helper.ask('never-answered', {}, { timeoutMs: 500 });
  await assert.rejects(waiting, { message: 'The ds side helper exited.' });
  await assert.rejects(next, { message: 'ds side never-answered timed out.' });
});
