'use strict';
// The Pull requests page of one chat, shown in a Browser tab of the chat. The Switch bridge that served it sends
// the chat's pull requests again on every change and runs each action through the gh CLI on this Mac.
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
  let refreshing = false;

  const refresh = el('button', { type: 'button', className: 'button small', textContent: 'Refresh', onclick: refreshAll });
  const updated = el('span', { className: 'updated' });
  const banner = el('div', { className: 'banner', role: 'status', hidden: true });
  const notice = el('div', { className: 'banner notice', ariaLive: 'polite', hidden: true });
  const trouble = newProblem(() => trouble.show(null));
  const list = el('ul', { className: 'cards' });
  const empty = el('div', { className: 'empty', hidden: true }, [
    el('h2', { textContent: 'No pull requests in this chat yet.' }),
    el('p', { textContent: 'Paste a PR link in the chat, or type /pr <link or number>.' }),
  ]);
  document.body.append(
    el('header', { className: 'toolbar' }, [el('h1', { textContent: 'Pull requests' }), refresh, updated]),
    banner,
    notice,
    el('main', { className: 'content' }, [el('div', { className: 'column' }, [trouble.node, empty, list])]),
  );

  /** A line that stays in the page across redraws, so a screen reader hears its text change. Empty, it is hidden. */
  function newLine(className, tone, extras = []) {
    const text = el('span', { className: `${className}-text`, ariaLive: 'polite' });
    const node = el('div', { className, hidden: true }, [icon(tone), text, ...extras]);
    return {
      node,
      show(message) {
        node.hidden = !message;
        setText(text, message ?? '');
      },
    };
  }

  /** The error of an action, until the reader hides it. */
  function newProblem(hide) {
    return newLine('problem', 'bad', [el('button', { type: 'button', className: 'close', textContent: '×', ariaLabel: 'Hide this error', onclick: hide })]);
  }

  function newCard(url) {
    const card = { url, number: ++numbered, json: '', controls: new Map(), focus: null };
    card.body = el('div', { className: 'card-body' });
    // A refresh that failed is told apart from an action that failed: it goes away with the next good snapshot.
    card.stale = newLine('stale', 'warn');
    card.problem = newProblem(() => change(url, view.hideError));
    // The card takes the keyboard focus when the control that had it is gone or switched off.
    card.node = el('li', { className: 'card', tabIndex: -1 }, [card.body, card.stale.node, card.problem.node]);
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

  /** A check with a link opens it; the others are plain lines. */
  function checkLine(card, check, index) {
    const workflow = check.workflow ? [' ', el('span', { className: 'workflow', textContent: check.workflow })] : [];
    const children = [icon(check.tone), el('span', { className: 'check-name' }, [check.name, ...workflow]), el('span', { className: 'check-word', textContent: check.word })];
    if (!check.url) return el('div', { className: 'check' }, children);
    return control(card, `check-${index}`, el('button', { type: 'button', className: 'check', title: 'Open this check in the browser', onclick: () => openLink(card.url, check.url) }, children));
  }

  function checksRow(card, checks) {
    if (!checks.toggle) return statusRow(checks);
    const toggle = button(card, 'checks', checks.toggle, () => change(card.url, (state) => view.showChecks(state, !checks.open)), {
      className: 'toggle',
      ariaExpanded: String(checks.open),
    });
    const items = checks.list.map((check, index) => el('li', {}, [checkLine(card, check, index)]));
    return statusRow(checks, [toggle], checks.open ? [el('ul', { className: 'checks' }, items)] : []);
  }

  function statusList(card, { behind, conflicts, checks, review }) {
    const reviewed = lines('sub', [review.reviewers.join(' · '), review.waiting]);
    return el('ul', { className: 'statuses' }, [statusRow(behind), statusRow(conflicts), checksRow(card, checks), statusRow(review, [], reviewed)]);
  }

  /** The step between Merge and the merge: only Confirm merge sends it. */
  function confirmStep(card, confirm) {
    const { url } = card;
    const radio = ({ value, label, chosen }) => {
      const input = el('input', { type: 'radio', name: `method-${card.number}`, value, checked: chosen, onchange: () => change(url, (state) => view.chooseMethod(state, value)) });
      return el('label', {}, [control(card, `method-${value}`, input), label]);
    };
    const methods = confirm.chooser
      ? [el('fieldset', { className: 'methods', ariaLabel: 'Merge method' }, confirm.methods.map(radio))]
      : lines('sub', confirm.methods.map(({ label }) => label));
    const warning = confirm.warning ? [el('p', { className: 'warning' }, [icon('warn'), confirm.warning])] : [];
    const buttons = [
      button(card, 'confirm-merge', 'Confirm merge', () => act(url, 'merge', { method: confirm.method }), { className: 'button primary' }),
      button(card, 'cancel', 'Cancel', () => change(url, view.cancelMerge, 'merge')),
    ];
    const step = el('div', { className: 'confirm', role: 'group', ariaLabel: 'Confirm merge', tabIndex: -1 }, [...methods, ...warning, el('div', { className: 'buttons' }, buttons)]);
    return control(card, 'confirm', step);
  }

  /** The buttons the server allows, why Merge is off, and the link out, which every card has. */
  function actionRow(card, model) {
    const { url } = card;
    const reason = `reason-${card.number}`;
    const buttons = model.actions.flatMap(({ action, label, enabled }) => {
      if (action !== 'merge') return [button(card, action, label, () => act(url, action), { disabled: !enabled })];
      // The first click only opens the confirm step, which takes the keyboard focus.
      const merge = button(card, action, label, () => change(url, view.startMerge, 'confirm'), { className: 'button primary', disabled: !enabled });
      if (!model.reason) return [merge];
      merge.setAttribute('aria-describedby', reason);
      return [merge, el('span', { className: 'reason', id: reason, textContent: model.reason })];
    });
    return el('div', { className: 'actions' }, [...buttons, button(card, 'open', 'Open on GitHub', () => openLink(url), { className: 'link' })]);
  }

  function metaLine({ branch, author, added, removed, files }) {
    const part = (className, textContent) => el('span', { className, textContent });
    // The two halves of the diff stay on one line.
    const diff = el('span', { className: 'diff' }, [part('added', added), ' ', part('removed', removed)]);
    return el('div', { className: 'meta' }, [part('branch', branch), part('author', author), diff, part('files', files)]);
  }

  /** A card's elements from its view model. */
  function parts(card, model) {
    const { url } = card;
    const dismiss = button(card, 'dismiss', '×', () => act(url, 'dismiss'), { className: 'close', ariaLabel: 'Remove from this tab', disabled: model.locked });
    const pill = model.pill ? [el('span', { className: `pill ${model.pill.kind}`, textContent: model.pill.text })] : [];
    return [
      el('div', { className: 'head' }, [el('span', { className: 'repo', textContent: model.heading }), ...pill, dismiss]),
      // A long title is clamped; the tooltip keeps the full text.
      model.title && el('h2', { className: 'title', textContent: model.title, title: model.title }),
      model.note && el('p', { className: `note ${model.kind}` }, model.kind === 'error' ? [icon('bad'), model.note] : [model.note]),
      model.meta && metaLine(model.meta),
      model.status && statusList(card, model.status),
      model.confirm && confirmStep(card, model.confirm),
      actionRow(card, model),
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
      card.stale.show(model.stale);
      card.problem.show(model.error);
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

  /** Asks the bridge for one action. Resolves to null when it went through, else to the line to show. */
  function post(body) {
    return fetch(`/pr/act?t=${encodeURIComponent(token)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then((response) => (response.ok ? response.json() : null))
      .catch(() => null)
      .then(view.replyError);
  }

  /** Runs an action on a pull request; its card is locked until the answer is back. */
  function act(url, action, extras = {}) {
    change(url, (state) => view.began(state, action));
    post(view.actBody(session, action, { url, ...extras })).then((error) => change(url, (state) => view.ended(state, error)));
  }

  /** Outside links open in the Mac's default browser, through the bridge: this tab never leaves the page. */
  function openLink(url, target) {
    post(view.actBody(session, 'open', { url, target })).then((error) => change(url, (state) => view.opened(state, error)));
  }

  function refreshAll() {
    // The button stays focusable while the request is out, so it is switched off by hand.
    if (refreshing) return;
    const set = (on) => {
      refreshing = on;
      refresh.ariaDisabled = String(on);
      setText(refresh, on ? 'Refreshing…' : 'Refresh');
    };
    set(true);
    post(view.actBody(session, 'refresh')).then((error) => {
      set(false);
      trouble.show(error);
    });
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
