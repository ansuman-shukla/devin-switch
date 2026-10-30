'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const thread = require('../../src/devin_switch/side_page/side-thread');
const { renderMarkdown } = require('../../src/devin_switch/side_page/markdown');

const thought = (text) => ({ sessionUpdate: 'agent_thought_chunk', content: { type: 'text', text } });
const message = (text) => ({ sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } });

test('updates stream the thoughts, the tool steps and the answer apart', () => {
  let turn = thread.newTurn('q');
  assert.deepEqual(turn, { user: 'q', assistant: '', status: 'answering', thoughts: '', steps: [], thinkingSince: null, thoughtMs: 0 });
  turn = thread.applyUpdate(turn, thought('Let me '), 1000);
  turn = thread.applyUpdate(turn, thought('check.'), 1500);
  turn = thread.applyUpdate(turn, { sessionUpdate: 'tool_call', toolCallId: 't1', title: 'Read app.py', kind: 'read' }, 2000);
  turn = thread.applyUpdate(turn, { sessionUpdate: 'tool_call', toolCallId: 't2', title: 'Searched cache' }, 2100);
  turn = thread.applyUpdate(turn, { sessionUpdate: 'tool_call_update', toolCallId: 't1', status: 'in_progress' }, 2200);
  turn = thread.applyUpdate(turn, { sessionUpdate: 'tool_call_update', toolCallId: 't1', status: 'completed', title: 'Read app.py#L1-40' }, 2500);
  turn = thread.applyUpdate(turn, { sessionUpdate: 'tool_call_update', toolCallId: 't2', status: 'failed' }, 2600);
  turn = thread.applyUpdate(turn, { sessionUpdate: 'tool_call_update', toolCallId: 'unknown', status: 'completed' }, 2700);
  assert.equal(turn.thoughts, 'Let me check.');
  assert.deepEqual(turn.steps, [
    { id: 't1', title: 'Read app.py#L1-40', status: 'completed' },
    { id: 't2', title: 'Searched cache', status: 'failed' },
  ]);
  assert.equal(turn.thinkingSince, 1000);
  assert.equal(thread.thinkingLabel(turn), 'Thinking');

  turn = thread.applyUpdate(turn, message('A '), 7000);
  turn = thread.applyUpdate(turn, message('cache.'), 7100);
  turn = thread.applyUpdate(turn, { sessionUpdate: 'usage_update', used: 1 }, 7200);
  assert.equal(turn.assistant, 'A cache.');
  assert.deepEqual([turn.thinkingSince, turn.thoughtMs], [null, 6000]);
  assert.equal(thread.thinkingLabel(turn), 'Thought for 6s');

  // Thinking again later adds to the time, and finishing closes it.
  turn = thread.applyUpdate(turn, thought(' More.'), 8000);
  assert.equal(thread.thinkingLabel(turn), 'Thinking');
  const done = thread.finish(turn, 'end_turn', 9000);
  assert.deepEqual([done.status, done.thinkingSince, done.thoughtMs, done.thoughts], ['done', null, 7000, 'Let me check. More.']);
  assert.equal(thread.thinkingLabel(done), 'Thought for 7s');
});

test('the thinking label rounds the time and is absent without thoughts or steps', () => {
  const finished = (thoughtMs) => ({ ...thread.newTurn('q'), status: 'done', thoughts: 'x', thoughtMs });
  assert.equal(thread.thinkingLabel(finished(400)), 'Thought for 1s');
  assert.equal(thread.thinkingLabel(finished(59_400)), 'Thought for 59s');
  assert.equal(thread.thinkingLabel(finished(65_000)), 'Thought for 1m 5s');
  assert.equal(thread.thinkingLabel({ ...finished(0), thoughts: '', steps: [{ id: 't', title: 'Read a', status: 'completed' }] }), 'Thought for 1s');
  assert.equal(thread.thinkingLabel(thread.newTurn('q')), null);
  assert.equal(thread.thinkingLabel(thread.finish(thread.applyUpdate(thread.newTurn('q'), message('hi'), 5), 'end_turn', 9)), null);
  // Turns saved by the older page have no thinking fields.
  assert.equal(thread.thinkingLabel({ user: 'q', assistant: 'a', status: 'done', activity: '' }), null);
});

test('only finished turns become history', () => {
  const answered = thread.finish(thread.applyUpdate(thread.newTurn('q'), message('A cache.'), 1), 'end_turn', 2);
  const stopped = thread.finish({ ...answered, user: 'q2' }, 'cancelled', 3);
  assert.equal(stopped.status, 'stopped');
  const failed = { ...thread.newTurn('q3'), status: 'error', error: 'x' };
  const empty = thread.finish(thread.applyUpdate(thread.newTurn('q4'), thought('hmm'), 1), 'end_turn', 2);
  assert.deepEqual(thread.historyOf([answered, stopped, failed, empty]), [{ user: 'q', assistant: 'A cache.' }]);
});

test('answers render code, lists and emphasis but never markup from the text', () => {
  const html = renderMarkdown('# Title\nUse `a<b>` and **bold** <img src=x onerror=alert(1)>\n- one\n- two\n1. first\n> quoted\n```js\nif (a < b) {}\n```\nafter');
  assert.equal(
    html,
    '<h4>Title</h4><p>Use <code>a&lt;b&gt;</code> and <strong>bold</strong> &lt;img src=x onerror=alert(1)&gt;</p>' +
      '<ul><li>one</li><li>two</li></ul><ol><li>first</li></ol><blockquote>quoted</blockquote>' +
      '<pre><code>if (a &lt; b) {}</code></pre><p>after</p>',
  );
  assert.equal(renderMarkdown('```\nstill streaming <x>'), '<pre><code>still streaming &lt;x&gt;</code></pre>');
  assert.equal(renderMarkdown('"quote" & more'), '<p>&quot;quote&quot; &amp; more</p>');
});

test('streamed answers split into whole JSON lines, keeping the unfinished tail', () => {
  assert.deepEqual(thread.takeLines('{"update":1}\n\n{"done":"end_turn"}\n{"upd'), { messages: [{ update: 1 }, { done: 'end_turn' }], rest: '{"upd' });
  assert.deepEqual(thread.takeLines(''), { messages: [], rest: '' });
});
