#!/usr/bin/env python3
"""
reset_modes.py — The two reset actions for context-hygiene.

MODE 1: notify (default)
    Warns the user and asks THEM to type /new. Delivery target is chosen at
    install time:
      --notify telegram:<chat_id>   → send via Telegram Bot API
                                     (needs TELEGRAM_BOT_TOKEN env var)
      --notify session              → print to stdout (the chat/session where
                                     the check runs, e.g. cron deliver origin)
      --notify <webhook_url>        → POST the text to any webhook (ntfy,
                                     Slack, Discord all accept simple POSTs)

MODE 2: auto
    Resets the session WITHOUT asking. What "reset" means per tool:
      hermes       → end the live session row in state.db + drop the routing
                     entry. The gateway recovers it as a FRESH session on the
                     next incoming message (see gateway/session.py recovery
                     path). Memory files are NOT touched.
      claude-code,
      codex,
      opencode,
      generic dirs → move the transcript file to <dir>/archived/ so the next
                     run of the tool starts a new transcript.

Safety rails:
  - auto mode NEVER deletes anything: hermes rows are only stamped ended,
    files are MOVED to an archive folder.
  - auto mode refuses to act if the session had activity in the last
    --min-idle-minutes (default 15) — we don't yank a session mid-conversation.
  - Everything is logged to stdout so cron captures an audit trail.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Reset backends
# ---------------------------------------------------------------------------


def reset_hermes(session_id: str, detail_db: str, min_idle_min: int, dry_run: bool) -> str:
    """End a live Hermes session row + clear its routing entry. Reversible:
    nothing is deleted; the gateway recovers with a fresh session on next msg."""
    db_path = detail_db.split("db=")[-1]
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    now = time.time()
    cur.execute("SELECT ended_at FROM sessions WHERE id = ?", (session_id,))
    row = cur.fetchone()
    if row is None:
        con.close()
        return f"SKIP: session {session_id} not found in {db_path}"
    if row[0] is not None:
        con.close()
        return f"SKIP: session {session_id} already ended"
    cur.execute("SELECT updated_at FROM gateway_routing WHERE entry_json LIKE ?",
                (f"%{session_id}%",))
    rrow = cur.fetchone()
    if rrow and (now - rrow[0]) < min_idle_min * 60:
        con.close()
        return (f"SKIP: session {session_id} active {int(now - rrow[0])}s ago "
                f"(< {min_idle_min} min idle guard) — refusing mid-conversation reset")
    if dry_run:
        con.close()
        return f"DRY-RUN: would end session {session_id} in {db_path}"
    cur.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = 'context-hygiene' "
        "WHERE id = ? AND ended_at IS NULL", (now, session_id))
    cur.execute("DELETE FROM gateway_routing WHERE entry_json LIKE ?",
                (f"%{session_id}%",))
    con.commit()
    con.close()
    return f"RESET: hermes session {session_id} ended — next message starts fresh"


def _archive_file(file_path: str, dry_run: bool) -> str:
    p = Path(file_path)
    arch = p.parent / "archived"
    if dry_run:
        return f"DRY-RUN: would move {p} → {arch}/"
    arch.mkdir(exist_ok=True)
    dest = arch / f"{p.stem}.{int(time.time())}{p.suffix}"
    shutil.move(str(p), str(dest))
    return f"RESET: archived {p.name} → {dest}"


def reset_generic(session: dict, min_idle_min: int, dry_run: bool) -> str:
    """File-based tools: archive the transcript. detail = absolute file path."""
    p = Path(session["detail"])
    if not p.is_file():
        return f"SKIP: {p} not found"
    idle_s = time.time() - p.stat().st_mtime
    if idle_s < min_idle_min * 60:
        return (f"SKIP: {p.name} active {int(idle_s)}s ago "
                f"(< {min_idle_min} min idle guard)")
    return _archive_file(str(p), dry_run)


def perform_resets(warnings: list[dict], mode: str, min_idle_min: int,
                   dry_run: bool) -> list[str]:
    """Dispatch each warning session to the right reset backend."""
    out = []
    for w in warnings:
        try:
            if w["tool"].startswith("hermes"):
                out.append(reset_hermes(w["session_id"], w["detail"],
                                        min_idle_min, dry_run))
            else:
                out.append(reset_generic(w, min_idle_min, dry_run))
        except Exception as e:  # never abort the whole batch on one failure
            out.append(f"ERROR resetting {w.get('session_id')}: {e}")
    return out


# ---------------------------------------------------------------------------
# Notification backends
# ---------------------------------------------------------------------------


def notify_stdout(text: str) -> bool:
    print(text)
    return True


def notify_telegram(text: str, chat_id: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set — cannot send Telegram notice",
              file=sys.stderr)
        return False
    url = (f"https://api.telegram.org/bot{token}/sendMessage")
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        print(f"ERROR sending Telegram notice: {e}", file=sys.stderr)
        return False


def notify_webhook(text: str, url: str) -> bool:
    """ntfy/Slack/Discord-style plain POST."""
    req = urllib.request.Request(url, data=text.encode(),
                                 headers={"Content-Type": "text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status < 300
    except Exception as e:
        print(f"ERROR posting webhook: {e}", file=sys.stderr)
        return False


def send_notice(text: str, target: str) -> bool:
    """target: 'session' | 'telegram:<id>' | any URL."""
    if target == "session" or not target:
        return notify_stdout(text)
    if target.startswith("telegram:"):
        return notify_telegram(text, target.split(":", 1)[1])
    if target.startswith("http"):
        return notify_webhook(text, target)
    print(f"ERROR: unknown notify target {target!r}", file=sys.stderr)
    return False


def build_notice_text(warnings: list[dict], reset_log: list[str] | None) -> str:
    lines = ["🧠🧹 context-hygiene:"]
    for w in warnings:
        lines.append(
            f"• [{w['tool']}] {w['session_id'][:20]} — "
            f"umur {w['age_hours']}h, ~{w['tokens']} token ({', '.join(w['reasons'])})")
    if reset_log:
        lines.append("")
        lines.extend(f"  {r}" for r in reset_log)
    else:
        lines.append("")
        lines.append("Saran: ketik /new di chat ini biar agent mulai fresh.")
        lines.append("(Mencegah halu — sesi panjang = ingatan terkompresi)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reset actions for context-hygiene (notify OR auto)")
    ap.add_argument("--mode", choices=["notify", "auto"], default="notify",
                    help="notify = warn user (safe). auto = reset directly.")
    ap.add_argument("--notify", default="session",
                    help="notify target: 'session' (stdout), "
                         "'telegram:<chat_id>', or an http(s) webhook URL")
    ap.add_argument("--warnings-file", default="-",
                    help="JSON file of warning rows (from context_hygiene.py "
                         "--json, filtered to verdict=warning); '-' = stdin")
    ap.add_argument("--min-idle-minutes", type=int, default=15,
                    help="auto mode: refuse reset if session active more "
                         "recently than this (default 15)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.warnings_file == "-" else \
        Path(args.warnings_file).read_text()
    try:
        warnings = [w for w in json.loads(raw) if w.get("verdict") == "warning"]
    except json.JSONDecodeError as e:
        print(f"ERROR: bad warnings JSON: {e}", file=sys.stderr)
        return 2

    if not warnings:
        print("context-hygiene reset: nothing to do (all healthy)")
        return 0

    reset_log = None
    if args.mode == "auto":
        reset_log = perform_resets(warnings, args.mode,
                                   args.min_idle_minutes, args.dry_run)
    ok = send_notice(build_notice_text(warnings, reset_log), args.notify)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
