'use strict';
// The side page of one chat, shown in a Browser tab of the chat. Questions go to the Switch
// bridge that served it; /btw typed in the chat arrives as a server-sent event.
(() => {
  const { newTurn, historyOf, applyUpdate, finish, thinkingLabel, takeLines } = window.SideThread;
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
  turns = turns.map((t) => (t.status === 'answering' ? finish(t, 'cancelled') : t));
  let asking = false;
  const queued = [];
  // Thinking blocks the reader opened or closed by hand, by turn index.
  const toggled = new Map();
  const save = () => localStorage.setItem(key, JSON.stringify(turns));

  const STARTERS = ['What is this chat about?', 'Summarize what has been done so far', 'What is left to do?', 'Explain the last change'];
  const ICONS = {
    send: '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    stop: '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2.5" fill="currentColor"/></svg>',
    down: '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M12 5v14M5 12l7 7 7-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };
  const NEAR_BOTTOM = 48;
  const MAX_INPUT = 200;

  const column = el('div', { className: 'column' });
  const transcript = el('div', { className: 'transcript' }, [column]);
  const input = el('textarea', { rows: 1, placeholder: 'Ask on the side' });
  const send = el('button', { type: 'button', className: 'send', onclick: () => (asking ? stop() : ask(input.value)) });
  const jump = el('button', { type: 'button', className: 'jump', title: 'Scroll to the latest', hidden: true, onclick: () => toBottom('smooth') });
  jump.innerHTML = ICONS.down;
  const fresh = el('button', { type: 'button', className: 'new', textContent: 'New', title: 'Start a new side thread', onclick: () => newThread() });
  document.body.append(
    el('div', { className: 'toolbar' }, [el('strong', { textContent: 'Side chat' }), fresh]),
    transcript,
    el('div', { className: 'composer' }, [
      jump,
      el('div', { className: 'pill' }, [input, send]),
      el('div', { className: 'footer', textContent: 'The main chat keeps working and never sees this.' }),
    ]),
  );

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!asking) ask(input.value);
    }
  });
  input.addEventListener('input', () => (fitInput(), renderSend()));
  transcript.addEventListener('scroll', () => (jump.hidden = nearBottom()));

  function fitInput() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, MAX_INPUT)}px`;
  }

  function nearBottom() {
    return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < NEAR_BOTTOM;
  }

  function toBottom(behavior = 'auto') {
    transcript.scrollTo({ top: transcript.scrollHeight, behavior });
  }

  function renderSend() {
    send.innerHTML = asking ? ICONS.stop : ICONS.send;
    send.title = asking ? 'Stop' : 'Send';
    send.classList.toggle('stop', asking);
    send.disabled = !asking && !input.value.trim();
  }

  function newThread() {
    if (asking) return;
    turns = [];
    toggled.clear();
    save();
    render();
  }

  /** Redraws the thread; it follows the stream only while the reader is at the bottom. */
  function render(follow = false) {
    renderSend();
    fresh.disabled = asking;
    const stick = follow || nearBottom();
    const top = transcript.scrollTop;
    if (!turns.length) {
      column.replaceChildren(
        el('div', { className: 'empty' }, [
          el('h2', { textContent: 'Ask on the side' }),
          el('p', { textContent: 'Questions here see this chat but never interrupt it. You can also type /btw … in the chat.' }),
          el('div', { className: 'starters' }, STARTERS.map((text) => el('button', { type: 'button', textContent: text, onclick: () => ask(text) }))),
        ]),
      );
      jump.hidden = true;
      return;
    }
    column.replaceChildren(
      ...turns.flatMap((turn, index) => {
        const user = el('div', { className: 'user' });
        user.innerHTML = renderMarkdown(turn.user); // renderMarkdown escapes everything first
        const answer = el('div', { className: 'answer' });
        answer.innerHTML = renderMarkdown(turn.assistant);
        const waiting = turn.status === 'answering' && !turn.assistant && !thinkingLabel(turn);
        const note = turn.error || (turn.status === 'stopped' ? 'Stopped.' : '');
        return [
          user,
          ...(waiting ? [el('div', { className: 'thinking-label live', textContent: 'Thinking' })] : []),
          ...thinkingBlock(turn, index),
          answer,
          ...(note ? [el('div', { className: turn.error ? 'note error' : 'note', textContent: note })] : []),
        ];
      }),
    );
    if (stick) toBottom();
    else transcript.scrollTop = top;
    jump.hidden = nearBottom();
  }

  /** ChatGPT-style thoughts: streamed open while thinking, then folded into "Thought for 6s". */
  function thinkingBlock(turn, index) {
    const label = thinkingLabel(turn);
    if (!label) return [];
    const live = turn.thinkingSince != null && turn.status === 'answering';
    const block = el('details', { className: 'thinking', open: toggled.get(index) ?? live });
    const summary = el('summary', {}, [el('span', { className: live ? 'thinking-label live' : 'thinking-label', textContent: label })]);
    const body = el('div', { className: 'thinking-body' });
    if (turn.thoughts) {
      const thoughts = el('div', { className: 'thoughts' });
      thoughts.innerHTML = renderMarkdown(turn.thoughts);
      body.append(thoughts);
    }
    if (turn.steps?.length) {
      body.append(el('ul', { className: 'steps' }, turn.steps.map((step) => el('li', { className: `step ${step.status}`, textContent: step.title }))));
    }
    block.append(summary, body);
    summary.addEventListener('click', (event) => {
      event.preventDefault();
      toggled.set(index, !block.open);
      block.open = !block.open;
    });
    return [block];
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
    turns.push(newTurn(question.trim()));
    asking = true;
    input.value = '';
    fitInput();
    save();
    render(true);
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
          else if (message.error) (update((turn) => fail(turn, message.error)), (ended = true));
        }
      }
      if (!ended) update((turn) => fail(turn, 'The Switch agent closed before answering. Ask again.'));
    } catch {
      update((turn) => fail(turn, 'Could not reach the Switch agent. Reopen the chat and ask again.'));
    } finally {
      asking = false;
      render();
      input.focus();
      if (queued.length) ask(queued.shift());
    }
  }

  function fail(turn, error) {
    return { ...finish(turn, 'cancelled'), status: 'error', error };
  }

  function stop() {
    fetch(url('cancel'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sessionId: session }) }).catch(() => {});
  }

  // /btw typed in the chat; EventSource reconnects by itself while the bridge runs.
  new EventSource(url('events', `&session=${encodeURIComponent(session)}`)).onmessage = (event) => ask(JSON.parse(event.data).text);

  render(true);
  input.focus();
})();
