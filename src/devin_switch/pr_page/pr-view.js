'use strict';
// How a chat's pull requests are worded and which controls each one gets, shared by the Pull requests page
// (browser) and the node tests. The bridge decides what is allowed (`can`, `why`); nothing here works that out again.
(function (root) {
  const PILLS = { open: 'Open', draft: 'Draft', merged: 'Merged', closed: 'Closed' };
  /** One glyph per tone, so a status never reads by colour alone. */
  const ICONS = { good: '✓', bad: '✕', warn: '!', wait: '○', unknown: '?', quiet: '–' };
  // In the order the summary counts them.
  const CHECKS = [
    { status: 'fail', tone: 'bad', counted: 'failed', word: 'Failed' },
    { status: 'pending', tone: 'wait', counted: 'running', word: 'Running' },
    { status: 'pass', tone: 'good', counted: 'passed', word: 'Passed' },
    { status: 'skipped', tone: 'quiet', counted: 'skipped', word: 'Skipped' },
  ];
  const NOT_GREEN = ['fail', 'pending'];
  const DECISIONS = {
    approved: { tone: 'good', text: 'Approved' },
    changes_requested: { tone: 'bad', text: 'Changes requested' },
    review_required: { tone: 'warn', text: 'Review required' },
    none: { tone: 'quiet', text: 'No review yet' },
  };
  const REVIEWED = {
    approved: ' approved',
    changes_requested: ' requested changes',
    commented: ' commented',
    dismissed: "'s review was dismissed",
    pending: ' has a pending review',
  };
  const METHODS = { merge: 'Merge commit', squash: 'Squash and merge', rebase: 'Rebase and merge' };
  const ACTIONS = [
    { action: 'merge', label: 'Merge', running: 'Merging…' },
    { action: 'update', label: 'Update branch', running: 'Updating…' },
    { action: 'ready', label: 'Mark ready', running: 'Marking ready…' },
    { action: 'draft', label: 'Convert to draft', running: 'Converting…' },
    { action: 'rerun', label: 'Re-run failed checks', running: 'Re-running…' },
  ];
  const UNREACHED = "That didn't work. Try again.";

  /** A `loading` or `error` pull request may carry nothing but its address; `done` is merged or closed. */
  const kindOf = (pull) => (pull.loading ? 'loading' : pull.error ? 'error' : pull.state === 'open' ? 'open' : 'done');
  const headingOf = (pull) => `${pull.repo} #${pull.number}`;

  /** Open, Draft (open and a draft), Merged or Closed; null while the state is not known. */
  function pillOf(pull) {
    const kind = pull.state === 'open' && pull.draft ? 'draft' : pull.state;
    return Object.hasOwn(PILLS, kind) ? { kind, text: PILLS[kind] } : null;
  }

  const filesLabel = (count) => (count === 1 ? '1 file' : `${count} files`);

  /** The parts of the meta line: "feature → main", the author, "+12", "−3", "2 files". */
  function metaOf(pull) {
    return {
      branch: `${pull.head} → ${pull.base}`,
      author: pull.author,
      added: `+${pull.additions}`,
      removed: `−${pull.deletions}`,
      files: filesLabel(pull.files),
    };
  }

  function behindRow(pull) {
    const { behind, base } = pull;
    if (behind == null) return { tone: 'unknown', text: `Can't tell if it is behind ${base}` };
    if (behind === 0) return { tone: 'good', text: `Up to date with ${base}` };
    return { tone: 'warn', text: `${behind === 1 ? '1 commit' : `${behind} commits`} behind ${base}` };
  }

  function conflictsRow(pull) {
    if (pull.conflicts == null) return { tone: 'wait', text: 'Checking for conflicts…' };
    return pull.conflicts ? { tone: 'bad', text: 'Has merge conflicts' } : { tone: 'good', text: 'No conflicts' };
  }

  const countChecks = (checks, statuses) => checks.filter(({ status }) => statuses.includes(status)).length;

  /** "1 failed, 2 passed": failed, running, passed, skipped, leaving out the ones without checks. */
  function checksSummary(checks) {
    const counts = CHECKS.map(({ status, counted }) => [countChecks(checks, [status]), counted]).filter(([count]) => count);
    return counts.map(([count, counted]) => `${count} ${counted}`).join(', ') || 'No checks';
  }

  /** How many checks failed or still run: what a merge now would not wait for. */
  const notGreen = (checks) => countChecks(checks, NOT_GREEN);

  function checkItem(check) {
    const { tone, word } = CHECKS.find(({ status }) => status === check.status) ?? { tone: 'unknown', word: 'Unknown' };
    return { name: check.name, workflow: check.workflow ?? '', tone, word, url: check.url ?? null };
  }

  /** The summary with its list. Until the reader opens or closes the list, it is open only while a check is not green. */
  function checksRow(pull, local) {
    const { checks } = pull;
    const open = checks.length > 0 && (local.checksOpen ?? notGreen(checks) > 0);
    return {
      tone: CHECKS.find(({ status }) => countChecks(checks, [status]))?.tone ?? 'quiet',
      text: checksSummary(checks),
      toggle: checks.length ? `${open ? 'Hide' : 'Show'} checks` : '',
      open,
      list: open ? checks.map(checkItem) : [],
    };
  }

  /** The decision, then "alice approved" per reviewer and "Waiting on dave, team-x" for the ones still asked. */
  function reviewRow(pull) {
    const { decision, reviews, requested } = pull.review;
    return {
      ...(DECISIONS[decision] ?? DECISIONS.none),
      reviewers: reviews.map(({ author, state }) => `${author}${REVIEWED[state] ?? ' reviewed'}`),
      waiting: requested.length ? `Waiting on ${requested.join(', ')}` : '',
    };
  }

  /** What runs on a pull request now: the server's action, else the page's own request that is still out. */
  const runningOf = (pull, local) => pull.busy ?? local.pending;

  /**
   * The buttons of an open pull request. Merge is always there, off when the server says no; the others only
   * when the server allows them. While something runs they are all off and the running one says so.
   */
  function actionsOf(pull, local) {
    const running = runningOf(pull, local);
    return ACTIONS.filter(({ action }) => action === 'merge' || pull.can[action] || action === running).map((item) => ({
      action: item.action,
      label: item.action === running ? item.running : item.label,
      enabled: !running && pull.can[item.action],
    }));
  }

  /** Why Merge is off, in the server's words. */
  const reasonOf = (pull) => (pull.can.merge ? '' : (pull.why?.merge ?? ''));

  /** The reader's merge method while the repository still allows it, else the repository's default: its first. */
  const methodOf = (pull, local) => (pull.methods.includes(local.method) ? local.method : (pull.methods[0] ?? null));

  /** Whether a merge can be confirmed now: an open pull request the server lets merge, with nothing running on it. */
  const mergeable = (pull, local) => kindOf(pull) === 'open' && pull.can.merge && !runningOf(pull, local);

  /** The step between Merge and the merge itself; null unless the reader opened it on a pull request that can be merged now. */
  function confirmOf(pull, local) {
    if (!local.confirming || !mergeable(pull, local)) return null;
    const method = methodOf(pull, local);
    const waiting = notGreen(pull.checks);
    return {
      method,
      methods: pull.methods.map((value) => ({ value, label: METHODS[value] ?? value, chosen: value === method })),
      chooser: pull.methods.length > 1,
      warning: waiting ? `${waiting === 1 ? '1 check is' : `${waiting} checks are`} not green` : '',
    };
  }

  /**
   * Everything one card shows, as plain data: the page draws it and draws again only when it changed.
   * `local` is what the reader did to this pull request on this page (see `fresh`).
   */
  function cardOf(pull, local = fresh()) {
    const kind = kindOf(pull);
    const card = {
      kind,
      heading: headingOf(pull),
      title: '',
      pill: null,
      note: '',
      meta: null,
      status: null,
      actions: [],
      reason: '',
      confirm: null,
      error: local.error ?? '',
      locked: local.pending != null,
    };
    if (kind === 'loading') return { ...card, note: 'Loading…' };
    if (kind === 'error') return { ...card, note: pull.error };
    Object.assign(card, { title: pull.title, pill: pillOf(pull), meta: metaOf(pull) });
    if (kind === 'done') return card;
    card.status = { behind: behindRow(pull), conflicts: conflictsRow(pull), checks: checksRow(pull, local), review: reviewRow(pull) };
    const confirm = confirmOf(pull, local);
    return confirm ? { ...card, confirm } : { ...card, actions: actionsOf(pull, local), reason: reasonOf(pull) };
  }

  /**
   * What the reader did to one pull request on this page, kept across snapshots: the confirm step is open,
   * the merge method chosen in it, the check list opened or closed by hand (null: not touched), the error
   * of the latest action, and the page's own action that has not been answered yet.
   */
  const fresh = () => ({ confirming: false, method: null, checksOpen: null, error: null, pending: null });

  /** A pull request's page state after a new snapshot: kept, apart from what the snapshot makes moot. */
  function reconcile(local, pull) {
    // A pull request that is loading says nothing yet, so nothing is moot.
    if (pull.loading) return local;
    return {
      ...local,
      // Closed for good: it must not open again by itself once the pull request can be merged again.
      confirming: local.confirming && mergeable(pull, local),
      method: kindOf(pull) === 'open' && !pull.methods.includes(local.method) ? null : local.method,
    };
  }

  /** The page state of every pull request in a snapshot, by url; the ones that left the snapshot are forgotten. */
  function reconcileAll(states, pulls) {
    return new Map(pulls.map((pull) => [pull.url, reconcile(states.get(pull.url) ?? fresh(), pull)]));
  }

  const startMerge = (local) => ({ ...local, confirming: true });
  const cancelMerge = (local) => ({ ...local, confirming: false });
  const chooseMethod = (local, method) => ({ ...local, method });
  const showChecks = (local, open) => ({ ...local, checksOpen: open });
  /** The page sent an action; the confirm step, if it was open, has done its part. */
  const began = (local, action) => ({ ...local, confirming: false, pending: action });
  /** The answer is back: an error replaces the line on the card, a success clears it. */
  const ended = (local, error) => ({ ...local, pending: null, error });
  /** Opening a link changes nothing on the pull request, so only a failure shows and a success leaves an earlier error alone. */
  const opened = (local, error) => (error ? { ...local, error } : local);
  const hideError = (local) => ({ ...local, error: null });

  /** The body of a `POST /pr/act`, with only the fields the action has. */
  function actBody(session, action, { url, method, target } = {}) {
    return { session, action, ...(url && { url }), ...(method && { method }), ...(target && { target }) };
  }

  /** Null when an action went through, else the line to show. `reply` is the JSON of a 200 answer; anything else is null. */
  function replyError(reply) {
    if (reply?.ok === true) return null;
    return typeof reply?.error === 'string' && reply.error.trim() ? reply.error.trim() : UNREACHED;
  }

  /** "Updated 12:34:56" in the reader's local time. */
  function updatedLabel(date) {
    const pad = (part) => String(part).padStart(2, '0');
    return `Updated ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  const api = {
    ICONS,
    kindOf,
    headingOf,
    pillOf,
    filesLabel,
    metaOf,
    behindRow,
    conflictsRow,
    checksSummary,
    notGreen,
    checksRow,
    reviewRow,
    actionsOf,
    reasonOf,
    confirmOf,
    cardOf,
    fresh,
    reconcile,
    reconcileAll,
    startMerge,
    cancelMerge,
    chooseMethod,
    showChecks,
    began,
    ended,
    opened,
    hideError,
    actBody,
    replyError,
    updatedLabel,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PrView = api;
})(typeof window === 'undefined' ? globalThis : window);
