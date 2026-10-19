# ai-subs

A local dashboard for people who run several Claude and ChatGPT subscriptions on one Mac.

- **Usage:** how full each account's 5-hour and weekly limits are, read from Anthropic and
  OpenAI directly, and which account has the most room right now.
- **Sessions:** every recent Claude Code and Codex session, with a short summary. Copy a
  handoff packet to continue the work anywhere, or move a Claude app session from an account
  that ran out to one with room.

Everything runs on your Mac. Nothing is sent anywhere except the two providers' own usage
endpoints, called with the tokens Claude Code and Codex already hold.

## Requirements

- macOS, with `python3` (run `xcode-select --install` if it is missing). No packages to install.
- Claude Code and/or Codex, signed in. Each signed-in account shows up on its own.
- Optional: the Claude desktop app (for moving and archiving sessions), and
  [SwiftBar](https://swiftbar.app) for the menu bar item.

## Install

```bash
git clone https://github.com/ArnelBukva/ai-subs-public.git ai-subs
cd ai-subs
./install.sh
```

Then open http://localhost:8787. A launch agent keeps the server running and restarts it at
login. Re-run `./install.sh` after a `git pull`; `./install.sh --uninstall` removes it.

**More Claude accounts.** Each Claude Code login lives in its own folder (`~/.claude`,
`~/.claude-<name>`). To add one: `python3 login.py anthropic --label work`.

**A nicer address.** The page works at any `*.localhost` name. If you use a local proxy such as
portless, put the address in `~/.ai-subs/url` (for example `https://subs.localhost:1355`) so the
menu's links open it.

## Caveats

- The usage endpoints are the ones Claude Code and Codex call themselves. They are not a public
  API and can change without notice.
- Moving and archiving sessions edits the Claude desktop app's own session records under
  `~/Library/Application Support/Claude/claude-code-sessions/`. Auto-archive is off until you
  switch it on.

## Files

- `server.py` — usage collector (`/api/usage`) and the page's server.
- `sessions.py` — reads session transcripts: listing, Haiku summaries, handoff packets,
  moving a session's app record to another account. Also the CLI the menu's clicks call.
- `sessions_web.py` — the session routes of the page (list, packet, copy, move, resume).
- `index.html` — the page. `menubar/ai-subs.2m.py` — the menu: usage on top, sessions below.

```bash
python3 server.py --once            # JSON, for cron or a status bar
```

## Which account a session belongs to

The Claude app keeps one record per session per account under
`~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/`.
A session belongs to the account whose record saw activity last (`lastActivityAt`):
work in it from another account and it follows you there. A move makes the target's
copy the newest, so the session belongs to the target from then on.

How a move reaches the sidebar:
- **Into the account the app is signed in to** (read from the app's log, the last
  `[LocalSessionManager] Initialization succeeded` line): the tool opens the app's own
  resume link, `claude://resume?session=<id>`. The app imports the session, shows it
  in the sidebar at once and opens it; a copy it already holds is reopened, not doubled.
  The tool waits for the app's log to confirm. No restart.
- **Into another account:** the tool writes the record file. The app reads an account's
  records from disk each time you switch to it, so it is there after the switch.
  A record written into the signed-in account behind the app's back stays invisible
  until the next switch or restart, which is why that case goes through the app.
The Remote Control line inside the transcript is not used for this: it names the
main account even for sessions that only ever ran in the personal one.

Times are the last message in the transcript, not the file's modification time:
switching accounts in the app writes a note into every session it closes.
Codex rollouts that only hold an imported Claude Code session are left out.

## Auto-archive

Every hour the server archives sessions with no message for N days, in every account
on this Mac (off by default; to switch it on, write `~/.local/state/ai-subs/auto-archive.json`: `{"enabled": true, "days": 4}`).
It sets `isArchived` on the app's record, the only field that differs from an active one.
The account the app has open picks the change up at its next load (switch or restart);
the app has no outside archive action. Idle means the later of the record's time and the
transcript's last message. Sessions the app marks `autoArchiveExempt`, or whose own
worktree has uncommitted changes, are left alone. Each archive is logged to
`~/.local/state/ai-subs/auto-archive.jsonl`; `python3 sessions.py archive-idle 4 --dry-run`
lists without changing anything. Only `local_*.json` files are session records.

**The account the app has open** is covered by the optional `session-tidy` mod in this repo. To use it,
load it in every session by setting `CLAUDE_CODE_PLUGIN_DIRS` to `<repo>/session-tidy` in the `env`
block of `~/.claude/settings.json`. In a desktop
session it checks every 10 minutes; one sweep per hour across all sessions (a shared
timestamp) lists the app's sessions and archives the idle ones through the app's own
`archive_session`, so the sidebar changes at once and the app does not write them back. It
reads the same settings file, sweeps only when the default permission mode is bypass or auto
(the app asks before archiving otherwise), skips running and pinned sessions and worktrees
with uncommitted changes, and writes its last sweep to `~/.local/state/ai-subs/session-tidy.json`.

## Where the numbers come from

Each provider is asked directly. Nothing is estimated from local logs, so the
percentages match what the provider itself enforces.

| Provider | Source | Verified |
|---|---|---|
| Anthropic | `GET api.anthropic.com/api/oauth/usage` | 2026-09-17 |
| OpenAI | `GET chatgpt.com/backend-api/wham/usage` | 2026-09-17 |

## Two kinds of account

**External** — a token already held by Claude Code or Codex. It is only ever
read. Both providers rotate the refresh token every time it is used, so
refreshing a token we do not own would silently sign that CLI out. When one
expires (Claude Code's every 8 hours, Codex's every 10 days) the dashboard asks
the owning CLI to refresh it: one tiny one-shot call in that account's own
folder (`claude -p` with nothing else loaded, or `codex exec`). The CLI rotates
its own token and stores it; the dashboard reads the result. Only a revoked
login still shows "sign in", with the exact command. Measured 2026-09-18: a
Claude refresh takes about 6 s and costs one Haiku token.

**Ours** — an account added with `login.py`. The dashboard owns that token pair
and refreshes it on its own.

```bash
python3 login.py anthropic --label "Claude account 2"
```

It prints a link instead of opening a browser: each subscription needs the
browser profile that is signed in to it, and a window stolen mid-work is worse
than a click. Tokens live in `~/.ai-subs/accounts.json`, mode 0600.

## Adding a provider

One function in `providers.py` returning the shared shape, one line in
`ADAPTERS`. Nothing else changes.

## Menu bar (SwiftBar)

`menubar/ai-subs.2m.py` is a SwiftBar plugin: one card per account with its 5-hour and weekly
bars, light and dark, refreshed every two minutes from the dashboard's API.
`menubar/ai-subs-menu-action` holds its buttons (refresh, add an account, sign in again, start
the dashboard). `install.sh` links both in when SwiftBar is installed.

Why it stays up: the server runs under launchd (`local.ai-subs`) on the fixed port 8787 and is
restarted within 3 s if it dies.
