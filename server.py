#!/usr/bin/env python3
"""ai-subs — accounts & sessions: how full every AI subscription is, and the sessions to carry
from an account that ran out to one with room. One page at http://localhost:8787.

    python3 server.py [--port 8787] [--once]     (the launch agent runs this)

--once prints the JSON to stdout and exits, for cron or a status bar.
No dependencies beyond the python3 that ships with macOS.
"""

import argparse
import concurrent.futures
import time
import datetime
import http.server
import json
import os
import socketserver
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from providers import ADAPTERS          # noqa: E402
from store import Store                 # noqa: E402
import weeks                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# How often each provider is asked. Measured 2026-09-17: eight reads of the
# Anthropic usage endpoint in 40 minutes, no 429. Five minutes is well inside
# that; if a 429 ever comes, BACKOFF_SECONDS below keeps the last good answer.
POLL_SECONDS = {"anthropic": 300, "openai": 60}
DEFAULT_POLL = 300
BACKOFF_SECONDS = 1800          # after a 429, wait this long before trying again

_per_account = {}               # id -> {"at": float, "row": dict, "until": float}
_lock = threading.Lock()

# The last good answer survives a restart: the usage endpoint allows about one
# call an hour, so a restart that forgot the cache would show nothing for an hour.
STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "ai-subs")
CACHE_FILE = os.path.join(STATE_DIR, "cache.json")


def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            _per_account.update(json.load(f))
    except Exception:
        pass


def _save_cache():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_per_account, f)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


_load_cache()


def _fresh_enough(entry, kind, force):
    import time
    if not entry:
        return False
    now = time.time()
    if now < entry.get("until", 0):     # throttled: do not ask again yet, even on force
        return True
    if force:
        return False
    return now - entry["at"] < POLL_SECONDS.get(kind, DEFAULT_POLL)


def collect(force=False):
    """Poll every account in parallel. One slow provider must not stall the page."""
    store = Store()
    accounts = store.discover()

    def one(account):
        acct_id = account.get("id")
        kind = account.get("kind")
        with _lock:
            cached = _per_account.get(acct_id)
        if _fresh_enough(cached, kind, force):
            row = dict(cached["row"])
            row["account"] = account.get("label") or row.get("account")   # names may change; numbers are what we cache
            row["asOf"] = datetime.datetime.fromtimestamp(cached["at"]).isoformat(timespec="seconds")
            if time.time() < cached.get("until", 0):
                row["throttled"] = True
            return row

        adapter = ADAPTERS.get(kind)
        if not adapter:
            return {"provider": account.get("kind", "?"), "account": account.get("label", "?"),
                    "plan": "?", "status": "error", "windows": [], "extra": None,
                    "note": "No adapter for this provider"}
        try:
            row = adapter(account, store)
        except Exception as exc:                                  # never let one provider break the page
            row = {"provider": account.get("kind", "?").title(), "account": account.get("label", "?"),
                   "plan": "?", "status": "error", "windows": [], "extra": None, "note": str(exc)[:160]}
        row["id"] = acct_id
        row["owner"] = account.get("owner")
        row["source"] = account.get("source")
        row["orgUuid"] = account.get("org_uuid")     # how the page joins this row to the app's account folder
        if row.get("status") == "needs_login" and account.get("fix"):
            row["note"] = account["fix"]

        throttled = row.get("status") == "error" and "rate limit" in (row.get("note") or "").lower()
        with _lock:
            if throttled and cached and cached["row"].get("status") == "ok":
                # Keep the last real numbers rather than replacing them with an error.
                _per_account[acct_id] = {"at": cached["at"], "row": cached["row"],
                                         "until": time.time() + BACKOFF_SECONDS}
                row = dict(cached["row"])
                row["asOf"] = datetime.datetime.fromtimestamp(cached["at"]).isoformat(timespec="seconds")
                row["throttled"] = True
            else:
                _per_account[acct_id] = {"at": time.time(), "row": row,
                                         "until": time.time() + BACKOFF_SECONDS if throttled else 0}
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, accounts))
    with _lock:
        _save_cache()

    def fullest(row):
        """The window closest to its limit. That window is what stops you working."""
        return max([w.get("percentUsed") or 0 for w in row.get("windows", [])], default=-1)

    for row in rows:
        row["fullest"] = fullest(row)
        row["headroom"] = 100 - row["fullest"] if row["fullest"] >= 0 else None

    rows.sort(key=lambda r: (r.get("status") != "ok", -r["fullest"]))

    usable = [r for r in rows if r["status"] == "ok" and r["fullest"] < 100]
    if usable:
        best = max(usable, key=lambda r: r["headroom"])
        pick = {"id": best["id"], "provider": best["provider"], "account": best["account"],
                "headroom": best["headroom"]}
    else:
        soonest = None
        for r in rows:
            for w in r.get("windows", []):
                if w.get("resetsAt") and (soonest is None or w["resetsAt"] < soonest["at"]):
                    soonest = {"at": w["resetsAt"], "in": w.get("resetsIn"), "account": r["account"]}
        pick = {"id": None, "exhausted": True, "nextReset": soonest}

    try:
        weeks.record(rows)            # weekly runway history; never allowed to break the usage read
    except Exception as exc:
        print(f"weeks.record failed: {exc}", flush=True)

    return {"generatedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "accounts": rows, "pick": pick}


import sessions_web                     # noqa: E402  the session picker: page, session list, actions
sessions_web.USAGE_FN = collect


def auto_archive_loop():
    """Every hour, when switched on: archive sessions idle past the set days, in every account on this Mac.
    Each check leaves {at, archived, skipped} in IDLE_STATUS, which the page shows, so a run is visible."""
    S = sessions_web.S
    while True:
        try:
            fixed = S.fix_moved_folders()   # moved sessions keep their real project, even if the app wrote over it
            if fixed:
                print(f"moved-session folders restored: {fixed}", flush=True)
            if S.idle_settings()["enabled"]:
                done, skipped = S.archive_idle()
                status = {"at": round(time.time()), "archived": len(done), "skipped": len(skipped)}
                os.makedirs(os.path.dirname(S.IDLE_STATUS), exist_ok=True)
                with open(S.IDLE_STATUS + ".tmp", "w") as f:
                    json.dump(status, f)
                os.replace(S.IDLE_STATUS + ".tmp", S.IDLE_STATUS)
                if done:
                    print(f"auto-archive: {len(done)} sessions", flush=True)
        except Exception as exc:
            print(f"auto-archive failed: {exc}", flush=True)
        time.sleep(3600)


class Handler(sessions_web.Handler):
    """One server for the whole page: usage from the collector above, everything else from sessions_web."""

    def do_GET(self):
        if self.path.startswith("/api/usage"):
            return self.send(200, collect(force="refresh=1" in self.path))
        if self.path.startswith("/api/weeks"):
            return self.send(200, weeks.summary())
        return super().do_GET()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    ap.add_argument("--once", action="store_true", help="print JSON and exit")
    args = ap.parse_args()

    if args.once:
        print(json.dumps(collect(force=True), indent=1))
        return

    threading.Thread(target=auto_archive_loop, daemon=True).start()
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        print(f"ai-subs (accounts & sessions) on http://127.0.0.1:{args.port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
