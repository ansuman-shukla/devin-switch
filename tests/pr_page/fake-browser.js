'use strict';
// Just enough of a browser for pr.js: a tree of elements, an event stream and fetch, all driven by the tests.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const PAGE = path.join(__dirname, '../../src/devin_switch/pr_page');
const SCRIPTS = ['pr-view.js', 'pr.js'].map((name) => new vm.Script(fs.readFileSync(path.join(PAGE, name), 'utf8'), { filename: name }));

class Element {
  constructor(document, tagName) {
    Object.assign(this, { document, tagName, parent: null, nodes: [], dataset: {}, attributes: {}, className: '', hidden: false, disabled: false });
  }
  /** Strings are text nodes; an element moves out of its old parent. */
  append(...nodes) {
    for (const node of nodes) {
      if (node instanceof Element) {
        if (node.parent) node.parent.nodes = node.parent.nodes.filter((other) => other !== node);
        node.parent = this;
      }
      this.nodes.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const node of this.children) node.parent = null;
    this.nodes = [];
    this.append(...nodes);
  }
  get children() {
    return this.nodes.filter((node) => node instanceof Element);
  }
  get textContent() {
    return this.nodes.map((node) => (node instanceof Element ? node.textContent : node)).join('');
  }
  set textContent(text) {
    this.replaceChildren(String(text));
  }
  set innerHTML(html) {
    throw new Error('the page must never parse HTML');
  }
  contains(node) {
    for (let at = node; at; at = at.parent) if (at === this) return true;
    return false;
  }
  get isConnected() {
    return this.document.body.contains(this);
  }
  focus() {
    if (this.isConnected && !this.disabled) this.document.focused = this;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  /** What a click or a keyboard activation does: nothing on a disabled control. */
  click() {
    if (this.disabled) return;
    this.focus();
    if (this.tagName === 'input') this.onchange?.({});
    else this.onclick?.({});
  }
}

/** Loads the page as the bridge serves it: an empty <body> with the chat and token, then the two scripts. */
function openPage({ session = 'chat 1', token = 't&k' } = {}) {
  const document = {
    focused: null,
    createElement: (tagName) => new Element(document, tagName),
    /** A control that left the page or was switched off no longer has the focus. */
    get activeElement() {
      return this.focused?.isConnected && !this.focused.disabled ? this.focused : this.body;
    },
  };
  document.body = new Element(document, 'body');
  document.body.dataset = { session, token };
  const page = { document, body: document.body, requests: [], source: null };
  class EventSource {
    static CLOSED = 2;
    constructor(url) {
      Object.assign(this, { url, readyState: 1 });
      page.source = this;
    }
  }
  const fetch = (url, options) => new Promise((resolve, reject) => page.requests.push({ url, options, body: JSON.parse(options.body), resolve, reject }));
  const context = vm.createContext({ document, EventSource, fetch });
  context.window = context;
  for (const script of SCRIPTS) script.runInContext(context);

  const settle = () => new Promise((resolve) => setImmediate(resolve));
  Object.assign(page, {
    /** The bridge sends a snapshot. */
    send: (snapshot) => page.source.onmessage({ data: JSON.stringify(snapshot) }),
    /** The bridge answers the oldest request that is still out. */
    async answer(reply, { status = 200 } = {}) {
      page.requests.find((request) => !request.done && (request.done = true)).resolve({ ok: status === 200, status, json: async () => reply });
      await settle();
    },
    async fail() {
      page.requests.find((request) => !request.done && (request.done = true)).reject(new TypeError('Failed to fetch'));
      await settle();
    },
    /** Elements a reader can see, in page order. */
    seen: (root = document.body) => (root.hidden ? [] : [root, ...root.children.flatMap(page.seen)]),
    /** The text a reader can see under an element. */
    text: (root = document.body) => (root.hidden ? '' : root.nodes.map((node) => (node instanceof Element ? page.text(node) : node)).join('')),
    all: (className, root) => page.seen(root).filter((node) => node.className.split(' ').includes(className)),
    one(className, root) {
      const found = page.all(className, root);
      if (found.length !== 1) throw new Error(`${found.length} visible .${className}`);
      return found[0];
    },
    control(key, root) {
      const found = page.seen(root).filter((node) => node.dataset.key === key);
      if (found.length !== 1) throw new Error(`${found.length} visible controls "${key}"`);
      return found[0];
    },
    has: (key, root) => page.seen(root).some((node) => node.dataset.key === key),
  });
  return page;
}

module.exports = { openPage };
