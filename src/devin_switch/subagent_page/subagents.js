'use strict';
// The Subagents page of one chat, shown in a Browser tab of the chat. It only watches: the Switch
// bridge that served it sends the whole list again on every change, and a subagent's reply on request.
(() => {
  const { isActive, timeLabel, titleOf, metaOf, detailOf, countLine, ordered, replyChars, hasReply, replyLabel, replySize, needsReply } = window.SubagentsView;
  const { renderMarkdown } = window.SideMarkdown;
  const { session, token } = document.body.dataset;
  const el = (tag, props = {}, children = []) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  };
  // Writing the same text again would drop a selection the reader made in it.
  const setText = (node, text) => {
    if (node.textContent !== text) node.textContent = text;
  };
  // Titles, steps and notes are clamped; the tooltip keeps the full text.
  const setClamped = (node, text) => {
    setText(node, text);
    node.title = text;
  };
  const now = () => Date.now() / 1000;
  const NEAR_BOTTOM = 48;
  const LOADING = 'Loading reply…';
  const GONE = 'This reply is no longer available.';

  // Row elements by subagent id, reused across snapshots.
  const items = new Map();
  // Reply boxes are numbered: a subagent id is not safe as the element id that aria-controls needs.
  let boxes = 0;

  const counts = el('span', { className: 'counts' });
  const banner = el('div', { className: 'banner', role: 'status', hidden: true });
  const list = el('ul', { className: 'rows' });
  const empty = el('div', { className: 'empty', hidden: true }, [
    el('h2', { textContent: 'No subagents yet' }),
    el('p', { textContent: 'Subagents this chat starts will show up here while it works.' }),
  ]);
  document.body.append(
    el('header', { className: 'toolbar' }, [el('h1', { textContent: 'Subagents' }), counts]),
    banner,
    el('main', { className: 'content' }, [el('div', { className: 'column' }, [empty, list])]),
    el('div', { className: 'footer', textContent: 'Shows subagents started since this chat was opened in this window. Their replies are shown here instead of in the chat.' }),
  );

  function newItem(id) {
    const item = {
      id,
      title: el('span', { className: 'title' }),
      pill: el('span', { className: 'pill' }),
      meta: el('span', { className: 'meta' }),
      time: el('span', { className: 'time' }),
      detail: el('div', { className: 'detail' }),
      toggle: el('button', { type: 'button', className: 'reply-toggle', onclick: () => toggleReply(item) }),
      size: el('span', { className: 'reply-size' }),
      box: el('div', { className: 'reply', id: `reply-${++boxes}`, hidden: true }),
      // Whether the reader expanded the reply, the reply_chars its latest request was made at, whether
      // that request is still out, and the Markdown the box shows (null: a note line, or nothing yet).
      open: false,
      asked: null,
      waiting: false,
      text: null,
    };
    item.toggle.setAttribute('aria-controls', item.box.id);
    item.bar = el('div', { className: 'reply-bar' }, [item.toggle, item.size]);
    item.node = el('li', { className: 'row' }, [
      el('div', { className: 'head' }, [item.title, item.pill]),
      el('div', { className: 'sub' }, [item.meta, item.time]),
      item.detail,
      item.bar,
      item.box,
    ]);
    items.set(id, item);
    return item;
  }

  function fill(item, row) {
    item.row = row;
    item.node.className = isActive(row) ? 'row active' : 'row';
    setClamped(item.title, titleOf(row));
    item.pill.className = `pill ${row.status}`;
    setText(item.pill, row.status);
    setText(item.meta, metaOf(row));
    setText(item.time, timeLabel(row, now()));
    item.detail.className = `detail ${row.status}`;
    setClamped(item.detail, detailOf(row));
    syncReply(item);
    return item.node;
  }

  /** Brings a row's toggle and reply box in line with its row and state, and asks for the reply when one is due. */
  function syncReply(item) {
    const { row } = item;
    const has = hasReply(row);
    // A reply that went away (the subagent started again) leaves no toggle to close its box with.
    if (!has) Object.assign(item, { open: false, text: null });
    item.bar.hidden = !has;
    setText(item.toggle, replyLabel(row, item.open));
    item.toggle.setAttribute('aria-expanded', String(item.open));
    setText(item.size, replySize(row));
    item.box.hidden = !item.open;
    if (needsReply(row, item)) request(item);
  }

  function toggleReply(item) {
    item.open = !item.open;
    if (item.open) {
      // Every expand asks again, also after a failed request; one that is still out is waited for.
      if (!item.waiting) item.asked = null;
      if (item.text == null) showNote(item, LOADING);
    }
    syncReply(item);
  }

  function request(item) {
    item.asked = replyChars(item.row);
    item.waiting = true;
    fetchReply(item.id)
      .catch(() => null)
      .then((text) => settle(item, text));
  }

  /** A subagent's reply as Markdown; null when the bridge no longer has it. */
  async function fetchReply(id) {
    const response = await fetch(`/subagents/reply?session=${encodeURIComponent(session)}&agent=${encodeURIComponent(id)}&t=${encodeURIComponent(token)}`);
    const text = response.ok ? (await response.json()).reply : null;
    return typeof text === 'string' ? text : null;
  }

  function settle(item, text) {
    item.waiting = false;
    // A row that is gone, or that the reader collapsed meanwhile, ignores the late answer.
    if (items.get(item.id) !== item || !item.open) return;
    if (text == null) showNote(item, GONE);
    else showReply(item, text);
    // reply_chars may have moved on while the request was out.
    syncReply(item);
  }

  function showNote(item, text) {
    item.text = null;
    item.box.replaceChildren(el('p', { className: 'reply-note', textContent: text }));
  }

  function showReply(item, text) {
    // The same text again leaves the box alone, so a selection in it and its scroll position stay.
    if (text === item.text) return;
    const { box } = item;
    // A reader at the bottom of a reply that only grew keeps following it.
    const follow = item.text != null && text.startsWith(item.text) && box.scrollHeight - box.scrollTop - box.clientHeight < NEAR_BOTTOM;
    const top = box.scrollTop;
    box.innerHTML = renderMarkdown(text); // renderMarkdown escapes everything first
    box.scrollTop = follow ? box.scrollHeight : top;
    item.text = text;
  }

  function show(snapshot) {
    const rows = ordered(snapshot.subagents);
    setText(counts, countLine(rows));
    empty.hidden = rows.length > 0;
    const ids = new Set(rows.map((row) => row.id));
    for (const id of items.keys()) if (!ids.has(id)) items.delete(id);
    const nodes = rows.map((row) => fill(items.get(row.id) ?? newItem(row.id), row));
    // Rows move only when the order changed, so a selection inside one survives the other updates.
    if (nodes.length !== list.children.length || nodes.some((node, index) => node !== list.children[index])) {
      // Taking rows out and putting them back loses the keyboard focus and where each reply box was scrolled to.
      const focused = list.contains(document.activeElement) ? document.activeElement : null;
      const tops = [...items.values()].filter((item) => item.open).map((item) => [item.box, item.box.scrollTop]);
      list.replaceChildren(...nodes);
      for (const [box, top] of tops) box.scrollTop = top;
      if (focused?.isConnected) focused.focus({ preventScroll: true });
    }
  }

  /** The one-second tick touches nothing but the counters of active rows. */
  function tick() {
    for (const item of items.values()) if (isActive(item.row)) setText(item.time, timeLabel(item.row, now()));
  }

  const source = new EventSource(`/subagents/events?session=${encodeURIComponent(session)}&t=${encodeURIComponent(token)}`);
  source.onmessage = (event) => {
    banner.hidden = true;
    show(JSON.parse(event.data));
  };
  // EventSource retries by itself unless the bridge refused it; the rows stay as they were meanwhile.
  source.onerror = () => {
    banner.textContent = source.readyState === EventSource.CLOSED ? 'Disconnected. Type /subagents in the chat to open this page again.' : 'Reconnecting…';
    banner.hidden = false;
  };
  setInterval(tick, 1000);
})();
