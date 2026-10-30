'use strict';
(() => {
  const vscode = acquireVsCodeApi();
  const { userText, historyOf, applyUpdate, finish, pickChat, chatLabel } = window.SideThread;
  const { renderMarkdown } = window.SideMarkdown;
  const post = (message) => vscode.postMessage(message);
  const el = (tag, props = {}, children = []) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  };

  // Threads survive the panel being hidden; one per chat, so switching chats keeps each one.
  const state = Object.assign({ sessionId: null, threads: {}, quote: '', source: '' }, vscode.getState());
  let chats = [];
  let asking = null; // { requestId, sessionId }
  const save = () => vscode.setState(state);
  const thread = () => (state.threads[state.sessionId] ??= []);

  const picker = el('select', { title: 'Ask about this chat', onchange: () => select(picker.value) });
  const refresh = el('button', { type: 'button', textContent: '⟳', title: 'Refresh chats', onclick: () => post({ type: 'chats' }) });
  const fresh = el('button', { type: 'button', textContent: 'New', title: 'Start a new side thread for this chat', onclick: () => newThread() });
  const transcript = el('div', { className: 'transcript' });
  const quoteText = el('div', { className: 'quote-text' });
  const quoteBox = el('div', { className: 'quote' }, [
    quoteText,
    el('button', { type: 'button', textContent: '✕', title: 'Remove quote', onclick: () => setQuote('', '') }),
  ]);
  const input = el('textarea', { rows: 3, placeholder: 'Ask on the side… (Enter to send, Shift+Enter for a new line)' });
  const send = el('button', { type: 'button', className: 'primary', textContent: 'Send', onclick: () => (asking ? stop() : ask(input.value)) });
  const status = el('div', { className: 'status' });
  document.body.append(
    el('div', { className: 'toolbar' }, [picker, refresh, fresh]),
    transcript,
    el('div', { className: 'composer' }, [quoteBox, el('div', { className: 'row' }, [input, send]), status]),
  );

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!asking) ask(input.value);
    }
  });

  function setQuote(quote, source) {
    state.quote = quote;
    state.source = source;
    save();
    quoteText.textContent = source ? `${source}\n${quote}` : quote;
    quoteBox.hidden = !quote;
  }

  function select(sessionId) {
    state.sessionId = sessionId;
    save();
    render();
  }

  function newThread() {
    if (asking) return;
    state.threads[state.sessionId] = [];
    save();
    render();
  }

  function render() {
    picker.replaceChildren(...chats.map((chat) => el('option', { value: chat.sessionId, textContent: chatLabel(chat) })));
    picker.value = state.sessionId ?? '';
    picker.disabled = Boolean(asking);
    send.textContent = asking ? 'Stop' : 'Send';
    send.disabled = !asking && !state.sessionId;
    fresh.disabled = Boolean(asking);
    const turns = state.sessionId ? thread() : [];
    if (!turns.length) {
      transcript.replaceChildren(
        el('div', { className: 'empty' }, [
          state.sessionId
            ? 'Ask anything about this chat. Its agent keeps working and never sees this side thread.'
            : 'Open a chat with the Devin Switch agent in this window, then ask here.',
        ]),
      );
      return;
    }
    transcript.replaceChildren(
      ...turns.flatMap((turn) => {
        const answer = el('div', { className: 'answer' });
        answer.innerHTML = renderMarkdown(turn.assistant); // escaped by renderMarkdown
        const note = turn.error || turn.activity || (turn.status === 'stopped' ? 'Stopped.' : '');
        const user = el('div', { className: 'user' });
        user.innerHTML = renderMarkdown(turn.user);
        return [user, answer, ...(note ? [el('div', { className: turn.error ? 'note error' : 'note' }, [note])] : [])];
      }),
    );
    transcript.scrollTop = transcript.scrollHeight;
  }

  function ask(question) {
    if (!question.trim() || !state.sessionId || asking) return;
    const turns = thread();
    const history = historyOf(turns);
    const user = userText(question, state.quote, state.source);
    turns.push({ user, assistant: '', status: 'answering', activity: 'Asking…' });
    const requestId = crypto.randomUUID();
    asking = { requestId, sessionId: state.sessionId };
    input.value = '';
    setQuote('', '');
    status.textContent = '';
    save();
    render();
    post({ type: 'ask', requestId, sessionId: state.sessionId, history, text: user });
  }

  function stop() {
    if (asking) post({ type: 'cancel', sessionId: asking.sessionId });
  }

  function update(requestId, change) {
    if (asking?.requestId !== requestId) return;
    const turns = state.threads[asking.sessionId];
    turns[turns.length - 1] = change(turns.at(-1));
    save();
    if (asking.sessionId === state.sessionId) render();
  }

  window.addEventListener('message', ({ data: message }) => {
    switch (message.type) {
      case 'chats':
        chats = message.chats;
        state.sessionId = pickChat(chats, state.sessionId);
        status.textContent = message.error ?? '';
        save();
        render();
        break;
      case 'quote':
        setQuote(message.text, message.source ?? '');
        input.focus();
        break;
      case 'question':
        // A /btw typed in a chat: answer it in that chat's side thread.
        if (asking) {
          status.textContent = 'Finish or stop the current side answer first.';
          break;
        }
        if (chats.some((chat) => chat.sessionId === message.sessionId)) select(message.sessionId);
        else {
          state.sessionId = message.sessionId;
          post({ type: 'chats' });
        }
        ask(message.text);
        break;
      case 'update':
        update(message.requestId, (turn) => applyUpdate(turn, message.update));
        break;
      case 'done':
        update(message.requestId, (turn) => finish(turn, message.stopReason));
        if (asking?.requestId === message.requestId) asking = null;
        render();
        input.focus();
        break;
      case 'failed':
        update(message.requestId, (turn) => ({ ...turn, status: 'error', activity: '', error: message.error }));
        if (asking?.requestId === message.requestId) asking = null;
        render();
        break;
    }
  });

  setQuote(state.quote, state.source);
  render();
  post({ type: 'ready' });
})();
