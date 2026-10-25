'use strict';
const vscode = require('vscode');
const { spawn } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { Cdp } = require('./cdp');
const { BrowserView } = require('./browser');
const { CanvasPanels } = require('./canvas');
const { SubagentStatus, listSubagents } = require('./subagents');
const { WhiteboardHost, whiteboardHome } = require('./whiteboard');

const STATUS_POLL_MS = 5000;
const HELPER_TIMEOUT_MS = 90_000;

/** `ds` argv recorded by `ds panels install`; falls back to `ds` on PATH. */
function dsConfig(extensionPath) {
  try {
    const { command } = JSON.parse(readFileSync(path.join(extensionPath, 'ds.json'), 'utf8'));
    if (Array.isArray(command) && command.length) return { command };
  } catch {
    // not installed through ds
  }
  return { command: ['ds'] };
}

/** JSON-lines client for `ds browser view`; one per window, holding at most one lease. */
class Helper {
  constructor(command) {
    this.command = command;
    this.process = null;
    this.pending = new Map();
    this.nextId = 0;
    this.buffer = '';
  }

  ensure() {
    if (this.process) return this.process;
    const child = spawn(this.command[0], [...this.command.slice(1), 'browser', 'view'], { stdio: ['pipe', 'pipe', 'ignore'] });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      this.buffer += chunk;
      let index;
      while ((index = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, index);
        this.buffer = this.buffer.slice(index + 1);
        let reply;
        try {
          reply = JSON.parse(line);
        } catch {
          continue;
        }
        const call = this.pending.get(reply.id);
        if (!call) continue;
        this.pending.delete(reply.id);
        clearTimeout(call.timer);
        if (reply.ok) call.resolve(reply);
        else call.reject(new Error(reply.error));
      }
    });
    const fail = (message) => {
      for (const call of this.pending.values()) {
        clearTimeout(call.timer);
        call.reject(new Error(message));
      }
      this.pending.clear();
      this.process = null;
      this.buffer = '';
    };
    child.on('error', (error) => fail(`Could not start ds: ${error.message}`));
    child.on('exit', () => fail('The ds browser helper exited.'));
    this.process = child;
    return child;
  }

  ask(op, fields = {}) {
    const child = this.ensure();
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`ds browser ${op} timed out.`));
      }, HELPER_TIMEOUT_MS);
      this.pending.set(id, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify({ id, op, ...fields })}\n`);
    });
  }

  dispose() {
    // Closing stdin makes the helper release its lease and exit.
    this.process?.stdin.end();
    this.process = null;
  }
}

function nonce() {
  return randomBytes(16).toString('base64');
}

class BrowserPanel {
  static current = null;

  static show(context, helper) {
    if (BrowserPanel.current) return BrowserPanel.current.panel.reveal(vscode.ViewColumn.Beside, true);
    const media = vscode.Uri.joinPath(context.extensionUri, 'media');
    const panel = vscode.window.createWebviewPanel('switchPanels.browser', 'Agent Browser', { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true }, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [media],
    });
    BrowserPanel.current = new BrowserPanel(panel, media, helper);
  }

  constructor(panel, media, helper) {
    this.panel = panel;
    this.helper = helper;
    this.cdp = null;
    this.view = null;
    this.attaching = null;
    panel.iconPath = new vscode.ThemeIcon('globe');
    const n = nonce();
    const asset = (name) => panel.webview.asWebviewUri(vscode.Uri.joinPath(media, name));
    panel.webview.html = `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src ${panel.webview.cspSource}; script-src 'nonce-${n}';">
<link rel="stylesheet" href="${asset('browser.css')}"></head><body>
<script nonce="${n}" src="${asset('browser-view.js')}"></script></body></html>`;
    this.subscriptions = [
      panel.webview.onDidReceiveMessage((message) => this.receive(message)),
      panel.onDidDispose(() => this.dispose()),
    ];
  }

  post(message) {
    void this.panel.webview.postMessage(message);
  }

  async receive(message) {
    try {
      if (message.type === 'ready') return await this.refreshProfiles(true);
      if (message.type === 'profiles') return await this.refreshProfiles(false);
      if (message.type === 'attach') return await this.attach(message.profile);
      if (!this.view) return;
      const result = await this.view.handle(message);
      if (message.type === 'copy' && result) await vscode.env.clipboard.writeText(result);
    } catch (error) {
      this.post({ type: 'status', text: error.message });
    }
  }

  async refreshProfiles(autoAttach) {
    const { profiles } = await this.helper.ask('profiles');
    this.post({ type: 'profiles', profiles });
    if (!autoAttach || this.view) return;
    // Prefer the browser an agent started most recently; otherwise wait for the human to pick.
    const live = profiles.filter((p) => p.running).sort((a, b) => (b.running.since ?? 0) - (a.running.since ?? 0));
    if (live[0]) await this.attach(live[0].directory);
  }

  async attach(profile) {
    if (this.attaching) await this.attaching.catch(() => {});
    this.attaching = (async () => {
      await this.disconnect();
      this.post({ type: 'status', text: `Connecting to ${profile}…` });
      const { port } = await this.helper.ask('attach', { profile });
      this.cdp = await Cdp.connect(port);
      this.cdp.onClose(() => {
        this.view = null;
        this.post({ type: 'closed', profile });
      });
      this.view = new BrowserView(this.cdp, (m) => this.post(m));
      await this.view.start();
      this.post({ type: 'attached', profile });
      await this.refreshProfiles(false);
    })();
    return this.attaching;
  }

  async disconnect() {
    const view = this.view;
    this.view = null;
    if (view) await view.dispose();
    this.cdp?.close();
    this.cdp = null;
  }

  dispose() {
    if (BrowserPanel.current === this) BrowserPanel.current = null;
    for (const s of this.subscriptions) s.dispose();
    void this.disconnect().finally(() => this.helper.ask('detach').catch(() => {}));
  }
}

async function pickBoard(host, canvases) {
  const boards = await host.boards();
  if (!boards.length) return void vscode.window.showInformationMessage('No whiteboards in this workspace yet. Ask the agent to explain something visually.');
  const untitled = (b) => !b.title || b.title === b.id;
  const items = boards.map((b) => ({ label: untitled(b) ? 'Untitled board' : b.title, description: `${b.elementCount} elements`, detail: new Date(b.updatedAt).toLocaleString(), board: b }));
  const chosen = items.length === 1 ? items[0] : await vscode.window.showQuickPick(items, { placeHolder: 'Open a whiteboard from this workspace' });
  if (chosen) canvases.show(chosen.board.id, host.viewerUrl(chosen.board.id), chosen.label);
}

function activate(context) {
  const config = dsConfig(context.extensionPath);
  const helper = new Helper(config.command);
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 90);
  status.command = 'switchPanels.openBrowser';
  status.name = 'Agent Browser';

  let polling = false;
  const poll = async () => {
    if (polling || !vscode.window.state.focused) return;
    polling = true;
    try {
      const { profiles } = await helper.ask('profiles');
      const live = profiles.filter((p) => p.running);
      if (live.length) {
        status.text = `$(globe) Agent browser${live.length > 1 ? ` ×${live.length}` : ''}`;
        status.tooltip = `Watch ${live.map((p) => p.email || p.directory).join(', ')}`;
        status.show();
      } else status.hide();
    } catch {
      status.hide();
    } finally {
      polling = false;
    }
  };
  const timer = setInterval(poll, STATUS_POLL_MS);
  void poll();
  // Desktop starts this window's chats from this extension host, so its pid identifies their bridges.
  const subagents = new SubagentStatus(vscode, () => listSubagents(config.command, process.pid));
  const subagentTimer = setInterval(() => subagents.refresh(), STATUS_POLL_MS);
  void subagents.refresh();

  const report = (error) => vscode.window.showErrorMessage(error.message);
  const canvases = new CanvasPanels(vscode);
  // The whiteboard daemon asks the window showing the chat's folder to open its board.
  const host = new WhiteboardHost({
    home: whiteboardHome(vscode.workspace.getConfiguration('switchPanels').get('whiteboardHome')),
    // This extension host started the window's chats, which pass its pid to whiteboard-mcp as WINDSURF_EXT_HOST_PID.
    hostId: String(process.pid),
    folders: () => (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath),
    focused: () => vscode.window.state.focused,
    onShow: ({ canvasId, url, title }) => canvases.show(canvasId, url, title),
  });
  host.start();
  context.subscriptions.push(
    status,
    subagents,
    { dispose: () => clearInterval(timer) },
    { dispose: () => clearInterval(subagentTimer) },
    { dispose: () => helper.dispose() },
    { dispose: () => host.dispose() },
    { dispose: () => canvases.dispose() },
    vscode.window.onDidChangeWindowState(() => {
      host.update();
      void subagents.refresh();
    }),
    vscode.commands.registerCommand('switchPanels.showSubagents', () => subagents.show().catch(report)),
    vscode.workspace.onDidChangeWorkspaceFolders(() => host.update()),
    vscode.commands.registerCommand('switchPanels.openCanvas', () => pickBoard(host, canvases).catch(report)),
    vscode.commands.registerCommand('switchPanels.openBrowser', () => BrowserPanel.show(context, helper)),
  );
}

function deactivate() {}

module.exports = { activate, deactivate, dsConfig };
