'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const view = require('../../src/devin_switch/subagent_page/subagents-view');

const row = (fields) => ({ id: 'a', title: 'Fix the flaky test', profile: 'General', background: true, status: 'running', started: 1000, ended: null, step: '', note: '', ...fields });
const ids = (rows) => view.ordered(rows).map(({ id }) => id);

test('durations read as seconds, then minutes, then hours, and never go negative', () => {
  const cases = [
    [0, '0s'],
    [0.4, '0s'],
    [12, '12s'],
    [59, '59s'],
    [59.9, '59s'],
    [60, '1m 00s'],
    [61, '1m 01s'],
    [125, '2m 05s'],
    [252, '4m 12s'],
    [3599, '59m 59s'],
    [3599.9, '59m 59s'],
    [3600, '1h 00m'],
    [3659, '1h 00m'],
    [3840, '1h 04m'],
    [7199, '1h 59m'],
    [90_000, '25h 00m'],
    [-1, '0s'],
    [-3600, '0s'],
    [NaN, '0s'],
    [Infinity, '0s'],
    [undefined, '0s'],
    [null, '0s'],
    ['90', '0s'],
  ];
  for (const [seconds, text] of cases) assert.equal(view.formatDuration(seconds), text, `formatDuration(${seconds})`);
});

test('active rows count up from their start and finished rows show the whole run', () => {
  assert.equal(view.timeLabel(row({ status: 'running' }), 1252), 'running 4m 12s');
  assert.equal(view.timeLabel(row({ status: 'running' }), 1253.9), 'running 4m 13s');
  assert.equal(view.timeLabel(row({ status: 'paused' }), 1252), 'paused 4m 12s');
  assert.equal(view.timeLabel(row({ status: 'done', ended: 1125 }), 99_999), 'done in 2m 05s');
  assert.equal(view.timeLabel(row({ status: 'failed', ended: 1012 }), 99_999), 'failed after 12s');
  assert.equal(view.timeLabel(row({ status: 'stopped', ended: 1063 }), 99_999), 'stopped after 1m 03s');
  // An active row counts to now even if the server left an end time on it.
  assert.equal(view.timeLabel(row({ status: 'paused', ended: 1005 }), 1090), 'paused 1m 30s');
});

test('time labels clamp a clock that runs backwards and stay empty without the times', () => {
  assert.equal(view.timeLabel(row({ status: 'running' }), 990), 'running 0s');
  assert.equal(view.timeLabel(row({ status: 'done', ended: 900 }), 2000), 'done in 0s');
  assert.equal(view.timeLabel(row({ status: 'running', started: NaN }), 2000), '');
  assert.equal(view.timeLabel(row({ status: 'running', started: undefined }), 2000), '');
  assert.equal(view.timeLabel(row({ status: 'running' }), NaN), '');
  assert.equal(view.timeLabel(row({ status: 'done', ended: null }), 2000), '');
  assert.equal(view.timeLabel(row({ status: 'failed', started: null, ended: 1500 }), 2000), '');
  assert.equal(view.timeLabel(row({ status: 'stopped', ended: NaN }), 2000), '');
});

test('active rows come first, newest start first, then finished rows, latest end first', () => {
  const rows = [
    row({ id: 'done-early', status: 'done', started: 100, ended: 150 }),
    row({ id: 'run-old', status: 'running', started: 200 }),
    row({ id: 'failed-late', status: 'failed', started: 300, ended: 900 }),
    row({ id: 'paused-mid', status: 'paused', started: 400 }),
    row({ id: 'stopped-mid', status: 'stopped', started: 500, ended: 600 }),
    row({ id: 'run-new', status: 'running', started: 700 }),
  ];
  const before = JSON.stringify(rows);
  assert.deepEqual(ids(rows), ['run-new', 'paused-mid', 'run-old', 'failed-late', 'stopped-mid', 'done-early']);
  assert.equal(JSON.stringify(rows), before, 'the snapshot is not reordered in place');
  assert.deepEqual(ids([]), []);
  // A finished row is placed by its end, not its start; an active one by its start, not a stray end.
  assert.deepEqual(
    ids([row({ id: 'long', status: 'done', started: 10, ended: 500 }), row({ id: 'short', status: 'done', started: 400, ended: 450 })]),
    ['long', 'short'],
  );
  assert.deepEqual(
    ids([row({ id: 'old', status: 'running', started: 10, ended: 999 }), row({ id: 'new', status: 'running', started: 20 })]),
    ['new', 'old'],
  );
});

test('ties put the later snapshot row on top and unknown times sink within their group', () => {
  assert.deepEqual(
    ids([
      row({ id: 'r1', started: 50 }),
      row({ id: 'd1', status: 'done', started: 10, ended: 80 }),
      row({ id: 'r2', status: 'paused', started: 50 }),
      row({ id: 'd2', status: 'stopped', started: 20, ended: 80 }),
      row({ id: 'r3', started: 50 }),
    ]),
    ['r3', 'r2', 'r1', 'd2', 'd1'],
  );
  // A finished row without an end time is placed by its start; rows without any time go last.
  assert.deepEqual(
    ids([
      row({ id: 'no-times', status: 'done', started: NaN, ended: null }),
      row({ id: 'no-end', status: 'failed', started: 300, ended: null }),
      row({ id: 'ended-200', status: 'done', started: 100, ended: 200 }),
      row({ id: 'ended-400', status: 'stopped', started: 100, ended: 400 }),
      row({ id: 'no-times-2', status: 'stopped', started: undefined, ended: undefined }),
      row({ id: 'run-no-start', status: 'running', started: null }),
      row({ id: 'run', status: 'running', started: 5 }),
    ]),
    ['run', 'run-no-start', 'ended-400', 'no-end', 'ended-200', 'no-times-2', 'no-times'],
  );
});

test('the count line names each status that has rows, in a fixed order', () => {
  const rows = (...statuses) => statuses.map((status, index) => row({ id: String(index), status }));
  assert.equal(view.countLine(rows('done', 'running', 'running', 'running')), '3 running · 1 done');
  assert.equal(view.countLine(rows('stopped', 'failed', 'done', 'paused', 'running', 'failed')), '1 running · 1 paused · 1 done · 2 failed · 1 stopped');
  assert.equal(view.countLine(rows('paused', 'paused')), '2 paused');
  assert.equal(view.countLine(rows('failed')), '1 failed');
  assert.equal(view.countLine([]), '');
});

test('only running and paused rows are active', () => {
  assert.deepEqual(
    ['running', 'paused', 'done', 'failed', 'stopped'].map((status) => view.isActive(row({ status }))),
    [true, true, false, false, false],
  );
});

test('a row without a title is named after its profile, and then the profile is not repeated', () => {
  assert.equal(view.titleOf(row({})), 'Fix the flaky test');
  assert.equal(view.titleOf(row({ title: '  Padded  ' })), 'Padded');
  assert.equal(view.titleOf(row({ title: '', profile: 'Explore' })), 'Explore');
  assert.equal(view.titleOf(row({ title: '   ', profile: 'Explore' })), 'Explore');
  assert.equal(view.titleOf(row({ title: '', profile: '' })), 'Subagent');
  assert.equal(view.titleOf(row({ title: ' ', profile: ' ' })), 'Subagent');

  assert.equal(view.metaOf(row({})), 'General · background');
  assert.equal(view.metaOf(row({ profile: 'Explore', background: false })), 'Explore · foreground');
  assert.equal(view.metaOf(row({ profile: '' })), 'background');
  assert.equal(view.metaOf(row({ title: '', profile: 'Explore' })), 'background');
  assert.equal(view.metaOf(row({ title: '', profile: '', background: false })), 'foreground');
});

test('active rows show their latest step and only failed or stopped rows show their note', () => {
  const texts = { step: 'Running command', note: 'the agent restarted' };
  assert.equal(view.detailOf(row({ ...texts, status: 'running' })), 'Running command');
  assert.equal(view.detailOf(row({ ...texts, status: 'paused' })), 'Running command');
  assert.equal(view.detailOf(row({ ...texts, status: 'failed' })), 'the agent restarted');
  assert.equal(view.detailOf(row({ ...texts, status: 'stopped' })), 'the agent restarted');
  assert.equal(view.detailOf(row({ ...texts, status: 'done' })), '');
  assert.equal(view.detailOf(row({ status: 'running', note: 'stale note' })), '');
  assert.equal(view.detailOf(row({ status: 'failed', step: 'Ran sleep' })), '');
  assert.equal(view.detailOf(row({ status: 'running', step: '  ' })), '');
});

test('only a positive whole reply_chars is a reply', () => {
  for (const chars of [1, 2, 999, 15_639, 50_000]) {
    assert.equal(view.hasReply(row({ reply_chars: chars })), true, `hasReply(${chars})`);
    assert.equal(view.replyChars(row({ reply_chars: chars })), chars, `replyChars(${chars})`);
  }
  const fresh = { open: true, asked: null, waiting: false };
  // A row without the field, as an older bridge sends it.
  assert.equal(view.hasReply(row({})), false);
  assert.equal(view.replyChars(row({})), 0);
  assert.equal(view.replySize(row({})), '');
  assert.equal(view.needsReply(row({}), fresh), false);
  for (const chars of [0, -1, -500, NaN, Infinity, 2.5, '12', '', null, undefined, true, [3], {}]) {
    const odd = row({ reply_chars: chars });
    assert.equal(view.hasReply(odd), false, `hasReply(${String(chars)})`);
    assert.equal(view.replyChars(odd), 0, `replyChars(${String(chars)})`);
    assert.equal(view.replySize(odd), '', `replySize(${String(chars)})`);
    assert.equal(view.needsReply(odd, fresh), false, `needsReply(${String(chars)})`);
  }
});

test('the reply toggle says show or hide, and "so far" while the row is still active', () => {
  const cases = [
    ['running', false, 'Show reply so far'],
    ['running', true, 'Hide reply so far'],
    ['paused', false, 'Show reply so far'],
    ['paused', true, 'Hide reply so far'],
    ['done', false, 'Show reply'],
    ['done', true, 'Hide reply'],
    ['failed', false, 'Show reply'],
    ['failed', true, 'Hide reply'],
    ['stopped', false, 'Show reply'],
    ['stopped', true, 'Hide reply'],
  ];
  for (const [status, expanded, text] of cases) assert.equal(view.replyLabel(row({ status, reply_chars: 3712 }), expanded), text, `${status}, expanded ${expanded}`);
});

test('the size hint counts characters, and thousands to one decimal from 1,000 on', () => {
  const cases = [
    [0, ''],
    [1, '1 character'],
    [2, '2 characters'],
    [999, '999 characters'],
    [1000, '1k characters'],
    [1049, '1k characters'],
    [1050, '1.1k characters'],
    [3712, '3.7k characters'],
    [9949, '9.9k characters'],
    [9950, '10k characters'],
    [15_639, '15.6k characters'],
    [50_000, '50k characters'],
  ];
  for (const [chars, text] of cases) assert.equal(view.replySize(row({ reply_chars: chars })), text, `replySize(${chars})`);
  // The hint is about the text, not the row's state.
  assert.equal(view.replySize(row({ status: 'done', reply_chars: 3712 })), '3.7k characters');
});

test('an expanded row asks for its reply, then again only when reply_chars has moved on', () => {
  const needs = (chars, state) => view.needsReply(row({ reply_chars: chars }), { open: true, asked: null, waiting: false, ...state });
  assert.equal(needs(120, {}), true, 'just expanded: nothing was asked yet');
  assert.equal(needs(120, { asked: 120 }), false, 'the same count again');
  assert.equal(needs(300, { asked: 120 }), true, 'it grew');
  assert.equal(needs(40, { asked: 120 }), true, 'a different count, even a smaller one');
  // A finished row's reply is asked for the same way.
  assert.equal(view.needsReply(row({ status: 'done', ended: 1100, reply_chars: 120 }), { open: true, asked: null, waiting: false }), true);
  // A collapsed row asks for nothing, whatever changed.
  assert.equal(needs(120, { open: false }), false);
  assert.equal(needs(300, { open: false, asked: 120 }), false);
});

test('a row has one request at a time, and a change during it is asked for once it returns', () => {
  const needs = (chars, state) => view.needsReply(row({ reply_chars: chars }), state);
  const flying = { open: true, asked: 120, waiting: true };
  assert.equal(needs(120, flying), false);
  assert.equal(needs(300, flying), false, 'changed while the request is in flight');
  assert.equal(needs(900, flying), false, 'changed again while it is in flight');
  const back = { ...flying, waiting: false };
  assert.equal(needs(900, back), true, 'the request returned and the count had moved on');
  assert.equal(needs(120, back), false, 'the request returned and nothing had changed');

  // The page's loop: every snapshot and every returned request asks this question.
  const state = { open: true, asked: null, waiting: false };
  const requests = [];
  const see = (chars) => {
    if (!needs(chars, state)) return;
    Object.assign(state, { asked: chars, waiting: true });
    requests.push(chars);
  };
  const returned = (chars) => {
    state.waiting = false;
    see(chars);
  };
  see(100);
  see(200);
  see(300);
  assert.deepEqual(requests, [100]);
  returned(300);
  assert.deepEqual(requests, [100, 300]);
  see(300);
  returned(300);
  see(300);
  assert.deepEqual(requests, [100, 300], 'the latest count was already asked for');
  see(301);
  assert.deepEqual(requests, [100, 300, 301]);
});
