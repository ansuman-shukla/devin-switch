# Devin Switch

**Your accounts, one workspace.** A native macOS app and lightweight CLI for managing
saved Devin CLI logins, checking reported usage, and reopening shared local conversations.

[Get started](#quick-start) · [Download releases](../../releases) ·
[CLI reference](#cli-reference) · [Development](#development) · [License](LICENSE)

> **Independent community project · Beta · macOS 14+**
> Not affiliated with or endorsed by Cognition. Devin CLI is installed separately.
> Use only accounts you are authorized to access, in accordance with your organization's policies.

## What it does

- **Separate account profiles.** Keep each saved login, CLI configuration, and cache isolated.
- **A shared local history.** Reopen conversations created through Switch with a chosen account.
- **A native SwiftUI interface.** Manage profiles, choose a project, and launch chats in Terminal.
- **Usage at a glance.** Show daily and weekly allowance when the service reports it, including
  reset times and explicit stale or unavailable states.
- **Predictable switching.** Changing the default affects future launches—not an already-open CLI.
- **Concurrent launches.** Open multiple sessions while protecting profiles from removal or
  replacement login while in use.

There is no automatic quota failover, interrupted-request retry, or guarantee that a different
account can continue every conversation. `ds next` cycles through saved logins; it does not
select an account based on remaining quota. Switch does not change the Devin Desktop IDE account.

## Quick start

### Option A: Download the Mac app

1. Install [Devin CLI](https://docs.devin.ai/cli) if you do not already have it.
   With Homebrew: `brew install --cask devin-cli`. An installation bundled with
   `/Applications/Devin.app` is also detected automatically.
2. Open this repository's **[Releases](../../releases)** page and download a DMG:

   | Your Mac | Download |
   | --- | --- |
   | Apple Silicon (M1 or later) | `Devin-Switch-VERSION-macos-arm64.dmg` |
   | Intel | `Devin-Switch-VERSION-macos-x86_64.dmg` |

3. Open the disk image and drag **Devin Switch** to **Applications**. Eject the image,
   then open the installed app—not the copy inside the disk image.
4. Choose **Add account**, give it a local alias, and select **Sign in**. Complete
   authentication in Terminal. Repeat for any additional authorized accounts.
5. Select an account, choose your project folder, and click **Start new**.

The portable app includes its Python runtime. **No Python, uv, or developer tools are
required to use the downloaded app.** Devin CLI and access to a valid account are still required.
A DMG does not install the `ds` command on your shell's PATH; use Option B if you want it.

**First launch on macOS:** builds are ad-hoc signed for local integrity, **not signed with
an Apple Developer ID or notarized by Apple**. macOS may block the first launch. After
checking the source and download checksum, try opening the app once, then use
**System Settings → Privacy & Security → Open Anyway** if offered. Do not disable Gatekeeper.
Managed Macs may require administrator approval. Building from source is another option.

Release downloads become available after a maintainer publishes the draft produced by CI.

### Option B: Install the CLI with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv` on macOS)
and Devin CLI. Then download the wheel from a published release:

```sh
uv tool install --python 3.11 ~/Downloads/devin_switch-0.3.0-py3-none-any.whl
uv tool update-shell
```

Open a new terminal, then register and sign into your first account:

```sh
ds --help
ds add personal
ds login personal
ds use personal
ds run
```

Run `ds run` from the project directory you want to work in. To install from a clone or
an extracted source archive instead, run `uv tool install --python 3.11 .` inside that folder.
`uv` manages the Python environment; the installed package has no third-party Python runtime
dependencies. The package is not currently published to PyPI.

### Verify a download

Download the release's `SHA256SUMS` alongside the files you want, then run from that folder:

```sh
shasum -a 256 --check --ignore-missing SHA256SUMS
```

Each DMG also has an individual `.dmg.sha256` file. Checksums detect download corruption;
they are not a substitute for trusting the publisher or an Apple signing identity.

## Everyday workflow

### Add another account

```sh
ds add work
ds login work
ds list
ds use work
ds run
```

Aliases such as `personal` and `work` are local labels, not verified account identities.
Confirm the intended user and organization during sign-in. A saved login can be reused
until it expires or is revoked; you do not need to run `ds login` every time you switch.

### Choose a Chrome profile for sign-in

```sh
ds profiles
ds add work --chrome-profile "Profile 1"
ds login work
```

`ds profiles` reads Chrome's profile index, not its cookies or password store. It shows
internal directory identifiers alongside profile names and email addresses. A Chrome
profile's email does not prove which Devin account was chosen during authentication.

When a Chrome profile is assigned, Switch opens the native manual-token login page in
that profile. Paste the token **only into the native CLI's Terminal prompt**—never into
an issue, screenshot, README, or chat. Switch does not collect it in its own prompts.
For native default-browser login instead, use `ds login work --default-browser`.

### Switch inside a chat when an account runs out

Start your chat with `ds run` as usual. When you hit a limit, enter this at the chat prompt:

```text
!ds switch
```

**That's it—no project setup and no manual exit.** The local command refreshes reported usage,
chooses the other signed-in account with the most remaining allowance, closes the CLI normally,
and reopens the **exact saved conversation in the same terminal and project**. No model response
is needed to perform the switch. When the resumed CLI process starts, its account also becomes
**the app's default for new chats**. The app picks this up on its next automatic state refresh
(or when brought to the foreground). Other already-open chats keep their own accounts.
Canceled requests, failed checks, and process-creation failures leave the default unchanged.
Once the new CLI has launched, closing that chat does not revert the default.

Accounts are ranked by the smaller of their remaining daily and weekly percentages: either quota
can prevent another request. Explicitly non-applicable windows are excluded. Exhausted accounts,
stale readings, and unavailable usage are not treated as unlimited; if no usable reading exists,
the current chat stays open. Equal scores use alias order. To override the choice, use
**`!ds switch work`**. Reported quota is not a guarantee of model access or independent allowances.

Switch manages its exit hook automatically in each account's private CLI configuration,
preserving existing settings, hooks, and JSON comments. It does not create or modify project
configuration. Native CLI **3000.5.20 or newer** is needed for exit hooks. An already-open older
wrapper needs one normal restart after updating the installed `ds` command.

The exit hook provides the final conversation ID, including after in-chat `/new` or `/resume`.
Missing exit receipts, failed exits, or another open Switch chat in this repo stop the handoff
rather than guessing “latest.” If automatic exit is unavailable (for example, a rebound exit
shortcut), the request times out without force-killing the CLI. `!ds switch --cancel` cancels a
pending request.

The saved transcript survives, but unsent input and in-memory tool shells are not transferred.
Switch does not replay initial prompts or prompt files; send your next message when the chat
reopens. Sandbox, permission-mode, and export flags are retained, and native resume uses the
saved model rather than reapplying the initial `--model` override. Piped/non-interactive runs,
custom `--config` overrides, and unsupported native options are forwarded normally without
automatic handoff support.

### Switch accounts and resume from the shell

First exit CLI chats running in the target project. Then, from the same project directory:

```sh
ds use work
ds run -- --continue
```

Or select an exact conversation:

```sh
ds sessions
ds run --account personal -- --resume SESSION_ID
```

`--account` chooses a profile for just that launch; it does not change the saved default.
Arguments after `--` are forwarded to Devin CLI without shell interpretation. `--continue`
is resolved to an exact conversation ID, and `--resume` requires an explicit ID.

In the app, **Resume chat with…** opens the shared-history picker. **Resume latest** selects
the latest conversation in the chosen folder. Resume is conservatively blocked while
Switch-managed CLI chats remain open in that repository. Other repositories can keep running.

The session overview tracks **wrapper launches and open processes**, not whether an agent
is actively working. In-terminal `/new` and `/resume` changes are not tracked or guessed.
Use separate Git worktrees if concurrent sessions would otherwise edit the same files.

### Manage saved profiles

- **Use for new chats** updates the default, leaving existing sessions unchanged.
- **Check login** checks native saved-login status; it is not a quota check.
- **Auto refresh** polls reported usage approximately once per minute. Usage is shared by
  all chats on the same account, not allocated independently per session.
- **Remove profile…** deletes that profile's local login, settings, and usage cache after
  confirmation. It preserves shared conversations, repositories, and the Chrome profile.
  Removal and renaming are blocked while the profile is in use.

CLI removal requires explicit confirmation: `ds remove work --yes`.

## CLI reference

| Command | Purpose |
| --- | --- |
| `ds gui` | Open the app from `~/Applications` or `/Applications` |
| `ds add ALIAS` | Register a local profile |
| `ds profiles` | List available Chrome profile identifiers |
| `ds login ALIAS` | Enroll or check a saved login |
| `ds list` | List profiles and the selected default |
| `ds status [ALIAS]` | Check a saved login with the native CLI |
| `ds use ALIAS` | Change the default for future launches |
| `ds next` | Select another profile with a saved login; does not measure quota |
| `!ds switch [ALIAS]` | Resume this chat automatically with the best reported allowance, or a chosen account |
| `!ds switch --cancel` | Cancel this chat's queued handoff |
| `ds run [--account ALIAS] -- ARGS` | Launch Devin CLI with the selected profile |
| `ds sessions` | List shared history for the current project |
| `ds verify FIRST SECOND` | Check A → B → A login and local-history visibility |
| `ds remove ALIAS --yes` | Remove a local profile, preserving shared history |

`ds verify` makes no model request. Matching history across accounts is separate from a
successful inference request; different credential files alone do not prove distinct users
or independent allowances. Testing live cross-account continuation consumes account usage.

## Privacy and storage

Switch stores its state outside this repository, in `~/.local/share/devin-switch` by default:

```text
accounts/<alias>/data/devin/credentials.toml
accounts/<alias>/config/
accounts/<alias>/cache/
shared/cli/sessions.db
shared/summaries/
runs/
handoffs/
launchers/
selected
```

- Native credentials are local files restricted to the current user, **not Keychain-encrypted**.
  Protect this directory and its backups as sensitive data.
- Each account receives isolated XDG directories. `CHISEL_SESSION_DB` is explicitly pinned
  to shared history, and known authentication environment overrides are removed.
- Usage refresh sends the saved credential to the service's status endpoint over HTTPS.
  Custom API servers are not supported for usage reporting; credential-bearing redirects
  are rejected. The usage cache can contain account email and plan information.
- The Swift app communicates with Python over stdin/stdout; there is no local web server.
  Authentication tokens are not included in bridge responses.
- Launch metadata contains account aliases, project paths, and IDs—not prompts or credentials.
  Local conversations themselves can contain sensitive project material.
- Existing CLI/Desktop conversations are not imported. Your normal `HOME`, ordinary CLI
  storage, external-tool credentials, and project-local configuration are not migrated.

| Variable | Purpose |
| --- | --- |
| `DS_HOME` | Override Switch's private state directory; keep it outside the repository |
| `DS_BINARY` | Choose the native Devin executable explicitly |
| `DS_TEST_NATIVE` | Enable isolated, offline native-CLI compatibility tests |

Never commit your state directory, environment files, signing certificates, or credentials.
Ignore rules and secret scans provide safeguards, not a guarantee against every possible leak.

## Troubleshooting

**`ds` is not found:** run `uv tool update-shell`, then open a new terminal.

**Devin CLI is not found:** install it separately. Switch checks PATH, the Desktop bundle,
`~/.local/bin`, and standard Homebrew locations. For a custom location, set `DS_BINARY` to
its executable path. Finder-launched apps do not inherit your terminal's shell configuration;
use a standard installation location for the app.

**No sessions to continue:** start a conversation using `ds run`, send a message, exit,
and resume from the same project directory. Ordinary CLI/Desktop history is not imported.

**A profile is busy:** close the CLI sessions using it before removing, renaming, or replacing
its login. Changing the saved default does not switch an already-running process.

**Usage is unavailable:** the account may require sign-in, the service may be unreachable,
or the plan may not expose daily/weekly quotas. Unavailable does not mean unlimited usage.

**Replacing a login:** `ds run --account work -- auth logout` signs that profile out;
then run `ds login work`. If the new login is canceled, the old credentials are not restored.

## Development

Requirements: macOS 14+, [uv](https://docs.astral.sh/uv/), Apple's Command Line Tools
(`xcode-select --install`), and Gitleaks (`brew install gitleaks`).

```sh
make setup
make check
```

| Target | Action |
| --- | --- |
| `make format` | Format Python with Ruff |
| `make format-check` | Check formatting without changing files |
| `make lint` | Ruff lint plus formatting check |
| `make test` | Run isolated pytest tests |
| `make typecheck` | Typecheck both SwiftUI source files for the current architecture |
| `make secrets` | Scan all local Git history and publishable working-tree files with Gitleaks |
| `make check` | Run lint, formatting, tests, Swift typechecking, and secret checks |
| `make app` | Install `ds` with uv and build `~/Applications/Devin Switch.app` |
| `make build` | Build a Python wheel and source archive in `dist/` |
| `make dmg` | Build a portable app, DMG, and SHA-256 checksum in `dist/` |
| `make smoke-app` | Test the portable app after relocation using isolated state and a fake CLI |

`uv.lock` is committed, and development commands require it to be up to date. Python is
formatted and linted with Ruff; Swift is typechecked, not automatically reformatted.
The portable runtime is built with a pinned PyInstaller version and Python 3.11.

To also check the installed native CLI without real credentials or model requests:

```sh
DS_TEST_NATIVE="/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin" make test
```

The native test uses a disposable database and checks the database layout expected by the
wrapper. Compatibility was originally validated with Desktop 3.9.19 / CLI 3000.6.19;
upstream authentication, session schemas, and usage APIs can change.

For builds from inside a Switch-managed session:

```sh
env -u XDG_DATA_HOME -u XDG_CONFIG_HOME -u XDG_CACHE_HOME -u XDG_STATE_HOME make app
```

The install target also clears these overrides and explicitly reinstalls the package so
source-only changes cannot silently reuse an old wheel. It does not alter account state.
A local `make app` build references the installed uv environment; **only `make dmg` builds
are portable**. Close the app before replacing an installed copy.

Portable builds refuse to overwrite an existing output. Choose a fresh output directory
when rebuilding without removing the previous release:

```sh
uv run --locked --group build --python 3.11 python scripts/build_app.py --standalone --dmg --output "dist/rebuild/Devin Switch.app"
```

Audit built Python packages and verify an isolated `uv tool install`:

```sh
make build
uv run --locked python scripts/check_dist.py
```

Tests cover account isolation, concurrent leases, session handoffs, argument quoting,
usage failures, private bridge responses, native discovery, and packaged launch behavior.
The portable-app smoke test relocates the bundle to a path containing spaces and exercises
the bundled runtime and Terminal launcher without accessing your real accounts.

## CI and releases

GitHub Actions runs lint, format checks, tests, Swift typechecking, secret scanning, and
package-install checks on Apple Silicon and Intel runners. Python tests cover 3.11 and 3.13.
Both architectures also build and smoke-test portable DMGs. PR workflows use read-only
repository permissions and do not receive signing or account credentials.

To release:

1. Update the version in `pyproject.toml` and refresh `uv.lock` with `uv lock`. Run `make check`,
   review the changes, and commit them. The app version is read from package metadata.
2. Push the reviewed commit, then tag that commit with the same version:

   ```sh
   git tag v0.3.0
   git push origin v0.3.0
   ```

3. The **Release** workflow reruns CI, verifies the tag against the package version, and
   creates a **draft** containing Apple Silicon and Intel DMGs, individual DMG checksums,
   a wheel, a source archive, and `SHA256SUMS`.
4. Review the artifacts and release notes in GitHub, then publish the draft. The Releases
   page is the download landing page; no separate web hosting is required.

Only the final release job has write permission. No personal access token, Apple signing
secret, or PyPI credential is required. CI configuration is provided here; successful local
checks do not imply it has already run on GitHub. Apple Developer ID signing and notarization
are not configured in this release pipeline.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for project notices.
Bundled runtime components retain their own licenses. Devin CLI is neither bundled nor
relicensed by this project.
