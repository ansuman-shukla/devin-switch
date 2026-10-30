'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const thread = require('../../src/devin_switch/side_page/side-thread');
const { renderMarkdown } = require('../../src/devin_switch/side_page/markdown');

test('only finished turns become history, and updates build the answer', () => {
  let turn = { user: 'q', assistant: '', status: 'answering', activity: 'Asking…' };
  turn = thread.applyUpdate(turn, { sessionUpdate: 'agent_thought_chunk', content: { type: 'text', text: 'hmm' } });
  assert.equal(turn.activity, 'Thinking…');
  turn = thread.applyUpdate(turn, { sessionUpdate: 'tool_call', title: 'Read app.py' });
  assert.equal(turn.activity, 'Looking at Read app.py…');
  turn = thread.applyUpdate(turn, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'A ' } });
  turn = thread.applyUpdate(turn, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'cache.' } });
  turn = thread.applyUpdate(turn, { sessionUpdate: 'agent_thought_chunk', content: { type: 'text', text: 'more' } });
  turn = thread.applyUpdate(turn, { sessionUpdate: 'usage_update', used: 1 });
  assert.deepEqual(turn, { user: 'q', assistant: 'A cache.', status: 'answering', activity: '' });
  const done = thread.finish(turn, 'end_turn');
  const stopped = thread.finish({ ...turn, user: 'q2' }, 'cancelled');
  assert.equal(done.status, 'done');
  assert.equal(stopped.status, 'stopped');
  const failed = { user: 'q3', assistant: '', status: 'error', error: 'x' };
  const empty = { user: 'q4', assistant: '', status: 'done' };
  assert.deepEqual(thread.historyOf([done, stopped, failed, empty]), [{ user: 'q', assistant: 'A cache.' }]);
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
