'use strict';
// Side chat thread state, shared by the side page (browser) and the node tests.
(function (root) {
  function newTurn(user) {
    return { user, assistant: '', status: 'answering', thoughts: '', steps: [], thinkingSince: null, thoughtMs: 0 };
  }

  /** Earlier finished turns, which the side agent can't remember on its own. */
  function historyOf(turns) {
    return turns.filter((t) => t.status === 'done' && t.assistant).map(({ user, assistant }) => ({ user, assistant }));
  }

  /** Thinking time runs from a thought or tool step until the answer (or the end) resumes. */
  const thinking = (turn, now) => ({ ...turn, thinkingSince: turn.thinkingSince ?? now });
  const stopThinking = (turn, now) =>
    turn.thinkingSince == null ? turn : { ...turn, thinkingSince: null, thoughtMs: turn.thoughtMs + now - turn.thinkingSince };

  /** Folds one side-chain update from the agent into the answering turn. */
  function applyUpdate(turn, update, now = Date.now()) {
    const text = update?.content?.type === 'text' ? update.content.text : '';
    switch (update?.sessionUpdate) {
      case 'agent_message_chunk':
        return { ...stopThinking(turn, now), assistant: turn.assistant + text };
      case 'agent_thought_chunk':
        return { ...thinking(turn, now), thoughts: turn.thoughts + text };
      case 'tool_call':
        return { ...thinking(turn, now), steps: [...turn.steps, { id: update.toolCallId, title: update.title || 'Tool', status: update.status || 'pending' }] };
      case 'tool_call_update':
        return {
          ...turn,
          steps: turn.steps.map((step) =>
            step.id === update.toolCallId ? { ...step, title: update.title || step.title, status: update.status || step.status } : step,
          ),
        };
      default:
        return turn;
    }
  }

  function finish(turn, stopReason, now = Date.now()) {
    return { ...stopThinking(turn, now), status: stopReason === 'cancelled' ? 'stopped' : 'done' };
  }

  /** "Thinking" while it thinks, then "Thought for 6s"; null when it never thought. */
  function thinkingLabel(turn) {
    if (!turn.thoughts && !turn.steps?.length) return null;
    if (turn.thinkingSince != null) return 'Thinking';
    const seconds = Math.max(1, Math.round(turn.thoughtMs / 1000));
    return `Thought for ${seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`}`;
  }

  /** Splits a streamed body into complete JSON lines, keeping the unfinished tail. */
  function takeLines(buffer) {
    const parts = buffer.split('\n');
    const rest = parts.pop();
    return { messages: parts.filter((line) => line.trim()).map((line) => JSON.parse(line)), rest };
  }

  const api = { newTurn, historyOf, applyUpdate, finish, thinkingLabel, takeLines };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SideThread = api;
})(typeof window === 'undefined' ? globalThis : window);
