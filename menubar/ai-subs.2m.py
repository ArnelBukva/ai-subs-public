#!/usr/bin/env python3
# <xbar.title>Accounts & sessions</xbar.title>
# <xbar.version>1.1.0</xbar.version>
# <xbar.desc>How full every AI subscription is, and every recent Claude and Codex session.</xbar.desc>
# <swiftbar.refreshOnOpen>false</swiftbar.refreshOnOpen>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
"""Menu bar view of ai-subs: usage on top, then every recent session (from ../sessions.py).

Reads <page>/api/usage (the server holds the
cache; the usage endpoints allow about one call an hour, so nothing here calls a
provider directly). Rows are native menu text: on macOS 26 SwiftBar shrinks any
row image to the text height, so a drawn card cannot be shown at full size.
"""

import datetime
import json
import os
import subprocess



def _base_url():
    """Same rule as store.base_url: AI_SUBS_URL, else ~/.ai-subs/url, else the server's own address."""
    url = os.environ.get("AI_SUBS_URL")
    if not url:
        try:
            with open(os.path.expanduser("~/.ai-subs/url")) as f:
                url = f.readline().strip()
        except OSError:
            pass
    return (url or "http://localhost:8787").rstrip("/")


DASHBOARD = _base_url()
API = DASHBOARD + "/api/usage"
ACTION = os.path.expanduser("~/.local/bin/ai-subs-menu-action")
STATE = os.path.expanduser("~/.local/state/ai-subs")
LAST = os.path.join(STATE, "last.json")

GREEN, AMBER, RED, GRAY = "#248a3d", "#c66a00", "#d70015", "#6e6e73"
MONO = "Menlo"
BAR_CELLS = 18
PROVIDER_SYMBOL = {"Anthropic": "asterisk", "OpenAI": "sparkle", "Google": "diamond"}
ICON_DIRS = [os.path.join(STATE, "icons"), os.path.join(os.path.dirname(os.path.realpath(__file__)), "icons")]
ICON_FILE = {"Anthropic": "claude-16.png", "OpenAI": "openai-16.png"}


def provider_icon(provider):
    """' image=<base64> width=16 height=16' for a provider with a cached logo, else an SF symbol.

    Logos come from openbrand (the sites' own app icons), rounded like a macOS
    icon. A row image is shown at text height, which is exactly icon size.
    """
    name = ICON_FILE.get(provider)
    for d in ICON_DIRS if name else []:
        path = os.path.join(d, name)
        if os.path.exists(path):
            import base64
            with open(path, "rb") as f:
                return f" image={base64.b64encode(f.read()).decode()} width=16 height=16"
    return f" sfimage={PROVIDER_SYMBOL.get(provider, 'circle')}"


def fetch():
    """The dashboard's JSON, or the last good copy with offline=True."""
    try:
        out = subprocess.run(["curl", "-sk", "--max-time", "8", API],
                             capture_output=True, text=True, timeout=12).stdout
        data = json.loads(out)
        if data.get("accounts") is not None:
            os.makedirs(STATE, exist_ok=True)
            with open(LAST, "w") as f:
                json.dump(data, f)
            return data, False
    except Exception:
        pass
    try:
        with open(LAST) as f:
            return json.load(f), True
    except Exception:
        return {"accounts": [], "pick": {}}, True


def severity(w):
    sev = w.get("severity")
    if sev in ("critical", "warning"):
        return sev
    pct = float(w.get("percentUsed") or 0)
    return "critical" if pct >= 90 else "warning" if pct >= 70 else "normal"


ROW_COLOUR = {"normal": GREEN, "warning": AMBER, "critical": RED}


def bar(pct):
    filled = int(round(BAR_CELLS * min(max(pct, 0), 100) / 100))
    return "█" * filled + "░" * (BAR_CELLS - filled)


def title_line(pick, offline):
    if offline:
        return "Dashboard offline | color=%s sfimage=wifi.exclamationmark" % GRAY
    if pick.get("id"):
        head = int(pick.get("headroom") or 0)
        col = GREEN if head >= 30 else AMBER if head >= 10 else RED
        sym = ("gauge.with.dots.needle.0percent" if head >= 85 else
               "gauge.with.dots.needle.33percent" if head >= 55 else
               "gauge.with.dots.needle.50percent" if head >= 35 else
               "gauge.with.dots.needle.67percent" if head >= 10 else
               "gauge.with.dots.needle.100percent")
        short = (pick.get("account") or "").replace("Claude · ", "").split(" · ")[0]
        return f"{short} {head}% left | color={col} sfimage={sym}"
    if pick.get("exhausted"):
        nr = (pick.get("nextReset") or {}).get("in")
        return f"All full{' · resets in ' + nr if nr else ''} | color={RED} sfimage=gauge.with.dots.needle.100percent"
    return f"No accounts | color={GRAY} sfimage=questionmark.circle"


def account_lines(row):
    ok = row.get("status") == "ok"
    plan = row.get("plan") or ""
    # No color= on this line: SwiftBar wraps coloured titles in a tracking title that
    # re-renders on highlight and loses the logo attachment. Default ink is right anyway.
    yield f"{row.get('account')}  —  {plan} | size=13{provider_icon(row.get('provider'))} href={DASHBOARD}"
    if not ok:
        if row.get("status") == "needs_login":
            cfg = (row.get("note") or "")
            cfg = cfg.split("CLAUDE_CONFIG_DIR=")[1].split()[0] if "CLAUDE_CONFIG_DIR=" in cfg else "~/.claude"
            yield (f"   Signed out · sign in again | color={RED} sfimage=person.crop.circle.badge.exclamationmark "
                   f"bash={ACTION} param1=login param2={cfg} terminal=false")
        else:
            yield f"   {row.get('note') or 'Could not read usage'} | color={GRAY} size=12"
        return
    windows = [w for w in row.get("windows", []) if w.get("percentUsed") is not None]
    if not windows:
        yield f"   No usage windows reported | color={GRAY} size=12"
    for w in windows:
        pct = float(w["percentUsed"])
        sev = severity(w)
        label = (w.get("label") or "").replace("Week · all models", "Week").replace("Primary · ", "")[:14]
        reset = f"  resets in {w['resetsIn']}" if w.get("resetsIn") else ""
        text = f"  {label:<15}{bar(pct)} {int(round(pct)):>3}%{reset}"
        yield f"{text} | font={MONO} size=12 color={ROW_COLOUR[sev]} trim=false href={DASHBOARD}"
    if row.get("throttled"):
        yield f"   Provider is rate-limiting reads; showing the last good numbers | color={GRAY} size=11"


def main():
    data, offline = fetch()
    rows = data.get("accounts", [])
    pick = data.get("pick") or {}

    print(title_line(pick, offline))
    print("---")
    if offline:
        print(f"The dashboard is not running. Showing the last numbers it had. | color={AMBER} sfimage=exclamationmark.triangle.fill")
        print(f"Start the dashboard | bash={ACTION} param1=start terminal=false refresh=true sfimage=play.fill")
        print("---")
    if pick.get("id"):
        print(f"Use now: {pick.get('account')} — {int(pick.get('headroom') or 0)}% of its tightest window left | color={GRAY} size=12")
        print("---")
    for i, row in enumerate(rows):
        if i:
            print("---")
        for line in account_lines(row):
            print(line)
    print("---")
    try:
        ago = datetime.datetime.now() - datetime.datetime.fromisoformat(data.get("generatedAt", ""))
        mins = int(ago.total_seconds() // 60)
        when = "just now" if mins < 1 else f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago"
    except Exception:
        when = "unknown"
    print(f"Numbers from {when}. Claude is re-read every 5 min, Codex every minute. | color={GRAY} size=11")
    print(f"Open accounts & sessions | href={DASHBOARD} sfimage=rectangle.on.rectangle")
    print(f"Refresh usage now | bash={ACTION} param1=refresh terminal=false refresh=true sfimage=arrow.clockwise")
    print(f"Add a Claude account… | bash={ACTION} param1=add terminal=false sfimage=plus.circle")
    print("---")
    sessions_menu()


def sessions_menu():
    """The recent-sessions list, from sessions.py in the repo root. A failure there must not cost the usage view."""
    try:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "sessions.py")
        spec = importlib.util.spec_from_file_location("sessions", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.menu(header=False)
    except Exception as exc:
        print(f"Sessions list failed: {str(exc)[:80]} | color={GRAY} size=12")


if __name__ == "__main__":
    main()
