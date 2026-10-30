'use strict';
// Side chat thread state, shared by the panel (browser) and the node tests.
(function (root) {
  /** The user turn as the side agent and the transcript see it. */
  function userText(question, quote = '', source = '') {
    const ask = question.trim();
    if (!quote.trim()) return ask;
    const lines = quote.replace(/\s+$/, '').split('\n').map((line) => `> ${line}`).join('\n');
    return `${source ? `From ${source}:\n` : ''}${lines}\n\n${ask}`;
  }

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

  /** Keeps the picked chat while it is open in the window, else the most recently active one. */
  function pickChat(chats, current) {
    if (chats.some((chat) => chat.sessionId === current)) return current;
    return chats[0]?.sessionId ?? null;
  }

  function chatLabel(chat) {
    const folder = chat.folder.split('/').filter(Boolean).pop() || chat.folder;
    return `${chat.title} · ${folder}`;
  }

  const api = { userText, historyOf, applyUpdate, finish, pickChat, chatLabel };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SideThread = api;
})(typeof window === 'undefined' ? globalThis : window);
