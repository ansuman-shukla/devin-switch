'use strict';
// How a chat's subagents are ordered and worded, shared by the Subagents page (browser) and the node tests.
(function (root) {
  const ACTIVE = ['running', 'paused'];
  const COUNTED = [...ACTIVE, 'done', 'failed', 'stopped'];
  const FINISHED = { done: 'done in', failed: 'failed after', stopped: 'stopped after' };

  const isActive = (row) => ACTIVE.includes(row.status);
  /** Times are epoch seconds; a missing or broken one is unknown. */
  const known = (time) => (Number.isFinite(time) ? time : null);

  /** "12s", "4m 12s" or "1h 04m"; never negative. */
  function formatDuration(seconds) {
    const total = Math.max(0, Math.floor(known(seconds) ?? 0));
    const pad = (part) => String(part).padStart(2, '0');
    if (total < 60) return `${total}s`;
    if (total < 3600) return `${Math.floor(total / 60)}m ${pad(total % 60)}s`;
    return `${Math.floor(total / 3600)}h ${pad(Math.floor((total % 3600) / 60))}m`;
  }

  /** "running 4m 12s" counts from the start to `now` (epoch seconds); "done in 2m 05s" is the whole run. Empty when a time is unknown. */
  function timeLabel(row, now) {
    const active = isActive(row);
    const start = known(row.started);
    const end = known(active ? now : row.ended);
    if (start == null || end == null) return '';
    return `${active ? row.status : (FINISHED[row.status] ?? row.status)} ${formatDuration(end - start)}`;
  }

  const titleOf = (row) => row.title.trim() || row.profile.trim() || 'Subagent';

  /** "General · background"; the profile is left out when it already stands in for the title. */
  function metaOf(row) {
    const mode = row.background ? 'background' : 'foreground';
    const profile = row.profile.trim();
    return row.title.trim() && profile ? `${profile} · ${mode}` : mode;
  }

  /** What an active row is doing now, or why a failed or stopped one ended. */
  function detailOf(row) {
    if (isActive(row)) return row.step.trim();
    return row.status === 'failed' || row.status === 'stopped' ? row.note.trim() : '';
  }

  /** "3 running · 1 done": only the statuses that have rows. */
  function countLine(rows) {
    return COUNTED.map((status) => [rows.filter((row) => row.status === status).length, status])
      .filter(([count]) => count)
      .map(([count, status]) => `${count} ${status}`)
      .join(' · ');
  }

  /**
   * Active rows first, newest start first; then finished rows, latest end first. The snapshot is in
   * start order, so on a tie the later row is the newer one and goes on top.
   */
  function ordered(rows) {
    return rows
      .map((row, index) => {
        const active = isActive(row);
        return { row, index, active, at: (active ? null : known(row.ended)) ?? known(row.started) ?? -Infinity };
      })
      .sort((a, b) => b.active - a.active || b.at - a.at || b.index - a.index)
      .map(({ row }) => row);
  }

  /** How much the subagent has said so far, in characters; anything but a positive whole number is no reply. */
  const replyChars = (row) => (Number.isInteger(row.reply_chars) && row.reply_chars > 0 ? row.reply_chars : 0);
  const hasReply = (row) => replyChars(row) > 0;

  /** The reply toggle's text; an active row is still writing, so its reply is not final. */
  function replyLabel(row, expanded) {
    return `${expanded ? 'Hide' : 'Show'} reply${isActive(row) ? ' so far' : ''}`;
  }

  /** "1 character", "999 characters", "3.7k characters"; empty without a reply. */
  function replySize(row) {
    const chars = replyChars(row);
    if (!chars) return '';
    if (chars < 1000) return chars === 1 ? '1 character' : `${chars} characters`;
    return `${Math.round(chars / 100) / 10}k characters`;
  }

  /**
   * Whether to ask the server for a row's reply now. `asked` is the `reply_chars` the latest request
   * was made at (null: none yet) and `waiting` says it has not come back. Only an expanded row asks,
   * one request at a time, and again only once the count has moved on from the one it asked at.
   */
  function needsReply(row, { open, asked, waiting }) {
    return open && !waiting && hasReply(row) && asked !== replyChars(row);
  }

  const api = { isActive, formatDuration, timeLabel, titleOf, metaOf, detailOf, countLine, ordered, replyChars, hasReply, replyLabel, replySize, needsReply };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SubagentsView = api;
})(typeof window === 'undefined' ? globalThis : window);
