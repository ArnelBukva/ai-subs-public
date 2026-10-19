"""Provider adapters. Each one answers: what plan is this, and how full is it right now?

Every adapter returns the same shape so the UI never learns a provider's quirks:

    {
      "provider":  "Anthropic",
      "account":   "you@example.com",
      "plan":      "Max 20x",
      "status":    "ok" | "needs_login" | "error",
      "windows":   [{"label", "percentUsed", "resetsAt", "resetsIn"}],
      "extra":     {"label", "spent", "limit", "currency"} | None,
      "note":      str | None,
    }

Adding a provider means adding one function here and one line in ADAPTERS.
Nothing else in the app changes.
"""

import base64
import datetime
import json
import os
import subprocess
import time

# Claude Code's public OAuth client. Same value the CLI uses for its own refresh.
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
OPENAI_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _curl(url, headers=None, method="GET", body=None, timeout=20, want_headers=False):
    """macOS system python ships without a usable CA bundle, so shell out to curl."""
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "--max-time", str(timeout), url]
    if want_headers:
        cmd += ["-D", "-"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if method != "GET":
        cmd += ["-X", method]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    parts = r.stdout.rsplit("\n", 1)
    if len(parts) != 2:
        return (0, r.stderr[:200] or "no response", {}) if want_headers else (0, r.stderr[:200] or "no response")
    text, code = parts

    got = {}
    if want_headers:
        # curl -D - prefixes the body with the header block; split on the blank line.
        head, sep, text = text.partition("\r\n\r\n")
        if not sep:
            head, sep, text = text.partition("\n\n")
        for line in head.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                got[k.strip().lower()] = v.strip()

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = text[:400]
    return (int(code or 0), parsed, got) if want_headers else (int(code or 0), parsed)


# Every real Claude response carries the account's limit state in these headers.
# This is how CLIProxyAPI and the tools built on it read quota: no extra request,
# so no separate rate limit to trip. Status is allowed / allowed_warning /
# rejected, and the reset values are unix seconds.
def _windows_from_headers(got):
    windows = []
    for prefix, label in (("5h", "5 hours"), ("7d", "Week · all models")):
        status = got.get(f"anthropic-ratelimit-unified-{prefix}-status")
        reset = got.get(f"anthropic-ratelimit-unified-{prefix}-reset")
        if not status and not reset:
            continue
        iso, human = _until(int(reset)) if (reset or "").isdigit() else (None, None)
        pct = {"allowed": 0, "allowed_warning": 90, "rejected": 100}.get(status)
        windows.append({"label": label, "percentUsed": pct, "status": status,
                        "resetsAt": iso, "resetsIn": human, "approx": True})
    return windows


def _human(seconds):
    if seconds is None or seconds < 0:
        return None
    d, rem = divmod(int(seconds), 86400)
    h, m = divmod(rem // 60, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _until(iso_or_epoch):
    """Accept an ISO string or a unix epoch; return (iso, human countdown)."""
    if iso_or_epoch is None:
        return None, None
    try:
        if isinstance(iso_or_epoch, (int, float)):
            dt = datetime.datetime.fromtimestamp(iso_or_epoch)
        else:
            s = str(iso_or_epoch).replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
    except Exception:
        return str(iso_or_epoch), None
    return dt.isoformat(timespec="seconds"), _human((dt - datetime.datetime.now()).total_seconds())


# --------------------------------------------------------------------------
# Anthropic — Claude Pro / Max
# --------------------------------------------------------------------------

def _anthropic_refresh(account, store):
    """Swap the refresh token for a fresh access token and persist the rotation.

    Anthropic rotates the refresh token on every use, so a refresh that is not
    written back logs the account out. Write first, then use.
    """
    refresh = account.get("refresh_token")
    if not refresh:
        return None
    code, body = _curl(
        ANTHROPIC_TOKEN_URL,
        headers={"Accept": "application/json, text/plain, */*",
                 "Content-Type": "application/json",
                 "User-Agent": "axios/1.15.2",
                 "Connection": "close"},
        method="POST",
        body={
            "client_id": ANTHROPIC_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "scope": ANTHROPIC_SCOPES,
        },
    )
    if code != 200 or not isinstance(body, dict) or "access_token" not in body:
        return None
    account["access_token"] = body["access_token"]
    account["refresh_token"] = body.get("refresh_token", refresh)
    account["expires_at"] = int(time.time() * 1000) + int(body.get("expires_in", 3600)) * 1000
    store.save(account)
    return account["access_token"]


PLAN_NAMES = {
    "default_claude_max_20x": "Max 20x",
    "default_claude_max_5x": "Max 5x",
    "default_claude_pro": "Pro",
}


def anthropic(account, store):
    label = account.get("label") or account.get("email") or "Claude account"
    plan = PLAN_NAMES.get(account.get("tier") or "") or (account.get("plan") or "?").title()
    if account.get("plan") == "team":
        # A Team seat is rate-limited like a Max tier; say both.
        plan = "Team" + (f" · {PLAN_NAMES[account['tier']]} limits" if account.get("tier") in PLAN_NAMES else "")
    token = account.get("access_token")
    expires = account.get("expires_at")

    if token and expires and expires / 1000 < time.time() + 60:
        # Expired. A token we own we refresh ourselves. An external one belongs to
        # Claude Code, so Claude Code is asked to refresh it (one tiny call in its
        # own folder); rotating it ourselves would sign that CLI out.
        if account.get("owner") == "self":
            token = _anthropic_refresh(account, store)
        else:
            fresh = store.refresh_external(account)
            token = fresh.get("access_token") if fresh else None

    if not token:
        return {"provider": "Anthropic", "account": label, "plan": plan,
                "status": "needs_login", "windows": [], "extra": None,
                "note": "No token stored. Run: ai-subs login anthropic"}

    code, body = _curl(ANTHROPIC_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-cli/2.1.270 (external, cli)",
        "Accept": "application/json",
    })

    if code == 401:
        if account.get("owner") == "self":
            token = _anthropic_refresh(account, store)
        else:
            fresh = store.refresh_external(account)
            token = fresh.get("access_token") if fresh else None
        if token:
            code, body = _curl(ANTHROPIC_USAGE_URL, headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-cli/2.1.270 (external, cli)",
                "Accept": "application/json",
            })

    if code != 200 or not isinstance(body, dict):
        msg = body.get("error", {}).get("message") if isinstance(body, dict) else str(body)[:120]
        if code == 401:
            return {"provider": "Anthropic", "account": label, "plan": plan,
                    "status": "needs_login", "windows": [], "extra": None, "note": msg}

        # The usage endpoint is throttled or unhappy. Fall back to the headers a
        # normal request carries: one cheap call, and it cannot hit the usage
        # endpoint's separate limit.
        hcode, _, got = _curl(
            "https://api.anthropic.com/v1/messages",
            headers={"Authorization": f"Bearer {token}",
                     "anthropic-beta": "oauth-2025-04-20",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json",
                     "User-Agent": "claude-cli/2.1.270 (external, cli)"},
            method="POST",
            body={"model": "claude-sonnet-4-5-20250929", "max_tokens": 1,
                  "messages": [{"role": "user", "content": "."}]},
            want_headers=True)
        windows = _windows_from_headers(got)
        if windows:
            return {"provider": "Anthropic", "account": label, "plan": plan, "status": "ok",
                    "windows": windows, "extra": None,
                    "note": "Read from response headers — status, not an exact percent"}

        return {"provider": "Anthropic", "account": label, "plan": plan,
                "status": "error", "windows": [], "extra": None,
                "note": msg or f"HTTP {code}"}

    # The "limits" list is what Claude Code itself shows: the 5-hour session, the
    # all-model week, and a week scoped to one model (Fable today). Older
    # responses only carry five_hour/seven_day; fall back to those.
    windows = []
    KIND_LABEL = {"session": "5 hours", "weekly_all": "Week · all models"}
    for lim in body.get("limits") or []:
        if not isinstance(lim, dict) or lim.get("percent") is None:
            continue
        scope_model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        wlabel = f"Week · {scope_model}" if scope_model else KIND_LABEL.get(lim.get("kind"), lim.get("kind", "?").replace("_", " "))
        iso, human = _until(lim.get("resets_at"))
        windows.append({"label": wlabel, "percentUsed": lim.get("percent"), "severity": lim.get("severity"),
                        "scope": scope_model, "resetsAt": iso, "resetsIn": human})
    if not windows:
        for key, wlabel in (("five_hour", "5 hours"), ("seven_day", "Week · all models")):
            entry = body.get(key)
            if isinstance(entry, dict) and entry.get("utilization") is not None:
                iso, human = _until(entry.get("resets_at"))
                windows.append({"label": wlabel, "percentUsed": entry.get("utilization"),
                                "scope": None, "resetsAt": iso, "resetsIn": human})
    # The model-scoped window first: it is the one that stops the expensive model.
    windows.sort(key=lambda w: (w.get("scope") is None, w["label"] != "Week · all models"))

    extra = None
    eu = body.get("extra_usage") or body.get("extraUsage")
    if isinstance(eu, dict):
        extra = {"label": "Extra usage", "spent": eu.get("spent"),
                 "limit": eu.get("monthly_limit") or eu.get("monthlyLimit"),
                 "currency": eu.get("currency", "USD"),
                 "enabled": eu.get("enabled", False)}

    return {"provider": "Anthropic", "account": label, "plan": plan,
            "status": "ok", "windows": windows, "extra": extra, "note": None}


# --------------------------------------------------------------------------
# OpenAI — ChatGPT Plus / Pro, as used by Codex
# --------------------------------------------------------------------------

def _openai_plan(id_token):
    try:
        seg = id_token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
        auth = claims.get("https://api.openai.com/auth", {})
        return auth.get("chatgpt_plan_type", "?").title(), claims.get("email"), auth
    except Exception:
        return "?", None, {}


def openai(account, store):
    token = account.get("access_token")
    account_id = account.get("account_id")
    plan, email, auth = _openai_plan(account.get("id_token") or "")
    label = account.get("label") or email or "ChatGPT account"

    if not token:
        return {"provider": "OpenAI", "account": label, "plan": plan,
                "status": "needs_login", "windows": [], "extra": None,
                "note": "No token stored. Sign in with: codex login"}

    def usage(tok):
        return _curl(OPENAI_USAGE_URL, headers={
            "Authorization": f"Bearer {tok}",
            "chatgpt-account-id": account_id or "",
            "User-Agent": "codex_cli_rs/0.45.0 (Mac OS 15.6; arm64)",
            "originator": "codex_cli_rs",
        })

    # Codex's access token lives about ten days. Inside the last day, or on a
    # 401, ask Codex to rotate it (one tiny `codex exec`) before giving up.
    exp = store.jwt_exp(token)
    if exp and exp < time.time() + 86400 and account.get("owner") == "external":
        fresh = store.refresh_external(account)
        if fresh:
            token, account_id = fresh["access_token"], fresh.get("account_id") or account_id

    code, body = usage(token)
    if code in (401, 403) and account.get("owner") == "external":
        fresh = store.refresh_external(account)
        if fresh:
            token, account_id = fresh["access_token"], fresh.get("account_id") or account_id
            code, body = usage(token)

    if code != 200 or not isinstance(body, dict):
        return {"provider": "OpenAI", "account": label, "plan": plan,
                "status": "needs_login" if code in (401, 403) else "error",
                "windows": [], "extra": None,
                "note": f"HTTP {code}. Re-run: codex login" if code in (401, 403) else f"HTTP {code}"}

    windows = []
    rl = body.get("rate_limit") or {}
    for key, name in (("primary_window", "Primary"), ("secondary_window", "Secondary")):
        w = rl.get(key)
        if not w:
            continue
        span = w.get("limit_window_seconds") or 0
        span_label = f"{round(span / 86400)} days" if span >= 86400 else f"{round(span / 3600)} hours"
        iso, human = _until(w.get("reset_at"))
        windows.append({"label": f"{name} · {span_label}", "percentUsed": w.get("used_percent"),
                        "resetsAt": iso, "resetsIn": human})

    extra = None
    credits = body.get("credits") or {}
    if credits:
        extra = {"label": "Credits", "spent": None, "limit": credits.get("balance"),
                 "currency": "credits", "enabled": bool(credits.get("has_credits"))}

    note = None
    if rl.get("limit_reached"):
        note = "Limit reached"
    unavailable = [m for m, v in (body.get("model_usage") or {}).items() if v.get("available") is False]
    if unavailable:
        note = (note + " · " if note else "") + "unavailable: " + ", ".join(unavailable)

    renews, _ = _until(auth.get("chatgpt_subscription_active_until"))

    return {"provider": "OpenAI", "account": label, "plan": plan, "status": "ok",
            "windows": windows, "extra": extra, "note": note, "renewsAt": renews}


ADAPTERS = {
    "anthropic": anthropic,
    "openai": openai,
}
