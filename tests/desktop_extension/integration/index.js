'use strict';
// Runs inside a real Desktop extension host (--extensionTestsPath). Driven by tests/test_desktop_extension.py.
const vscode = require('vscode');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const env = process.env;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(what, check, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const value = await check();
      if (value) return value;
    } catch {}
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${what}`);
}

const tabLabels = () => vscode.window.tabGroups.all.flatMap((g) => g.tabs.map((t) => t.label));

async function attachedPages() {
  const list = await (await fetch(`http://127.0.0.1:${env.PANELS_CHROME_PORT}/json/list`)).json();
  // /json/list has no "attached" flag; ask the browser target instead.
  const { webSocketDebuggerUrl } = await (await fetch(`http://127.0.0.1:${env.PANELS_CHROME_PORT}/json/version`)).json();
  const socket = new WebSocket(webSocketDebuggerUrl);
  await new Promise((resolve, reject) => ((socket.onopen = resolve), (socket.onerror = reject)));
  const reply = await new Promise((resolve) => {
    socket.onmessage = (event) => resolve(JSON.parse(event.data));
    socket.send(JSON.stringify({ id: 1, method: 'Target.getTargets' }));
  });
  socket.close();
  return { pages: list.filter((t) => t.type === 'page').length, attached: reply.result.targetInfos.filter((t) => t.type === 'page' && t.attached).length };
}

async function whiteboard(method, route, body) {
  const res = await fetch(`${env.PANELS_WB_URL}${route}`, {
    method,
    headers: { Authorization: `Bearer ${env.PANELS_WB_TOKEN}`, 'Content-Type': 'application/json' },
    body: body && JSON.stringify(body),
  });
  return { status: res.status, body: await res.json() };
}

/** Minimal ACP client for a `ds acp` bridge started as this window's agent. */
function startBridge() {
  const { spawn } = require('node:child_process');
  const [command, ...args] = JSON.parse(env.PANELS_DS);
  // Desktop starts agents with its extension host's pid; the side panel finds the bridge by it.
  const child = spawn(command, [...args, 'acp'], { env: { ...env, WINDSURF_EXT_HOST_PID: String(process.pid) }, stdio: ['pipe', 'pipe', 'ignore'] });
  const pending = new Map();
  const notes = [];
  let buffer = '';
  let next = 0;
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    buffer += chunk;
    let index;
    while ((index = buffer.indexOf('\n')) >= 0) {
      const message = JSON.parse(buffer.slice(0, index));
      buffer = buffer.slice(index + 1);
      if (message.method === 'session/update' && message.params.update.sessionUpdate === 'agent_message_chunk') notes.push(message.params.update.content.text);
      if (pending.has(message.id) && !message.method) {
        pending.get(message.id)(message);
        pending.delete(message.id);
      }
    }
  });
  const request = (method, params) =>
    new Promise((resolve) => {
      const id = ++next;
      pending.set(id, resolve);
      child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
  return { request, notes, close: () => child.stdin.end() };
}

const sidePrompts = () =>
  fs.existsSync(env.FAKE_ACP_LOG)
    ? fs.readFileSync(env.FAKE_ACP_LOG, 'utf8').split('\n').filter(Boolean).map(JSON.parse).filter((c) => c.chain === 'side').map((c) => c.text)
    : [];

/** /btw typed in a chat opens the real side panel, which asks, shows the answer and follows up. */
async function sideChat(results) {
  await vscode.extensions.getExtension('devin-switch.panels').activate();
  const commands = await vscode.commands.getCommands(true);
  results.commands = ['switchPanels.openSideChat', 'switchPanels.askOnSide'].filter((c) => commands.includes(c));
  const bridge = startBridge();
  try {
    await bridge.request('initialize', { protocolVersion: 1, clientCapabilities: {} });
    const { result } = await bridge.request('session/new', { cwd: env.PANELS_PROJECT, mcpServers: [] });
    const prompt = (text) => bridge.request('session/prompt', { sessionId: result.sessionId, prompt: [{ type: 'text', text }] });
    results.first = (await prompt('/btw what is a cache?')).result;
    results.sideTab = await waitFor('the Side chat tab', () => tabLabels().includes('Side chat'));
    await waitFor('the side question to reach the agent', () => sidePrompts().length === 1);
    // The follow-up carries the first answer, which the panel must have streamed and finished.
    await sleep(500);
    results.second = (await prompt('/btw is it shared?')).result;
    results.prompts = await waitFor('the follow-up to reach the agent', () => sidePrompts().length === 2 && sidePrompts());
    results.sessionId = result.sessionId;
    await vscode.env.clipboard.writeText('copied from the chat');
    await vscode.commands.executeCommand('switchPanels.askOnSide', { from: 'clipboard' });
    results.askedOnSide = tabLabels().filter((l) => l === 'Side chat').length;
  } finally {
    results.notes = bridge.notes;
    bridge.close();
  }
}

exports.run = async () => {
  const results = {};
  if (env.PANELS_SIDE) {
    try {
      await sideChat(results);
    } catch (error) {
      results.error = String((error && error.stack) || error);
    }
    fs.writeFileSync(env.PANELS_RESULT, JSON.stringify(results));
    return;
  }
  try {
    await vscode.extensions.getExtension('devin-switch.panels').activate();
    const workspace = vscode.workspace.workspaceFolders[0].uri.fsPath;
    // Chats started by this window carry the extension host's pid (WINDSURF_EXT_HOST_PID); the runner shares that process.
    const hostId = String(process.pid);
    results.hostId = hostId;
    results.hostProcess = execFileSync('ps', ['-o', 'command=', '-p', hostId], { encoding: 'utf8', timeout: 5000 });
    await whiteboard('PUT', '/api/canvases/panel-test', { title: 'How MCP works', workspace, chat: 'chat-1' });
    // A chat whose folder this window doesn't show: only the window hint can route it here.
    await whiteboard('PUT', '/api/canvases/panel-test-2', { title: 'Auth flow', workspace: '/nonexistent/other-folder', chat: 'chat-2' });
    // The extension connects to the daemon as a panel host within a few seconds.
    results.shown = await waitFor('the daemon to reach the panel host', async () => (await whiteboard('POST', '/api/canvases/panel-test/show', { workspace, host: hostId })).status === 200);
    results.canvasTab = await waitFor('the board tab titled after the topic', () => tabLabels().includes('How MCP works'));
    results.foreign = {
      // Never shown in an unrelated window: neither by folder nor for another window's id.
      noHint: (await whiteboard('POST', '/api/canvases/panel-test-2/show', { workspace: '/nonexistent/other-folder' })).status,
      otherWindow: (await whiteboard('POST', '/api/canvases/panel-test-2/show', { workspace: '/nonexistent/other-folder', host: '1' })).status,
    };
    results.shownByHint = (await whiteboard('POST', '/api/canvases/panel-test-2/show', { workspace: '/nonexistent/other-folder', host: hostId })).status;
    results.secondTab = await waitFor('a second tab for the second board', () => tabLabels().includes('Auth flow'));
    const viewers = async () => {
      const { body } = await whiteboard('GET', '/api/canvases');
      return Object.fromEntries(['panel-test', 'panel-test-2'].map((id) => [id, body.find((b) => b.id === id)?.viewers || 0]));
    };
    results.viewers = await waitFor('both boards to have a live viewer', async () => {
      const counts = await viewers();
      return counts['panel-test'] > 0 && counts['panel-test-2'] > 0 && counts;
    });
    results.reshown = (await whiteboard('POST', '/api/canvases/panel-test/show', { workspace, host: hostId })).status;
    results.active = await waitFor('the first board to be revealed', () => {
      const label = vscode.window.tabGroups.all.map((g) => g.activeTab?.label).find((l) => l === 'How MCP works' || l === 'Auth flow');
      return label === 'How MCP works' && label;
    });
    results.boardTabs = tabLabels().filter((l) => l === 'How MCP works' || l === 'Auth flow');
    results.viewersAfter = await viewers();

    await vscode.commands.executeCommand('switchPanels.openBrowser');
    results.browserTab = await waitFor('the Agent Browser tab', () => tabLabels().includes('Agent Browser'));
    results.browser = await waitFor('the panel to attach to the agent browser', async () => {
      const state = await attachedPages();
      return state.attached > 0 && state;
    });
  } catch (error) {
    results.error = String((error && error.stack) || error);
  }
  fs.writeFileSync(env.PANELS_RESULT, JSON.stringify(results));
};
