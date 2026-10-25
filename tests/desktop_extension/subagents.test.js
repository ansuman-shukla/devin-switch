'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { SubagentStatus, elapsed, listSubagents, pickItems, statusText, stepItems, tooltip } = require('../../src/devin_switch/desktop_extension/subagents');

const NOW = 10_000;
const ticker = {
  id: 'a1',
  title: 'Sleep ticker',
  profile: 'subagent_general',
  background: true,
  started_at: NOW - 330,
  status: 'running',
  last_step: 'tool: Sun Oct 25 10:48:36 IST 2026',
  recent: 'assistant: called exec {"command": "sleep 20 && date"}\ntool: Sun Oct 25 10:48:36 IST 2026',
  project: '/Users/me/work/devin-switch',
  chat: 'Fix subagent cards',
};
const explorer = { ...ticker, id: 'b2', title: '', profile: 'subagent_explore', background: false, started_at: NOW - 3_900, last_step: '', recent: '', chat: '' };

/** Just enough of the vscode API for a status bar item and quick picks, recording what the badge does. */
function fakeVscode({ focused = true, picks = [] } = {}) {
  const calls = { picks: [], messages: [] };
  const item = { shown: false, disposed: false, show() { this.shown = true; }, hide() { this.shown = false; }, dispose() { this.disposed = true; } };
  const vscode = {
    StatusBarAlignment: { Left: 1, Right: 2 },
    window: {
      state: { focused },
      createStatusBarItem(alignment, priority) {
        Object.assign(item, { alignment, priority });
        return item;
      },
      async showQuickPick(items, options) {
        calls.picks.push({ items, options });
        const choice = picks.shift();
        return choice === undefined ? undefined : items[choice];
      },
      showInformationMessage(message) {
        calls.messages.push(message);
      },
    },
  };
  return { vscode, item, calls };
}

test('elapsed time reads in whole minutes and hours', () => {
  assert.equal(elapsed(NOW - 59, NOW), 'under 1m');
  assert.equal(elapsed(NOW - 330, NOW), '5m');
  assert.equal(elapsed(NOW - 3_900, NOW), '1h 5m');
  assert.equal(elapsed(NOW + 30, NOW), 'under 1m');
});

test('the badge counts running subagents and its tooltip says what each is doing', () => {
  assert.equal(statusText([ticker]), '$(sync~spin) 1 subagent');
  assert.equal(statusText([ticker, explorer]), '$(sync~spin) 2 subagents');
  const text = tooltip([ticker, explorer], NOW);
  assert.match(text, /Sleep ticker \(background\) · running 5m · devin-switch · Fix subagent cards\n {4}tool: Sun Oct 25 10:48:36 IST 2026/);
  assert.match(text, /subagent_explore · running 1h 5m · devin-switch(\n|$)/);
});

test('the list names each subagent with its chat, time and latest step', () => {
  const [first, second] = pickItems([ticker, explorer], NOW);
  assert.deepEqual(
    { label: first.label, description: first.description, detail: first.detail },
    { label: '$(sync~spin) Sleep ticker', description: 'devin-switch · Fix subagent cards · running 5m · background', detail: 'tool: Sun Oct 25 10:48:36 IST 2026' },
  );
  assert.equal(first.agent, ticker);
  assert.deepEqual([second.label, second.description, second.detail], ['$(sync~spin) subagent_explore', 'devin-switch · running 1h 5m', 'No saved steps yet']);
  assert.deepEqual(stepItems(ticker).map((i) => i.label), ['assistant: called exec {"command": "sleep 20 && date"}', 'tool: Sun Oct 25 10:48:36 IST 2026']);
  assert.deepEqual(stepItems(explorer), [{ label: 'No saved steps yet' }]);
});

test('listing asks ds for exactly this window and surfaces failures', async () => {
  const seen = [];
  const run = (file, args, options, done) => {
    seen.push([file, args, options.timeout]);
    done(null, JSON.stringify({ subagents: [ticker] }));
  };
  assert.deepEqual(await listSubagents(['/usr/bin/env', 'ds'], 4242, run), [ticker]);
  assert.deepEqual(seen, [['/usr/bin/env', ['ds', 'subagents', '--window', '4242'], 15_000]]);
  await assert.rejects(listSubagents(['ds'], 1, (f, a, o, done) => done(new Error('ds missing'))), /ds missing/);
  await assert.rejects(listSubagents(['ds'], 1, (f, a, o, done) => done(null, 'not json')), SyntaxError);
});

test('the badge appears only while this window has running subagents', async () => {
  let result = [ticker, explorer];
  const { vscode, item } = fakeVscode();
  const status = new SubagentStatus(vscode, async () => result, () => NOW);
  assert.deepEqual([item.alignment, item.priority, item.command, item.shown], [2, 91, 'switchPanels.showSubagents', false]);
  await status.refresh();
  assert.equal(item.shown, true);
  assert.equal(item.text, '$(sync~spin) 2 subagents');
  assert.match(item.tooltip, /Sleep ticker/);
  result = [];
  await status.refresh();
  assert.equal(item.shown, false);
  result = [ticker];
  await status.refresh();
  assert.equal(item.text, '$(sync~spin) 1 subagent');
  status.list = async () => {
    throw new Error('ds failed');
  };
  await status.refresh();
  assert.equal(item.shown, false, 'A failing ds must hide the badge, not keep stale counts');
  status.dispose();
  assert.equal(item.disposed, true);
});

test('an unfocused window does not poll ds', async () => {
  let calls = 0;
  const { vscode, item } = fakeVscode({ focused: false });
  const status = new SubagentStatus(vscode, async () => {
    calls += 1;
    return [ticker];
  });
  await status.refresh();
  assert.equal(calls, 0);
  assert.equal(item.shown, false);
  vscode.window.state.focused = true;
  await status.refresh();
  assert.equal(calls, 1);
});

test('overlapping refreshes call ds once', async () => {
  let calls = 0;
  let release;
  const { vscode } = fakeVscode();
  const status = new SubagentStatus(vscode, () => {
    calls += 1;
    return new Promise((resolve) => {
      release = () => resolve([ticker]);
    });
  });
  const first = status.refresh();
  await status.refresh();
  release();
  await first;
  assert.equal(calls, 1);
});

test('clicking the badge lists subagents, then shows the chosen one\'s latest steps', async () => {
  const { vscode, calls } = fakeVscode({ picks: [0] });
  const status = new SubagentStatus(vscode, async () => [ticker, explorer], () => NOW);
  await status.show();
  assert.equal(calls.picks.length, 2);
  assert.deepEqual(calls.picks[0].items.map((i) => i.label), ['$(sync~spin) Sleep ticker', '$(sync~spin) subagent_explore']);
  assert.equal(calls.picks[0].options.placeHolder, 'Subagents running in this window');
  assert.deepEqual(calls.picks[1].items.map((i) => i.label), stepItems(ticker).map((i) => i.label));
  assert.equal(calls.picks[1].options.placeHolder, 'Sleep ticker · latest steps, newest last');
});

test('dismissing the list or having nothing running opens nothing else', async () => {
  const dismissed = fakeVscode({ picks: [] });
  await new SubagentStatus(dismissed.vscode, async () => [ticker]).show();
  assert.equal(dismissed.calls.picks.length, 1);
  const idle = fakeVscode();
  await new SubagentStatus(idle.vscode, async () => []).show();
  assert.deepEqual(idle.calls.picks, []);
  assert.deepEqual(idle.calls.messages, ['No subagents are running in this window.']);
});
