'use strict';
// Status bar badge for the subagents running in this window's chats, with a list of what each is doing.

const { execFile } = require('node:child_process');
const path = require('node:path');

const LIST_TIMEOUT_MS = 15_000;

function elapsed(startedAt, now = Date.now() / 1000) {
  const minutes = Math.floor(Math.max(0, now - startedAt) / 60);
  if (minutes < 1) return 'under 1m';
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}

function name(agent) {
  return agent.title || agent.profile || 'Subagent';
}

function where(agent) {
  const project = path.basename(agent.project || '');
  return [project, agent.chat].filter(Boolean).join(' · ');
}

function statusText(agents) {
  return `$(sync~spin) ${agents.length} subagent${agents.length === 1 ? '' : 's'}`;
}

function tooltip(agents, now) {
  const lines = agents.map((a) => `${name(a)}${a.background ? ' (background)' : ''} · running ${elapsed(a.started_at, now)} · ${where(a)}${a.last_step ? `\n    ${a.last_step}` : ''}`);
  return `Subagents running in this window\n\n${lines.join('\n')}\n\nClick to see what each one is doing.`;
}

function pickItems(agents, now) {
  return agents.map((agent) => ({
    label: `$(sync~spin) ${name(agent)}`,
    description: [where(agent), `running ${elapsed(agent.started_at, now)}`, agent.background ? 'background' : ''].filter(Boolean).join(' · '),
    detail: agent.last_step || 'No saved steps yet',
    agent,
  }));
}

function stepItems(agent) {
  const steps = (agent.recent || '').split('\n').filter(Boolean);
  return steps.length ? steps.map((step) => ({ label: step })) : [{ label: 'No saved steps yet' }];
}

/** Runs `ds subagents --window HOST`: the running subagents of the chats this extension host started. */
function listSubagents(command, host, run = execFile) {
  return new Promise((resolve, reject) => {
    run(command[0], [...command.slice(1), 'subagents', '--window', String(host)], { timeout: LIST_TIMEOUT_MS }, (error, stdout) => {
      if (error) return reject(error);
      try {
        resolve(JSON.parse(stdout).subagents);
      } catch (parseError) {
        reject(parseError);
      }
    });
  });
}

class SubagentStatus {
  /**
   * @param {typeof import('vscode')} vscode
   * @param {() => Promise<object[]>} list
   */
  constructor(vscode, list, now = () => Date.now() / 1000) {
    this.vscode = vscode;
    this.list = list;
    this.now = now;
    this.agents = [];
    this.refreshing = false;
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 91);
    this.item.name = 'Subagents';
    this.item.command = 'switchPanels.showSubagents';
  }

  async refresh() {
    if (this.refreshing || !this.vscode.window.state.focused) return;
    this.refreshing = true;
    try {
      this.agents = await this.list();
    } catch {
      this.agents = [];
    } finally {
      this.refreshing = false;
    }
    if (!this.agents.length) return this.item.hide();
    this.item.text = statusText(this.agents);
    this.item.tooltip = tooltip(this.agents, this.now());
    this.item.show();
  }

  async show() {
    await this.refresh();
    const { window } = this.vscode;
    if (!this.agents.length) return void window.showInformationMessage('No subagents are running in this window.');
    const chosen = await window.showQuickPick(pickItems(this.agents, this.now()), { placeHolder: 'Subagents running in this window', matchOnDescription: true, matchOnDetail: true });
    if (chosen) await window.showQuickPick(stepItems(chosen.agent), { placeHolder: `${name(chosen.agent)} · latest steps, newest last` });
  }

  dispose() {
    this.item.dispose();
  }
}

module.exports = { SubagentStatus, elapsed, listSubagents, pickItems, statusText, stepItems, tooltip };
