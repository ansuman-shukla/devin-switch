'use strict';
// Side chat: questions about a chat in this window, answered from its context by its own agent
// (through `ds side view` and the window's Switch bridge) without interrupting or entering it.

const { randomBytes } = require('node:crypto');

class SidePanel {
  /**
   * @param {typeof import('vscode')} vscode
   * @param {import('vscode').Uri} extensionUri
   * @param {import('./helper').Helper} helper  `ds side view` for this window
   */
  constructor(vscode, extensionUri, helper) {
    this.vscode = vscode;
    this.extensionUri = extensionUri;
    this.helper = helper;
    this.panel = null;
    this.ready = false;
    this.queue = [];
  }

  /** Opens the panel beside the chat, or reveals it. */
  show() {
    if (this.panel) {
      this.panel.reveal(undefined, false);
      return this;
    }
    const { vscode } = this;
    const media = vscode.Uri.joinPath(this.extensionUri, 'media');
    const panel = vscode.window.createWebviewPanel('switchPanels.side', 'Side chat', { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false }, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [media],
    });
    panel.iconPath = new vscode.ThemeIcon('comment-discussion');
    const nonce = randomBytes(16).toString('base64');
    const asset = (name) => panel.webview.asWebviewUri(vscode.Uri.joinPath(media, name));
    const script = (name) => `<script nonce="${nonce}" src="${asset(name)}"></script>`;
    panel.webview.html = `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${panel.webview.cspSource}; script-src 'nonce-${nonce}';">
<link rel="stylesheet" href="${asset('side.css')}"></head><body>
${script('side-thread.js')}${script('markdown.js')}${script('side-view.js')}</body></html>`;
    this.subscriptions = [
      panel.webview.onDidReceiveMessage((message) => this.receive(message)),
      panel.onDidChangeViewState(() => panel.visible && this.ready && this.refresh()),
      panel.onDidDispose(() => this.closed(panel)),
    ];
    this.panel = panel;
    this.ready = false;
    return this;
  }

  post(message) {
    if (this.ready) void this.panel.webview.postMessage(message);
    else this.queue.push(message);
  }

  /** Text to ask about, shown above the question box. */
  quote(text, source = '') {
    this.show().post({ type: 'quote', text, source });
  }

  /** A /btw typed in a chat: ask it straight away in that chat's side thread. */
  question(sessionId, text) {
    this.show().post({ type: 'question', sessionId, text });
  }

  async receive(message) {
    if (message.type === 'ready') {
      await this.refresh();
      this.ready = true;
      for (const queued of this.queue.splice(0)) void this.panel.webview.postMessage(queued);
    } else if (message.type === 'chats') await this.refresh();
    else if (message.type === 'ask') await this.ask(message);
    else if (message.type === 'cancel') await this.helper.ask('cancel', { sessionId: message.sessionId }).catch(() => {});
  }

  async refresh() {
    try {
      const { chats } = await this.helper.ask('chats');
      void this.panel?.webview.postMessage({ type: 'chats', chats });
    } catch (error) {
      void this.panel?.webview.postMessage({ type: 'chats', chats: [], error: error.message });
    }
  }

  async ask({ requestId, sessionId, history, text }) {
    const send = (message) => void this.panel?.webview.postMessage({ requestId, ...message });
    try {
      const { stopReason } = await this.helper.ask('ask', { sessionId, history, text }, {
        timeoutMs: 0,
        onEvent: (event) => send({ type: 'update', update: event.update }),
      });
      send({ type: 'done', stopReason });
    } catch (error) {
      send({ type: 'failed', error: error.message });
    }
  }

  closed(panel) {
    if (this.panel !== panel) return;
    for (const subscription of this.subscriptions) subscription.dispose();
    this.panel = null;
    this.ready = false;
    this.queue = [];
  }

  dispose() {
    this.panel?.dispose();
  }
}

/**
 * What "Ask on the Side" quotes: the editor selection (with where it came from) when asked from
 * the editor, else the clipboard, which is how text copied from the chat view arrives.
 * @returns {Promise<{ text: string, source: string }>}
 */
async function sideQuote(vscode, args) {
  const editor = vscode.window.activeTextEditor;
  if (args?.from !== 'clipboard' && editor && !editor.selection.isEmpty) {
    const { start, end } = editor.selection;
    const lines = start.line === end.line ? `line ${start.line + 1}` : `lines ${start.line + 1}-${end.line + 1}`;
    return { text: editor.document.getText(editor.selection), source: `${vscode.workspace.asRelativePath(editor.document.uri)} ${lines}` };
  }
  return { text: (await vscode.env.clipboard.readText()).trim(), source: '' };
}

module.exports = { SidePanel, sideQuote };
