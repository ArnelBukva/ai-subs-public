"""Where the accounts live.

Two kinds of account, and the difference matters:

  owner="external"  The token belongs to a CLI that is already installed (Claude
                    Code, Codex). We only ever READ it. Both Anthropic and OpenAI
                    rotate the refresh token on use, so refreshing a token we do
                    not own would silently sign that CLI out. When it has expired
                    we ask that CLI to refresh it itself: one tiny one-shot call
                    (`claude -p` / `codex exec`) in the right config folder. The
                    CLI rotates its own token, writes it where it always does, and
                    we read the result. Only a revoked refresh token still needs
                    a real sign-in.

  owner="self"      An account we signed in ourselves, for the second, third and
                    fourth subscriptions that no local CLI holds. We own the
                    whole token pair and refresh it freely.

Accounts we own are stored in ~/.ai-subs/accounts.json, mode 0600.
"""

import base64
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".ai-subs")
ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "accounts.json")
MAIN_CLAUDE_DIR = os.path.join(HOME, ".claude")
URL_FILE = os.path.join(CONFIG_DIR, "url")
STATE_DIR = os.path.join(HOME, ".local", "state", "ai-subs")
REFRESH_LOG = os.path.join(STATE_DIR, "refresh.log")


@contextlib.contextmanager
def claude_login_lock(config_dir):
    """Only one `claude` call from this tool may renew a login at a time. Anthropic rotates the
    refresh token on every use; a second call presenting the old one gets invalid_grant, and
    Claude Code then empties the stored login. The server's refresh and the summary job are
    separate processes, so this is a file lock."""
    os.makedirs(STATE_DIR, exist_ok=True)
    name = "claude-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8] + ".lock"
    with open(os.path.join(STATE_DIR, name), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def log_refresh(config_dir, caller, ok, out):
    """One line per renewal attempt, so the next lost login has a record to read."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        last = " ".join((out or "").split())[-160:]
        with open(REFRESH_LOG, "a") as f:
            f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {caller} "
                    f"{config_dir.replace(HOME, '~')} {'ok' if ok else 'FAILED'} {last}\n")
    except OSError:
        pass


def base_url():
    """Where the page is served: AI_SUBS_URL, else the first line of ~/.ai-subs/url
    (install.sh writes it), else the server's own address."""
    url = os.environ.get("AI_SUBS_URL")
    if not url:
        try:
            with open(URL_FILE) as f:
                url = f.readline().strip()
        except OSError:
            pass
    return (url or "http://localhost:8787").rstrip("/")

# One refresh per CLI folder at a time, and after a failure wait before trying
# again: a folder whose refresh token was revoked must not be hit every poll.
_refresh_locks = {}
_refresh_failed_at = {}
_refresh_lock = threading.Lock()
REFRESH_RETRY_SECONDS = 600
REFRESH_TIMEOUT = 120


class Store:
    def __init__(self):
        self.accounts = []
        self._own = {}

    # ---------------- accounts we own ----------------

    def load_own(self):
        if not os.path.exists(ACCOUNTS_FILE):
            return []
        try:
            with open(ACCOUNTS_FILE) as f:
                data = json.load(f)
            return data.get("accounts", [])
        except Exception:
            return []

    def save(self, account):
        """Persist one account we own. Called after every token rotation."""
        if account.get("owner") != "self":
            return
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        current = self.load_own()
        by_id = {a.get("id"): a for a in current}
        by_id[account["id"]] = account
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"accounts": list(by_id.values())}, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, ACCOUNTS_FILE)

    # ---------------- accounts other CLIs already hold ----------------

    @staticmethod
    def _claude_code_dirs():
        """Every Claude Code config folder on this Mac: ~/.claude plus ~/.claude-<name>.

        One folder per signed-in account. login.py creates them with
        CLAUDE_CONFIG_DIR, and Claude Code keeps each folder's token under its own
        keychain entry: "Claude Code-credentials" for ~/.claude, and
        "Claude Code-credentials-<first 8 hex of sha256(folder path)>" for any other.
        """
        dirs = [os.path.join(HOME, ".claude")]
        for name in sorted(os.listdir(HOME)):
            # A folder called backup is a copy of another login, never an account of its own.
            if name.startswith(".claude-") and "backup" not in name and os.path.isdir(os.path.join(HOME, name)):
                dirs.append(os.path.join(HOME, name))
        return dirs

    @staticmethod
    def _keychain_service(config_dir):
        if config_dir == os.path.join(HOME, ".claude"):
            return "Claude Code-credentials"
        return "Claude Code-credentials-" + hashlib.sha256(config_dir.encode()).hexdigest()[:8]

    @staticmethod
    def _claude_profile_path(config_dir):
        """Where Claude Code keeps oauthAccount for a folder. The main folder is the
        exception: its profile is ~/.claude.json, next to the folder, not inside it.
        Setting CLAUDE_CONFIG_DIR=~/.claude would make Claude Code look inside and
        report "not logged in" (measured 2026-09-18), so main is never given the
        variable."""
        if config_dir == MAIN_CLAUDE_DIR:
            return os.path.join(HOME, ".claude.json")
        return os.path.join(config_dir, ".claude.json")

    @staticmethod
    def claude_env(config_dir):
        """Environment for running the claude CLI against one folder."""
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)
        if config_dir != MAIN_CLAUDE_DIR:
            env["CLAUDE_CONFIG_DIR"] = config_dir
        return env

    @classmethod
    def claude_seconds_left(cls, config_dir):
        """Seconds until this folder's access token expires; None when there is no login."""
        a = cls._claude_code_account(config_dir)
        if not a or not a.get("expires_at"):
            return None
        return a["expires_at"] / 1000 - time.time()

    @classmethod
    def _claude_code_account(cls, config_dir):
        service = cls._keychain_service(config_dir)
        o = None
        try:
            raw = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                 capture_output=True, text=True, timeout=15).stdout
            o = json.loads(raw)["claudeAiOauth"]
            source = f"macOS keychain · {service}"
        except Exception:
            p = os.path.join(config_dir, ".credentials.json")
            if os.path.exists(p):
                try:
                    o = json.load(open(p))["claudeAiOauth"]
                    source = p.replace(HOME, "~")
                except Exception:
                    o = None
        if not o:
            return None
        # Who this is, from the profile Claude Code caches next to the token.
        who = {}
        try:
            who = json.load(open(cls._claude_profile_path(config_dir))).get("oauthAccount") or {}
        except Exception:
            pass
        short = os.path.basename(config_dir).replace(".claude", "").lstrip("-") or "main"
        # A real organisation is named; a personal org ("<email>'s Organization") is
        # just the email.
        org = who.get("organizationName")
        if who.get("organizationType") not in ("claude_team", "claude_enterprise"):
            org = who.get("emailAddress")
        label = f"Claude · {short}" + (f" · {org}" if org else "")
        return {
            "id": f"anthropic:claude-code:{short}",
            "kind": "anthropic",
            "owner": "external",
            "label": label,
            "email": who.get("emailAddress"),
            "access_token": o.get("accessToken"),
            "refresh_token": None,          # deliberately not taken: Claude Code owns it
            "expires_at": o.get("expiresAt"),
            "plan": o.get("subscriptionType"),
            "tier": o.get("rateLimitTier"),
            "source": source,
            "config_dir": config_dir,
            "fix": "Run: claude auth login" if config_dir == MAIN_CLAUDE_DIR
                   else f"Run: CLAUDE_CONFIG_DIR={config_dir.replace(HOME, '~')} claude auth login",
        }

    @classmethod
    def _claude_code(cls):
        """One row per distinct account. A backup folder holding a copy of the same
        login (same email, same plan) is skipped, the first folder wins."""
        out, seen = [], set()
        for d in cls._claude_code_dirs():
            a = cls._claude_code_account(d)
            if not a:
                continue
            key = (a.get("email"), a.get("plan"), a.get("tier"))
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
        return out

    @staticmethod
    def _codex():
        p = os.path.join(HOME, ".codex", "auth.json")
        if not os.path.exists(p):
            return None
        try:
            a = json.load(open(p))
            t = a.get("tokens") or {}
        except Exception:
            return None
        if not t.get("access_token"):
            return None
        return {
            "id": "openai:codex",
            "kind": "openai",
            "owner": "external",
            "label": "Codex (this Mac)",
            "access_token": t.get("access_token"),
            "id_token": t.get("id_token"),
            "refresh_token": None,          # Codex owns it
            "account_id": t.get("account_id"),
            "source": "~/.codex/auth.json",
            "fix": "Run: codex login",
        }

    # ---------------- asking a CLI to refresh its own token ----------------

    @staticmethod
    def _run(cmd, env, cwd):
        try:
            r = subprocess.run(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=REFRESH_TIMEOUT)
            return r.returncode == 0, (r.stdout + r.stderr)[-300:]
        except Exception as exc:
            return False, str(exc)[:300]

    @classmethod
    def _refresh_claude_code(cls, config_dir):
        """A one-line Haiku call in that folder. Claude Code sees its access token
        has expired, rotates the pair with its own refresh token and writes the
        result to its keychain entry. Nothing else loads: no hooks, no MCP
        servers, no session file. Measured 2026-09-18: 5.7 s, token valid 8 h."""
        cmd = ["claude", "-p", "reply with ok", "--model", "haiku", "--max-turns", "1",
               "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
               "--disable-slash-commands", "--no-chrome"]
        return cls._run(cmd, cls.claude_env(config_dir), cwd="/private/tmp")

    @classmethod
    def _refresh_codex(cls):
        cmd = ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
               "--ignore-rules", "-s", "read-only", "reply with ok"]
        env = dict(os.environ)
        env.pop("CODEX_HOME", None)
        return cls._run(cmd, env, cwd="/private/tmp")

    def refresh_external(self, account):
        """Ask the owning CLI to refresh this account. Returns the re-read account
        (fresh access token) or None. Serialised per folder; a failure blocks
        retries for REFRESH_RETRY_SECONDS so a revoked login is not hammered."""
        key = account.get("config_dir") or account.get("id")
        with _refresh_lock:
            lock = _refresh_locks.setdefault(key, threading.Lock())
        with lock:
            if time.time() - _refresh_failed_at.get(key, 0) < REFRESH_RETRY_SECONDS:
                return None
            if account.get("kind") == "anthropic" and account.get("config_dir"):
                cfg = account["config_dir"]
                with claude_login_lock(cfg):
                    fresh = self._claude_code_account(cfg)      # another process may have renewed it meanwhile
                    if not (fresh and fresh.get("expires_at") and fresh["expires_at"] / 1000 > time.time() + 60):
                        ok, out = self._refresh_claude_code(cfg)
                        log_refresh(cfg, "server", ok and "could not be refreshed" not in out, out)
                        fresh = self._claude_code_account(cfg) if ok else None
                if fresh and fresh.get("expires_at") and fresh["expires_at"] / 1000 > time.time() + 60:
                    return fresh
            elif account.get("kind") == "openai":
                ok, out = self._refresh_codex()
                fresh = self._codex() if ok else None
                if fresh and fresh.get("access_token") != account.get("access_token"):
                    return fresh
            _refresh_failed_at[key] = time.time()
            return None

    @staticmethod
    def jwt_exp(token):
        """Expiry (unix seconds) of a JWT, or None."""
        try:
            seg = token.split(".")[1]
            seg += "=" * (-len(seg) % 4)
            return json.loads(base64.urlsafe_b64decode(seg)).get("exp")
        except Exception:
            return None

    def discover(self):
        """Every subscription this Mac can already see, plus the ones we signed in ourselves."""
        found = []
        try:
            found.extend(self._claude_code())
        except Exception:
            pass
        try:
            a = self._codex()
        except Exception:
            a = None
        if a:
            found.append(a)

        seen_tokens = {a.get("access_token") for a in found if a.get("access_token")}
        for a in self.load_own():
            if a.get("access_token") in seen_tokens:
                continue
            a["owner"] = "self"
            found.append(a)

        self.accounts = found
        return found
