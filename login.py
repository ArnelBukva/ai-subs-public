#!/usr/bin/env python3
"""Sign in one more Claude subscription, so it shows on the dashboard.

    python3 login.py anthropic --label team

Since 20 Feb 2026 Anthropic only accepts subscription sign-in from Claude Code
and claude.ai; a sign-in link built by anything else is refused at the authorize
page with "Invalid request format", even when it is byte-for-byte what Claude
Code builds. So this does the one allowed thing: it runs Claude Code's own login
into a separate config folder, ~/.claude-<label>. Claude Code keeps that token in
the keychain and store.py finds every such folder. We never refresh or copy the
token; Claude Code owns it.

A browser opens to the sign-in page. If it opens the wrong profile, copy the
printed link into the browser signed in to the account you want to add.
"""

import argparse
import os
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", choices=["anthropic"])
    ap.add_argument("--label", required=True,
                    help="short name for the account, e.g. team or personal; becomes ~/.claude-<label>")
    args = ap.parse_args()
    label = args.label.lower().replace(" ", "-")
    config_dir = os.path.expanduser(f"~/.claude-{label}")
    os.makedirs(config_dir, exist_ok=True)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=config_dir)
    r = subprocess.run(["claude", "auth", "login", "--claudeai"], env=env)
    if r.returncode != 0:
        raise SystemExit(r.returncode)
    subprocess.run(["claude", "auth", "status", "--text"], env=env)
    print(f"\nAdded. It appears on the dashboard within a minute as 'Claude · {label}'.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was saved.")
        raise SystemExit(130)
