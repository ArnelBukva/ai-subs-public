#!/usr/bin/env python3
"""Accounts & sessions — subscriptions usage and the session picker on one page.

    Served by server.py on http://localhost:8787.

Shows how full every AI subscription is (read from the ai-subs server) and carries
sessions from an account that ran out to one that has room:
pick the sessions, then move each one (the whole conversation) or copy its handoff packet.
It reuses everything in sessions.py: the listing, the live summaries, the packet and the move.
Binds 127.0.0.1 only; every action checks the path is a transcript under ~/.claude or ~/.codex.
"""
import http.server, importlib.util, json, os, subprocess, sys, threading, time, urllib.parse

HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location("sessions_menu", os.path.join(HERE, "sessions.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

CARRIED = os.path.join(S.CACHE_DIR, "carried.json")
WRITE_LOCK = threading.Lock()   # the server is threaded: moves and carried.json writes go one at a time
USAGE_FN = None   # server.py sets this to its collector, so usage is read in-process
ROOTS = (os.path.realpath(S.CLAUDE_ROOT), os.path.realpath(S.CODEX_ROOT))


def load_json(p, default):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return default


def save_json(p, d):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, p)


def safe_path(p):
    r = os.path.realpath(p or "")
    if r.endswith(".jsonl") and r.startswith(ROOTS) and os.path.isfile(r):
        return r
    return None


# ---------- accounts ----------

def account_list():
    """[{key, acct, org, email, org_name, label, sessions}] from the app's folders, named from the login files."""
    names = {}
    home = os.path.expanduser("~")
    for cfg in [os.path.join(home, ".claude.json")] + [os.path.join(home, n, ".claude.json") for n in os.listdir(home) if n.startswith(".claude-")]:
        try:
            a = json.load(open(cfg)).get("oauthAccount") or {}
            if a.get("accountUuid") and a.get("organizationUuid"):
                names[(a["accountUuid"], a["organizationUuid"])] = (a.get("emailAddress") or "", a.get("organizationName") or "")
        except Exception:
            continue
    out = []
    if not os.path.isdir(S.APP_SESSIONS):
        return out
    for acct in sorted(os.listdir(S.APP_SESSIONS)):
        ad = os.path.join(S.APP_SESSIONS, acct)
        if not os.path.isdir(ad):
            continue
        for org in sorted(os.listdir(ad)):
            od = os.path.join(ad, org)
            if not os.path.isdir(od):
                continue
            n = sum(1 for f in os.listdir(od) if f.startswith("local_") and f.endswith(".json"))
            email, org_name = names.get((acct, org), ("", ""))
            if not email:   # the same person in another org still tells us the email
                email = next((e for (a, _), (e, _) in names.items() if a == acct and e), "")
            if email and org_name and org_name != f"{email}'s Organization":
                label = f"{email} · {org_name}"
            else:
                label = email or f"Account {acct[:8]}"
            out.append({"key": f"{acct}/{org}", "acct": acct, "org": org, "email": email,
                        "org_name": org_name, "label": label, "sessions": n})
    return out


def record_index():
    """cliSessionId -> [{key, active, archived}, ...] for every app record on this Mac."""
    idx = {}
    for acct, org, _, d in S.app_records():
        if d.get("cliSessionId"):
            idx.setdefault(d["cliSessionId"], []).append({"key": f"{acct}/{org}",
                                                          "active": d.get("lastActivityAt") or d.get("createdAt") or 0,
                                                          "archived": bool(d.get("isArchived"))})
    return idx


def home_record(idx, r):
    """The app record of a Claude session that saw activity last, or None.
    A session belongs to the account you last worked in it from. A move gives the new copy the
    current time, so from then on the session belongs to the target, until you work in another account.
    The Remote Control line in the transcript cannot say this: measured 2026-09-28, it named the main
    account on 77 of 82 sessions that only ever ran in the personal account, and personal on none."""
    recs = idx.get(os.path.basename(r["path"])[:-6], []) if r["kind"] == "claude" else []
    return max(recs, key=lambda x: x["active"]) if recs else None


def fetch_usage(refresh=False):
    """The usage payload {accounts, pick, generatedAt} from the collector in server.py, or None."""
    try:
        return USAGE_FN(force=refresh) if USAGE_FN else None
    except Exception:
        return None


def usage_by_account(accounts, usage):
    """Match the ai-subs rows to app accounts. Tags each matched row with appKey and returns
    account key -> {nick, percent, window, resetsIn, full}."""
    out = {}
    rows = [r for r in (usage or {}).get("accounts") or [] if r.get("provider") == "Anthropic"]
    for a in accounts:
        match = None
        for r in rows:
            name = (r.get("account") or "").lower()
            if a["org_name"] and a["org_name"].lower() in name and "team" in name:
                match = r
                break
        if not match and a["email"]:
            match = next((r for r in rows if a["email"].lower() in (r.get("account") or "").lower()
                          and "team" not in (r.get("account") or "").lower()
                          and a["org_name"] in ("", f"{a['email']}'s Organization")), None)
        if not match or match.get("appKey"):
            continue
        match["appKey"] = a["key"]
        wins = [w for w in match.get("windows") or [] if w.get("percentUsed") is not None and not w.get("scope")]
        wins = wins or [w for w in match.get("windows") or [] if w.get("percentUsed") is not None]
        parts = [x.strip() for x in (match.get("account") or "").split("·")]
        nick = " · ".join(parts[1:-1]) if len(parts) > 2 else ""   # "Claude · main · x@y" -> "main"
        if nick == "team" and parts:
            nick = "team · " + parts[-1]
        top = max(wins, key=lambda w: w["percentUsed"]) if wins else {}
        out[a["key"]] = {"nick": nick, "percent": top.get("percentUsed"), "window": top.get("label"),
                         "resetsIn": top.get("resetsIn"), "full": (top.get("percentUsed") or 0) >= 95}
    return out


# ---------- sessions ----------

def owner_from_tail(path):
    txt = S.read_slice(path, max(0, os.path.getsize(path) - S.TAIL_BYTES), S.TAIL_BYTES)
    for line in reversed(txt.split("\n")):
        if '"bridge-session"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ownerAccountUuid") and d.get("ownerOrganizationUuid"):
                return f"{d['ownerAccountUuid']}/{d['ownerOrganizationUuid']}"
    return None


_LAST_YOU = {}   # path -> ((size, mtime), text): re-read only when the transcript changed


def last_user(r):
    stamp = (r["size"], r["mtime"])
    hit = _LAST_YOU.get(r["path"])
    if not hit or hit[0] != stamp:
        text = next((S.clip(" ".join(t.split()), 260) for role, t in reversed(S.tail_turns(r["path"], r["kind"], want=8))
                     if role == "user"), "")
        hit = _LAST_YOU[r["path"]] = (stamp, text)
    return hit[1]


def sessions_payload(refresh=False):
    idx = record_index()
    # archived in the account it was last used in: nothing to carry over
    rows = S.collect(S.PAGE_SHOW, keep=lambda r: not (home_record(idx, r) or {}).get("archived"))
    cache = S.load_summaries()
    carried = load_json(CARRIED, {})
    now = time.time()
    out = []
    seen = set()
    for r in rows:
        sid = os.path.basename(r["path"])[:-6]
        if sid in seen:
            continue   # the app keeps a copy of a transcript in a second project folder: list the session once
        seen.add(sid)
        recs = idx.get(sid, []) if r["kind"] == "claude" else []
        top = home_record(idx, r)
        # Only a session the app has no record of (a Terminal run) falls back to its Remote Control owner.
        home = top["key"] if top else (owner_from_tail(r["path"]) if r["kind"] == "claude" else None)
        c = carried.get(r["path"]) or {}
        if c.get("how") == "moved" and top and c.get("at", 0) * 1000 >= top["active"] and any(x["key"] == c.get("to") for x in recs):
            home = c["to"]   # moved onto a copy the app reopened: yours there until you work in another account
        s = cache.get(r["path"]) or {}
        out.append({
            "path": r["path"], "kind": r["kind"], "project": r["project"], "cwd": r["cwd"],
            "title": r["title"] if r["named"] or not s.get("title") else s["title"],
            "now": s.get("now") or "", "recent": s.get("recent") or [], "state": s.get("state") or "",
            "next": s.get("next") or "", "stale": bool(s) and s.get("size") != r["size"],
            "at": r["last"], "ago": S.relative(r["last"]), "active": now - r["last"] < 120,
            "sizeKb": r["size"] // 1024, "lastYou": last_user(r),
            "account": home, "inAccounts": list(dict.fromkeys(x["key"] for x in recs)),
            "carried": carried.get(r["path"]),
        })
    homes = {x["account"] for x in out} | {k for x in out for k in x["inAccounts"]}
    accounts = [a for a in account_list() if a["sessions"] >= 3 or a["key"] in homes]
    raw = fetch_usage(refresh)
    if raw:   # the collector hands out its cached rows: tag copies. A tag left on the cache made every later match skip.
        raw = {**raw, "accounts": [{k: v for k, v in r.items() if k != "appKey"} for r in raw.get("accounts") or []]}
    usage = usage_by_account(accounts, raw)
    for a in accounts:
        if usage.get(a["key"], {}).get("nick"):
            a["label"] = usage[a["key"]]["nick"]
            a["detail"] = a["email"]
        else:
            a["detail"] = a["org_name"] if a["org_name"] and "'s Organization" not in a["org_name"] else ""
    pick = (raw or {}).get("pick") or {}
    rows = (raw or {}).get("accounts") or []
    pick_row = next((r for r in rows if pick.get("id") and r.get("id") == pick.get("id")), None)
    if pick_row and pick_row.get("appKey"):
        pick["appKey"] = pick_row["appKey"]
    dups = {}
    for g in S.duplicate_records():
        dups.setdefault(g["key"], []).append({"title": g["title"], "extra": len(g["extras"])})
    signed_in = S.app_account()
    return {"generatedAt": now, "sessions": out, "accounts": accounts, "usage": usage, "duplicates": dups,
            "appAccount": "/".join(signed_in) if signed_in else None,
            "autoArchive": {**S.idle_settings(), "last": load_json(S.IDLE_STATUS, None)},
            "subscriptions": rows, "pick": pick, "usageAt": (raw or {}).get("generatedAt"), "usageDown": raw is None}


def mark_carried(path, how, to=None):
    with WRITE_LOCK:
        c = load_json(CARRIED, {})
        c[path] = {"how": how, "to": to, "at": time.time()}
        save_json(CARRIED, c)


# ---------- server ----------

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self.send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path in ("/favicon.svg", "/favicon.ico"):
            return self.send(200, open(os.path.join(HERE, "favicon.svg"), "rb").read(), "image/svg+xml")
        if u.path == "/api/sessions":
            return self.send(200, sessions_payload(refresh=(q.get("refresh") or [""])[0] == "1"))
        if u.path == "/api/packet":
            p = safe_path((q.get("path") or [""])[0])
            return self.send(200, {"text": S.packet(p)}) if p else self.send(400, {"error": "bad path"})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        # Same-origin only: a page on another site must not be able to trigger actions here.
        origin = self.headers.get("Origin") or ""
        host = urllib.parse.urlparse(origin).hostname or ""
        if origin and not (host in ("localhost", "127.0.0.1") or host.endswith(".localhost")):
            return self.send(403, {"error": "origin"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send(400, {"error": "bad json"})
        u = urllib.parse.urlparse(self.path).path
        if u == "/api/summarize":
            subprocess.Popen([sys.executable, os.path.join(HERE, "sessions.py"), "summarize"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return self.send(200, {"ok": True})
        if u == "/api/dedupe":
            with WRITE_LOCK:
                n, where = S.remove_duplicates()
            return self.send(200, {"ok": True, "moved": n, "where": where,
                                   "message": f"Removed {n} extra {'copy' if n == 1 else 'copies'}. If any come back, the Claude app was holding them open: quit it, remove again, then reopen it." if n else "No duplicates."})
        p = safe_path(body.get("path"))
        if not p:
            return self.send(400, {"error": "bad path"})
        if u == "/api/copy":
            S.copy(S.packet(p))
            mark_carried(p, "packet")
            return self.send(200, {"ok": True, "message": "Handoff packet copied. Paste it into a new session."})
        if u == "/api/move":
            key = body.get("to") or ""
            acct, _, org = key.partition("/")
            if not (acct and org and os.path.isdir(os.path.join(S.APP_SESSIONS, acct))) or "/.codex/" in p:
                return self.send(400, {"error": "bad target"})
            with WRITE_LOCK:   # two runs at once could both pass the "already there" check
                ok, msg = S.move_record(p, acct, org)
            if ok:
                mark_carried(p, "moved", key)
            return self.send(200, {"ok": ok, "message": msg})
        if u == "/api/resume":
            S.resume_in_terminal(p)
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})
