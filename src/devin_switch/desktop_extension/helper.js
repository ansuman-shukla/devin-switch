'use strict';
// JSON-lines client for a long-running `ds` helper (`ds browser view`, `ds side view`).
// Replies carry the request id and `ok`; lines with a pending id but no `ok` are progress
// events for that request, and lines for no pending request are pushed to `onEvent` listeners.

const { spawn } = require('node:child_process');

const DEFAULT_TIMEOUT_MS = 90_000;

class Helper {
  /**
   * @param {string[]} command  the `ds` argv
   * @param {string[]} args  the helper subcommand, e.g. ['browser', 'view']
   */
  constructor(command, args, { name = args[0] } = {}) {
    this.command = command;
    this.args = args;
    this.name = name;
    this.process = null;
    this.pending = new Map();
    this.listeners = new Set();
    this.nextId = 0;
    this.buffer = '';
  }

  ensure() {
    if (this.process) return this.process;
    const child = spawn(this.command[0], [...this.command.slice(1), ...this.args], { stdio: ['pipe', 'pipe', 'ignore'] });
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
        this.receive(reply);
      }
    });
    const fail = (message) => {
      for (const [id, call] of this.pending) {
        if (call.child !== child) continue;
        this.pending.delete(id);
        clearTimeout(call.timer);
        call.reject(new Error(message));
      }
      if (this.process === child) {
        this.process = null;
        this.buffer = '';
      }
    };
    child.on('error', (error) => fail(`Could not start ds: ${error.message}`));
    child.on('exit', () => fail(`The ds ${this.name} helper exited.`));
    this.process = child;
    return child;
  }

  receive(reply) {
    const call = this.pending.get(reply?.id);
    if (!call) {
      for (const listener of this.listeners) listener(reply);
      return;
    }
    if (!('ok' in reply)) return call.onEvent?.(reply);
    this.pending.delete(reply.id);
    clearTimeout(call.timer);
    if (reply.ok) call.resolve(reply);
    else call.reject(new Error(reply.error));
  }

  /** Listen for events the helper pushes on its own (not replies to a request). */
  onEvent(listener) {
    this.listeners.add(listener);
    return { dispose: () => this.listeners.delete(listener) };
  }

  /** timeoutMs 0 waits as long as the answer takes (a streamed side answer). */
  ask(op, fields = {}, { onEvent, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    const child = this.ensure();
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = timeoutMs
        ? setTimeout(() => {
            this.pending.delete(id);
            reject(new Error(`ds ${this.name} ${op} timed out.`));
          }, timeoutMs)
        : null;
      this.pending.set(id, { resolve, reject, timer, onEvent, child });
      child.stdin.write(`${JSON.stringify({ id, op, ...fields })}\n`);
    });
  }

  dispose() {
    // Closing stdin makes the helper release what it holds and exit.
    this.process?.stdin.end();
    this.process = null;
  }
}

module.exports = { Helper };
