'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { SidePanel, sideQuote } = require('../../src/devin_switch/desktop_extension/side');
const thread = require('../../src/devin_switch/desktop_extension/media/side-thread');
const { renderMarkdown } = require('../../src/devin_switch/desktop_extension/media/markdown');

test('a quoted question names its source and keeps the quote line by line', () => {
  assert.equal(thread.userText('  why?  '), 'why?');
  assert.equal(thread.userText('why?', '  \n'), 'why?');
  assert.equal(thread.userText('why async?', 'await load()\nreturn x\n\n'), '> await load()\n> return x\n\nwhy async?');
  assert.equal(thread.userText('what is it?', 'cache = {}', 'src/app.py line 3'), 'From src/app.py line 3:\n> cache = {}\n\nwhat is it?');
});

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

test('the picked chat stays while open, else the most recently active one is picked', () => {
  const chats = [
    { sessionId: 'b', title: 'Fix login', folder: '/Users/me/work/app/' },
    { sessionId: 'a', title: 'New chat', folder: '/' },
  ];
  assert.equal(thread.pickChat(chats, 'a'), 'a');
  assert.equal(thread.pickChat(chats, 'gone'), 'b');
  assert.equal(thread.pickChat([], 'a'), null);
  assert.equal(thread.chatLabel(chats[0]), 'Fix login · app');
  assert.equal(thread.chatLabel(chats[1]), 'New chat · /');
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

/** Just enough of the vscode API for one webview panel. */
function fakeVscode({ editor = null, clipboard = '' } = {}) {
  const created = [];
  const vscode = {
    ViewColumn: { Beside: -2 },
    Uri: { joinPath: (base, ...parts) => [base, ...parts].join('/') },
    ThemeIcon: class {
      constructor(id) {
        this.id = id;
      }
    },
    env: { clipboard: { readText: async () => clipboard } },
    workspace: { asRelativePath: (uri) => uri.replace('/repo/', '') },
    window: {
      activeTextEditor: editor,
      createWebviewPanel(viewType, title, showOptions, options) {
        const handlers = {};
        const panel = {
          viewType,
          title,
          showOptions,
          options,
          visible: true,
          reveals: [],
          posted: [],
          webview: {
            html: '',
            cspSource: 'vscode-resource:',
            asWebviewUri: (uri) => `webview:${uri}`,
            postMessage: async (message) => panel.posted.push(message),
            onDidReceiveMessage: (fn) => ((handlers.message = fn), { dispose() {} }),
          },
          onDidChangeViewState: (fn) => ((handlers.view = fn), { dispose() {} }),
          onDidDispose: (fn) => ((handlers.dispose = fn), { dispose() {} }),
          reveal: (column, preserveFocus) => panel.reveals.push(preserveFocus),
          send: (message) => handlers.message(message),
          dispose: () => handlers.dispose(),
        };
        created.push(panel);
        return panel;
      },
    },
  };
  return { vscode, created };
}

/** Records requests and answers like `ds side view`. */
function fakeHelper() {
  return {
    calls: [],
    async ask(op, fields = {}, options = {}) {
      this.calls.push({ op, fields, timeoutMs: options.timeoutMs });
      if (op === 'chats') return { chats: [{ sessionId: 's1', title: 'T', folder: '/a' }] };
      if (op === 'ask') {
        if (fields.text === 'fail') throw new Error('This chat is open in another Devin window.');
        options.onEvent({ id: 1, event: 'update', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'hi' } } });
        return { ok: true, stopReason: 'end_turn' };
      }
      return { ok: true };
    },
  };
}

test('the panel lists chats first, then delivers quotes and /btw questions sent before it was ready', async () => {
  const { vscode, created } = fakeVscode();
  const helper = fakeHelper();
  const side = new SidePanel(vscode, '/ext', helper);
  side.quote('const x = 1', 'a.js line 1');
  side.question('s1', 'why?');
  assert.equal(created.length, 1, 'one panel for both');
  const [panel] = created;
  assert.deepEqual(panel.showOptions, { viewColumn: -2, preserveFocus: false });
  assert.deepEqual(panel.options.localResourceRoots, ['/ext/media']);
  assert.match(panel.webview.html, /script-src 'nonce-[^']+'/);
  for (const name of ['side-thread.js', 'markdown.js', 'side-view.js', 'side.css']) assert.ok(panel.webview.html.includes(`webview:/ext/media/${name}`), name);
  assert.deepEqual(panel.posted, []);
  await panel.send({ type: 'ready' });
  assert.deepEqual(panel.posted, [
    { type: 'chats', chats: [{ sessionId: 's1', title: 'T', folder: '/a' }] },
    { type: 'quote', text: 'const x = 1', source: 'a.js line 1' },
    { type: 'question', sessionId: 's1', text: 'why?' },
  ]);
  side.quote('more');
  assert.deepEqual(panel.posted.at(-1), { type: 'quote', text: 'more', source: '' });
  // Opening counts as showing; later quotes and questions reveal it with focus.
  assert.deepEqual(panel.reveals, [false, false]);
});

test('the panel streams a side answer, reports failures and forwards Stop', async () => {
  const { vscode, created } = fakeVscode();
  const helper = fakeHelper();
  const side = new SidePanel(vscode, '/ext', helper).show();
  const [panel] = created;
  await panel.send({ type: 'ready' });
  const history = [{ user: 'q', assistant: 'a' }];
  await panel.send({ type: 'ask', requestId: 'r1', sessionId: 's1', history, text: 'next?' });
  assert.deepEqual(helper.calls.at(-1), { op: 'ask', fields: { sessionId: 's1', history, text: 'next?' }, timeoutMs: 0 });
  assert.deepEqual(panel.posted.slice(-2), [
    { requestId: 'r1', type: 'update', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'hi' } } },
    { requestId: 'r1', type: 'done', stopReason: 'end_turn' },
  ]);
  await panel.send({ type: 'ask', requestId: 'r2', sessionId: 's1', history: [], text: 'fail' });
  assert.deepEqual(panel.posted.at(-1), { requestId: 'r2', type: 'failed', error: 'This chat is open in another Devin window.' });
  await panel.send({ type: 'cancel', sessionId: 's1' });
  assert.deepEqual(helper.calls.at(-1), { op: 'cancel', fields: { sessionId: 's1' }, timeoutMs: undefined });
  panel.dispose();
  assert.equal(side.panel, null);
  side.show();
  assert.equal(created.length, 2, 'a closed panel opens fresh');
});

test('Ask on the Side quotes the editor selection with its place, else the clipboard', async () => {
  const selection = { isEmpty: false, start: { line: 9 }, end: { line: 11 } };
  const editor = { selection, document: { uri: '/repo/src/app.py', getText: () => 'def f():\n    pass' } };
  const { vscode } = fakeVscode({ editor, clipboard: '  copied from the chat \n' });
  assert.deepEqual(await sideQuote(vscode, { from: 'selection' }), { text: 'def f():\n    pass', source: 'src/app.py lines 10-12' });
  assert.deepEqual(await sideQuote(vscode, undefined), { text: 'def f():\n    pass', source: 'src/app.py lines 10-12' });
  assert.deepEqual(await sideQuote(vscode, { from: 'clipboard' }), { text: 'copied from the chat', source: '' });
  selection.end = { line: 9 };
  assert.equal((await sideQuote(vscode, {})).source, 'src/app.py line 10');
  selection.isEmpty = true;
  assert.deepEqual(await sideQuote(vscode, { from: 'selection' }), { text: 'copied from the chat', source: '' });
});
