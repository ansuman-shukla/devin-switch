'use strict';
// The side page of one chat, shown in Desktop's in-chat preview. Questions go to the Switch
// bridge that served it; /btw typed in the chat arrives as a server-sent event.
(() => {
  const { historyOf, applyUpdate, finish, takeLines } = window.SideThread;
  const { renderMarkdown } = window.SideMarkdown;
  const { session, token } = document.body.dataset;
  const url = (path, extra = '') => `/side/${path}?t=${encodeURIComponent(token)}${extra}`;
  const key = `switch-side:${session}`;
  const el = (tag, props = {}, children = []) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  };

  let turns = [];
  try {
    turns = JSON.parse(localStorage.getItem(key) || '[]');
  } catch {}
  // A page reloaded mid-answer can't resume it.
  turns = turns.map((t) => (t.status === 'answering' ? { ...t, status: 'stopped', activity: '' } : t));
  let asking = false;
  const queued = [];
  const save = () => localStorage.setItem(key, JSON.stringify(turns));

  const transcript = el('div', { className: 'transcript' });
  const input = el('textarea', { rows: 3, placeholder: 'Ask on the side… (Enter to send, Shift+Enter for a new line)' });
  const send = el('button', { type: 'button', className: 'primary', textContent: 'Send', onclick: () => (asking ? stop() : ask(input.value)) });
  const fresh = el('button', { type: 'button', textContent: 'New', title: 'Start a new side thread', onclick: () => newThread() });
  document.body.append(
    el('div', { className: 'toolbar' }, [el('strong', { textContent: 'Side chat' }), el('span', { className: 'hint', textContent: 'The chat keeps working and never sees this.' }), fresh]),
    transcript,
    el('div', { className: 'composer' }, [el('div', { className: 'row' }, [input, send])]),
  );

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!asking) ask(input.value);
    }
  });

  function newThread() {
    if (asking) return;
    turns = [];
    save();
    render();
  }

  function render() {
    send.textContent = asking ? 'Stop' : 'Send';
    fresh.disabled = asking;
    if (!turns.length) {
      transcript.replaceChildren(el('div', { className: 'empty' }, ['Ask anything about this chat, or type /btw … in the chat itself.']));
      return;
    }
    transcript.replaceChildren(
      ...turns.flatMap((turn) => {
        const user = el('div', { className: 'user' });
        user.innerHTML = renderMarkdown(turn.user); // renderMarkdown escapes everything first
        const answer = el('div', { className: 'answer' });
        answer.innerHTML = renderMarkdown(turn.assistant);
        const note = turn.error || turn.activity || (turn.status === 'stopped' ? 'Stopped.' : '');
        return [user, answer, ...(note ? [el('div', { className: turn.error ? 'note error' : 'note', textContent: note })] : [])];
      }),
    );
    transcript.scrollTop = transcript.scrollHeight;
  }

  function update(change) {
    turns[turns.length - 1] = change(turns.at(-1));
    save();
    render();
  }

  async function ask(question) {
    if (!question.trim()) return;
    if (asking) return void queued.push(question);
    const history = historyOf(turns);
    turns.push({ user: question.trim(), assistant: '', status: 'answering', activity: 'Asking…' });
    asking = true;
    input.value = '';
    save();
    render();
    try {
      const response = await fetch(url('ask'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sessionId: session, history, text: question.trim() }) });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let ended = false;
      while (!ended) {
        const { value, done } = await reader.read();
        if (done) break;
        const { messages, rest } = takeLines(buffer + decoder.decode(value, { stream: true }));
        buffer = rest;
        for (const message of messages) {
          if (message.update) update((turn) => applyUpdate(turn, message.update));
          else if (message.done) (update((turn) => finish(turn, message.done)), (ended = true));
          else if (message.error) (update((turn) => ({ ...turn, status: 'error', activity: '', error: message.error })), (ended = true));
        }
      }
      if (!ended) update((turn) => ({ ...turn, status: 'error', activity: '', error: 'The Switch agent closed before answering. Ask again.' }));
    } catch {
      update((turn) => ({ ...turn, status: 'error', activity: '', error: 'Could not reach the Switch agent. Reopen the chat and ask again.' }));
    } finally {
      asking = false;
      render();
      input.focus();
      if (queued.length) ask(queued.shift());
    }
  }

  function stop() {
    fetch(url('cancel'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sessionId: session }) }).catch(() => {});
  }

  // /btw typed in the chat; EventSource reconnects by itself while the bridge runs.
  new EventSource(url('events', `&session=${encodeURIComponent(session)}`)).onmessage = (event) => ask(JSON.parse(event.data).text);

  render();
  input.focus();
})();
