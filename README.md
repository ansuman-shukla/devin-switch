# Devin Switch

A local macOS prototype for switching saved Devin CLI logins. Run `ds use ansuman-2`
and launch `ds run` without signing into Google again while that login remains valid.
Each account has separate credentials and settings. Conversations started through
`ds` use one shared local history store, so switching accounts keeps them visible.

This version provides explicit switching. It does not automatically detect quota
exhaustion or retry an interrupted agent. `ds next` cycles through saved logins;
it does not know whether those accounts have quota remaining.

## Install

Requires macOS, Python 3.11+, [uv](https://docs.astral.sh/uv/), and Devin CLI.
The CLI bundled in `/Applications/Devin.app` is detected automatically when `devin`
is not on PATH. To choose another executable, set `DS_BINARY` to its absolute path.

```sh
uv tool install /absolute/path/to/devin-switch
ds --help
```

If uv's tool directory is not on PATH, run `uv tool update-shell`, then open a new terminal.

## Enroll two accounts

```sh
ds profiles
ds add ansuman-1 --chrome-profile "Profile 1"
ds add ansuman-2 --chrome-profile "Profile 11"
ds login ansuman-1
ds login ansuman-2
```

`ds profiles` shows Chrome's internal directory identifiers alongside the familiar
profile names and email addresses. It only reads Chrome's profile index.

When a Chrome profile is assigned, login opens Devin's manual token page in that
profile and runs the CLI's built-in manual login flow. Complete Google sign-in,
copy the token from the page, and paste it **only into the native terminal prompt**.
This enrollment step is needed once per account, and again if the login is revoked
or expires. The wrapper does not collect tokens in its own prompts or logs.

For the normal native callback login instead, run `ds login ansuman-1 --default-browser`.
That uses your default browser rather than the assigned Chrome profile.

To replace an existing login, run `ds run --account ansuman-1 -- auth logout`, then
`ds login ansuman-1`. Logout removes that profile's credentials, so a canceled
replacement login leaves it signed out.

## Add the rest of your accounts

There is no two-account limit. Register each additional account with a unique alias,
then sign into it once. Choose profile identifiers from `ds profiles`:

```sh
ds profiles
ds add ansuman-3 --chrome-profile "Profile 12"
ds login ansuman-3
ds add ansuman-4 --chrome-profile "Profile 13"
ds login ansuman-4
ds list
```

The alias is your local label, not your Google email or Devin organization name.
Two aliases may use the same Chrome profile if that profile has multiple Google
accounts; select the intended account during each login. A Chrome profile by itself
does not establish which Devin subscription was authenticated. Check the identity
and organization shown by the native CLI when you launch it.

## Switch and continue

```sh
ds use ansuman-1
ds run
# Exit Devin before switching.
ds use ansuman-2
ds run -- --continue
# Or choose a particular conversation:
ds sessions
ds run -- --resume SESSION_ID
ds next
ds run
```

Commands after `ds run --` are forwarded without shell interpretation. Your current
working directory is preserved. `ds run --account ansuman-2 -- --continue` uses an
account for that invocation without changing your saved selection.

One command holds the profile lock while an interactive session is running. A second
switch or runner fails with a clear message instead of racing with the first.

### "No sessions to continue in this directory"

Start your first conversation in that project using `ds run` with no `--continue`.
Send at least one message. Exit the CLI, select another account, and run
`ds run -- --continue` from the **same project directory**. Existing conversations
from your ordinary Devin CLI or Desktop are not imported into this prototype.

When you hit a quota limit, exit the CLI before running:

```sh
ds next
ds run -- --continue
```

Alternatively, choose a specific account with `ds use <alias>`. `ds next` only checks
for a saved login; if that account is also exhausted, choose another. Do not use
`ds login` for every switch: `ds use` reuses the credentials already saved.

## Verify A → B → A

```sh
ds verify ansuman-1 ansuman-2
```

This checks both saved logins, rejects identical credential files, and verifies that
the same local session list is visible through A, B, and A again. It makes no model
request and does not change the selected account. Different tokens are not, by
themselves, proof of different Google users or independent paid allowances: confirm
the intended identity during each enrollment.

Local history visibility is separate from successful inference across accounts.
To test the latter, start a disposable conversation through A, ask it to remember
a unique phrase, exit, switch to B, and resume it with `--continue`. Ask it to recall
the phrase, then repeat on A. This uses a small amount of each account's allowance.
Do this in an empty scratch directory before using the tool for real work.

## Storage

State defaults to `~/.local/share/devin-switch` (override with `DS_HOME`):

```text
accounts/ansuman-1/data/devin/credentials.toml  # native saved login A
accounts/ansuman-2/data/devin/credentials.toml  # native saved login B
accounts/<alias>/config/                      # account-specific CLI configuration
shared/cli/                                  # new shared session database and state
shared/summaries/                             # new shared session summaries
selected                                     # alias only
```

The wrapper scopes XDG directories per account and removes known environment token
overrides before invoking Devin. Your normal `HOME` and existing Devin storage are
untouched. Existing conversations in your normal CLI or IDE are not imported.
Credentials stay outside this project's source tree; account directories are private
and the native credentials files are restricted to the current user.

Settings and MCP configuration are initially fresh in each account. Credentials for
external tools and project-local configuration are still governed by those tools.
The prototype does not switch the Desktop IDE's account.

## Development

```sh
uv sync --python 3.11
make format && make lint && make test
# Optional offline checks against the actual installed CLI:
DS_TEST_NATIVE="/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin" make test
```

Tests cover failed logins, credential separation, environment overrides, browser
selection, concurrent commands, argument forwarding, and shared session visibility.
The optional native test uses an isolated temporary database and no real credentials
or model requests. It is deliberately sensitive to changes in the native database
layout so an incompatible CLI update is detected.

Authentication format and commands are based on Devin's
[authentication documentation](https://docs.devin.ai/cli/enterprise/devin-auth) and
[command reference](https://docs.devin.ai/cli/reference/commands). Saved-login status
is not a live quota or billing check.

Validated on macOS with Devin Desktop 3.9.19 and bundled CLI 3000.6.19. The suite
passed 27 tests including the installed-CLI isolation/history test. On September 11,
2026, a live conversation was started under account A, resumed under account B,
and resumed again under A. Both resumed requests correctly recalled the phrase
from the first message without repeating it in the follow-up prompts. No new
browser login was needed. This verifies a small conversation across the two enrolled
accounts, not recovery of interrupted tool calls or automatic quota detection.
