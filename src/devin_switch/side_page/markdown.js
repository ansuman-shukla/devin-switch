'use strict';
// Minimal Markdown for side answers: code blocks, inline code, bold, headings, lists, quotes.
// Everything is escaped first, so answer text can never inject markup or scripts.
(function (root) {
  const escape = (text) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function inline(text) {
    return escape(text)
      .split(/(`[^`]+`)/)
      .map((part) => (/^`[^`]+`$/.test(part) ? `<code>${part.slice(1, -1)}</code>` : part.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')))
      .join('');
  }

  function blocks(text) {
    const html = [];
    let list = null;
    const close = () => {
      if (list) html.push(`</${list}>`);
      list = null;
    };
    for (const line of text.split('\n')) {
      const item = /^\s*(?:([-*])|(\d+)[.)])\s+(.*)$/.exec(line);
      if (item) {
        const kind = item[1] ? 'ul' : 'ol';
        if (list !== kind) {
          close();
          html.push(`<${kind}>`);
          list = kind;
        }
        html.push(`<li>${inline(item[3])}</li>`);
        continue;
      }
      close();
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      const quote = /^>\s?(.*)$/.exec(line);
      if (heading) html.push(`<h4>${inline(heading[2])}</h4>`);
      else if (quote) html.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      else if (line.trim()) html.push(`<p>${inline(line)}</p>`);
    }
    close();
    return html.join('');
  }

  function renderMarkdown(text) {
    // An unfinished fence (still streaming) renders as code until it closes.
    return text
      .split(/^```[^\n]*\n?/m)
      .map((part, index) => (index % 2 ? `<pre><code>${escape(part.replace(/\n$/, ''))}</code></pre>` : blocks(part)))
      .join('');
  }

  const api = { renderMarkdown };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SideMarkdown = api;
})(typeof window === 'undefined' ? globalThis : window);
