'use strict';
// The Subagents page of one chat, shown in a Browser tab of the chat. It only watches: the Switch
// bridge that served it sends the whole list again on every change.
(() => {
  const { isActive, timeLabel, titleOf, metaOf, detailOf, countLine, ordered } = window.SubagentsView;
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

  // Row elements by subagent id, reused across snapshots.
  const items = new Map();

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
    el('div', { className: 'footer', textContent: 'Shows subagents started since this chat was opened in this window.' }),
  );

  function newItem(id) {
    const item = {
      title: el('span', { className: 'title' }),
      pill: el('span', { className: 'pill' }),
      meta: el('span', { className: 'meta' }),
      time: el('span', { className: 'time' }),
      detail: el('div', { className: 'detail' }),
    };
    item.node = el('li', { className: 'row' }, [
      el('div', { className: 'head' }, [item.title, item.pill]),
      el('div', { className: 'sub' }, [item.meta, item.time]),
      item.detail,
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
    return item.node;
  }

  function show(snapshot) {
    const rows = ordered(snapshot.subagents);
    setText(counts, countLine(rows));
    empty.hidden = rows.length > 0;
    const ids = new Set(rows.map((row) => row.id));
    for (const id of items.keys()) if (!ids.has(id)) items.delete(id);
    const nodes = rows.map((row) => fill(items.get(row.id) ?? newItem(row.id), row));
    // Rows move only when the order changed, so a selection inside one survives the other updates.
    if (nodes.length !== list.children.length || nodes.some((node, index) => node !== list.children[index])) list.replaceChildren(...nodes);
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
