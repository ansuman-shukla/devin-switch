'use strict';
// Side chat thread state, shared by the side page (browser) and the node tests.
(function (root) {
  /** Earlier finished turns, which the side agent can't remember on its own. */
  function historyOf(turns) {
    return turns.filter((t) => t.status === 'done' && t.assistant).map(({ user, assistant }) => ({ user, assistant }));
  }

  /** Folds one side-chain update from the agent into the answering turn. */
  function applyUpdate(turn, update) {
    const text = update?.content?.type === 'text' ? update.content.text : '';
    switch (update?.sessionUpdate) {
      case 'agent_message_chunk':
        return { ...turn, assistant: turn.assistant + text, activity: '' };
      case 'agent_thought_chunk':
        return turn.assistant ? turn : { ...turn, activity: 'Thinking…' };
      case 'tool_call':
        return { ...turn, activity: `Looking at ${update.title || 'the code'}…` };
      default:
        return turn;
    }
  }

  function finish(turn, stopReason) {
    return { ...turn, status: stopReason === 'cancelled' ? 'stopped' : 'done', activity: '' };
  }

  /** Splits a streamed body into complete JSON lines, keeping the unfinished tail. */
  function takeLines(buffer) {
    const parts = buffer.split('\n');
    const rest = parts.pop();
    return { messages: parts.filter((line) => line.trim()).map((line) => JSON.parse(line)), rest };
  }

  const api = { historyOf, applyUpdate, finish, takeLines };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SideThread = api;
})(typeof window === 'undefined' ? globalThis : window);
