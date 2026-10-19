"""Weekly runway: how long each account's weekly allowance lasted, week by week.

The collector calls record(rows) on every poll. For each weekly window (Claude's "Week · …",
Codex's "7 days") this keeps one record per week: when it started, the peak it reached, and the
first time it was seen at 90% and at 100%. "Lasted" is start → first seen at 100%.

Stored in ~/.local/state/ai-subs/weeks.json. Only numbers, never content.
Accuracy: the collector polls every few minutes while the Mac is awake, so a week that ran out
while the Mac slept is stamped at the first poll after waking (an upper bound).
"""
import datetime as dt
import json
import os
import threading

STATE = os.path.join(os.path.expanduser("~"), ".local", "state", "ai-subs", "weeks.json")
WEEK = dt.timedelta(days=7)
EARLY_RESET_DROP = 15      # a fall this large inside one week means the provider reset it early
_lock = threading.Lock()


def _load():
    try:
        with open(STATE) as f:
            d = json.load(f)
            d.setdefault("weeks", [])
            return d
    except (OSError, ValueError):
        return {"weeks": []}


def _save(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, STATE)


def _hour(ts):
    """ISO timestamp rounded to the hour, so 23:59:59 and 00:00:00 name the same reset."""
    t = dt.datetime.fromisoformat(ts)
    return (t + dt.timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)


def weekly(label):
    l = (label or "").lower()
    return l.startswith("week") or "7 days" in l


def record(rows, now=None):
    now = now or dt.datetime.now()
    iso = now.isoformat(timespec="seconds")
    with _lock:
        d = _load()
        open_by_key = {w["key"]: w for w in d["weeks"] if not w.get("closed")}
        changed = False
        for r in rows:
            if r.get("status") != "ok" or not r.get("id"):
                continue
            for win in r.get("windows") or []:
                pct, reset = win.get("percentUsed"), win.get("resetsAt")
                if pct is None or not reset or not weekly(win.get("label")):
                    continue
                key = f"{r['id']}|{win['label']}"
                reset_at = _hour(reset)
                cur = open_by_key.get(key)
                new_week = cur is None
                if cur and abs(dt.datetime.fromisoformat(cur["reset_at"]) - reset_at) > dt.timedelta(hours=2):
                    new_week = True                                    # the reset date moved: a new week
                elif cur and pct < cur["last_pct"] - EARLY_RESET_DROP:
                    new_week = True                                    # same date, big drop: reset early
                if new_week:
                    early = bool(cur) and dt.datetime.fromisoformat(cur["reset_at"]) - now > dt.timedelta(hours=2)
                    if cur:
                        cur["closed"] = True
                        cur["ended"] = iso if early else cur["reset_at"]
                        cur["ended_early"] = early
                    start = reset_at - WEEK
                    cur = {"key": key, "account_id": r["id"], "account": r.get("account"), "provider": r.get("provider"),
                           "window": win["label"], "start": (now if early else start).isoformat(timespec="seconds"),
                           "reset_at": reset_at.isoformat(timespec="seconds"), "first_seen": iso,
                           "first_pct": pct, "peak_pct": pct, "last_pct": pct, "last_seen": iso,
                           "at_90": iso if pct >= 90 else None, "at_100": iso if pct >= 100 else None,
                           "partial": cur is None and now - start > dt.timedelta(hours=6)}   # tracking began mid-week
                    d["weeks"].append(cur)
                    open_by_key[key] = cur
                    changed = True
                    continue
                if pct != cur["last_pct"] or (pct >= 90 and not cur["at_90"]) or (pct >= 100 and not cur["at_100"]):
                    changed = True
                cur["last_pct"], cur["last_seen"] = pct, iso
                cur["peak_pct"] = max(cur["peak_pct"], pct)
                cur["account"] = r.get("account") or cur["account"]
                if pct >= 90 and not cur["at_90"]:
                    cur["at_90"] = iso
                if pct >= 100 and not cur["at_100"]:
                    cur["at_100"] = iso
        if changed:
            _save(d)


def _days(a, b):
    return round((dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds() / 86400, 2)


def summary(now=None):
    """Per account and window: every week with how long it lasted, plus a month-by-month average."""
    now = now or dt.datetime.now()
    d = _load()
    out = {}
    for w in d["weeks"]:
        # Already at 100% on the first reading of a week we joined late: it ran out, but we don't know when.
        out_unseen = bool(w.get("partial") and w.get("at_100") and w["at_100"] == w["first_seen"])
        lasted = _days(w["start"], w["at_100"]) if w.get("at_100") and not out_unseen else None
        elapsed = min(_days(w["start"], now.isoformat()), 7.0)
        live = not w.get("closed")
        pace = None
        if live and not w.get("at_100") and elapsed >= 0.5 and w["last_pct"] > 0 and not w.get("partial"):
            pace = round(elapsed * 100 / w["last_pct"], 1)            # days to 100% at this week's pace
        row = {"start": w["start"], "reset_at": w["reset_at"], "lasted_days": lasted, "peak": w["peak_pct"],
               "last": w["last_pct"], "live": live, "elapsed_days": elapsed, "projected_days": pace,
               "partial": w.get("partial", False), "ended_early": w.get("ended_early", False),
               "out_before_tracking": out_unseen}
        acct = out.setdefault(w["account_id"], {"account": w["account"], "provider": w.get("provider"), "windows": {}})
        acct["account"] = w["account"] or acct["account"]
        acct["windows"].setdefault(w["window"], []).append(row)
    for acct in out.values():
        for label, weeks in acct["windows"].items():
            weeks.sort(key=lambda x: x["start"], reverse=True)
            months = {}
            for x in weeks:
                if x["live"] or x["partial"]:
                    continue                                          # only whole, finished weeks count
                m = months.setdefault(x["start"][:7], {"weeks": 0, "ran_out": 0, "days": []})
                m["weeks"] += 1
                if x["lasted_days"] is not None:
                    m["ran_out"] += 1
                    m["days"].append(x["lasted_days"])
            acct.setdefault("months", {})[label] = [
                {"month": k, "weeks": v["weeks"], "ran_out": v["ran_out"],
                 "avg_lasted_days": round(sum(v["days"]) / len(v["days"]), 1) if v["days"] else None}
                for k, v in sorted(months.items(), reverse=True)]
    return {"accounts": out, "since": min((w["first_seen"] for w in d["weeks"]), default=None)}
