'use strict';
// The Pull requests page of one chat, shown in a Browser tab of the chat. The Switch bridge that served it sends
// the chat's pull requests again on every change.
(() => {
  const view = window.PrView;
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
  // The glyph only repeats the words next to it, so a screen reader skips it.
  const icon = (tone) => el('span', { className: `icon ${tone}`, textContent: view.ICONS[tone], ariaHidden: 'true' });
  const lines = (className, texts) => texts.filter(Boolean).map((textContent) => el('p', { className, textContent }));

  // The latest snapshot's pull requests, and what the reader did to each one on this page, by url.
  let pulls = [];
  let states = new Map();
  // Card elements by url, reused across snapshots.
  const cards = new Map();
  // Cards are numbered: a url is not safe as the element id that aria-describedby needs.
  let numbered = 0;

  const updated = el('span', { className: 'updated' });
  const banner = el('div', { className: 'banner', role: 'status', hidden: true });
  const notice = el('div', { className: 'banner notice', ariaLive: 'polite', hidden: true });
  const list = el('ul', { className: 'cards' });
  const empty = el('div', { className: 'empty', hidden: true }, [
    el('h2', { textContent: 'No pull requests in this chat yet.' }),
    el('p', { textContent: 'Paste a PR link in the chat, or type /pr <link or number>.' }),
  ]);
  document.body.append(
    el('header', { className: 'toolbar' }, [el('h1', { textContent: 'Pull requests' }), updated]),
    banner,
    notice,
    el('main', { className: 'content' }, [el('div', { className: 'column' }, [empty, list])]),
  );

  function newCard(url) {
    const card = { url, number: ++numbered, json: '', controls: new Map(), focus: null };
    card.body = el('div', { className: 'card-body' });
    // The card takes the keyboard focus when the control that had it is gone or switched off.
    card.node = el('li', { className: 'card', tabIndex: -1 }, [card.body]);
    cards.set(url, card);
    return card;
  }

  /** Every control has a key, so the keyboard focus can go back to it after its card was drawn again. */
  function control(card, key, node) {
    node.dataset.key = key;
    card.controls.set(key, node);
    return node;
  }
  const button = (card, key, textContent, onclick, props = {}) => control(card, key, el('button', { type: 'button', className: 'button', textContent, onclick, ...props }));

  function statusRow({ tone, text }, beside = [], below = []) {
    const line = el('div', { className: 'status-line' }, [el('span', { textContent: text }), ...beside]);
    return el('li', { className: 'status' }, [icon(tone), el('div', { className: 'status-body' }, [line, ...below])]);
  }

  function checkLine(check) {
    const workflow = check.workflow ? [' ', el('span', { className: 'workflow', textContent: check.workflow })] : [];
    return el('div', { className: 'check' }, [icon(check.tone), el('span', { className: 'check-name' }, [check.name, ...workflow]), el('span', { className: 'check-word', textContent: check.word })]);
  }

  function checksRow(card, checks) {
    if (!checks.toggle) return statusRow(checks);
    const toggle = button(card, 'checks', checks.toggle, () => change(card.url, (state) => view.showChecks(state, !checks.open)), {
      className: 'toggle',
      ariaExpanded: String(checks.open),
    });
    const items = checks.list.map((check) => el('li', {}, [checkLine(check)]));
    return statusRow(checks, [toggle], checks.open ? [el('ul', { className: 'checks' }, items)] : []);
  }

  function statusList(card, { behind, conflicts, checks, review }) {
    const reviewed = lines('quiet', [review.reviewers.join(' · '), review.waiting]);
    return el('ul', { className: 'statuses' }, [statusRow(behind), statusRow(conflicts), checksRow(card, checks), statusRow(review, [], reviewed)]);
  }

  /** A card's elements from its view model. */
  function parts(card, model) {
    const pill = model.pill ? [el('span', { className: `pill ${model.pill.kind}`, textContent: model.pill.text })] : [];
    const meta = model.meta.map(({ kind, text }) => el('span', { className: kind, textContent: text }));
    return [
      el('div', { className: 'head' }, [el('span', { className: 'repo', textContent: model.heading }), ...pill]),
      // A long title is clamped; the tooltip keeps the full text.
      model.title && el('h2', { className: 'title', textContent: model.title, title: model.title }),
      model.note && el('p', { className: `note ${model.kind}` }, model.kind === 'error' ? [icon('bad'), model.note] : [model.note]),
      meta.length > 0 && el('div', { className: 'meta' }, meta),
      model.status && statusList(card, model.status),
    ].filter(Boolean);
  }

  /** Draws a card again only when what it shows changed, so a selection in an unchanged card stays. */
  function draw(card, pull) {
    const model = view.cardOf(pull, states.get(pull.url));
    const json = JSON.stringify(model);
    if (json !== card.json) {
      const focused = card.body.contains(document.activeElement) ? document.activeElement.dataset.key : null;
      card.controls.clear();
      card.node.className = `card ${model.kind}`;
      card.body.replaceChildren(...parts(card, model));
      card.json = json;
      const target = card.controls.get(card.focus ?? focused);
      if (target && !target.disabled) target.focus({ preventScroll: true });
      else if (focused) card.node.focus({ preventScroll: true });
    }
    card.focus = null;
    return card.node;
  }

  function render() {
    empty.hidden = pulls.length > 0;
    const urls = new Set(pulls.map(({ url }) => url));
    for (const url of cards.keys()) if (!urls.has(url)) cards.delete(url);
    const nodes = pulls.map((pull) => draw(cards.get(pull.url) ?? newCard(pull.url), pull));
    // Cards move only when the list changed, so a selection inside one survives the other updates.
    if (nodes.length !== list.children.length || nodes.some((node, index) => node !== list.children[index])) {
      // Taking cards out and putting them back loses the keyboard focus.
      const focused = list.contains(document.activeElement) ? document.activeElement : null;
      list.replaceChildren(...nodes);
      if (focused?.isConnected) focused.focus({ preventScroll: true });
    }
  }

  function show(snapshot) {
    pulls = snapshot.pulls;
    states = view.reconcileAll(states, pulls);
    setText(updated, view.updatedLabel(new Date()));
    notice.hidden = !snapshot.notice;
    setText(notice, snapshot.notice ?? '');
    render();
  }

  /** Applies what the reader did to a pull request that is still listed; `focus` is the key of the control the keyboard goes to. */
  function change(url, update, focus = null) {
    if (!states.has(url)) return;
    states.set(url, update(states.get(url)));
    cards.get(url).focus = focus;
    render();
  }

  const source = new EventSource(`/pr/events?session=${encodeURIComponent(session)}&t=${encodeURIComponent(token)}`);
  source.onmessage = (event) => {
    banner.hidden = true;
    show(JSON.parse(event.data));
  };
  // EventSource retries by itself unless the bridge refused it; the cards stay as they were meanwhile.
  source.onerror = () => {
    banner.textContent = source.readyState === EventSource.CLOSED ? 'Disconnected. Type /pr in the chat to open this page again.' : 'Reconnecting…';
    banner.hidden = false;
  };
})();
