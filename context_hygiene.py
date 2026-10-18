#!/usr/bin/env python3
"""
context_hygiene.py — Universal context-hygiene checker for AI coding/chat agents.

Works with: Hermes Agent, Claude Code, OpenCode, Codex CLI, Aider, Cursor,
or any tool that stores sessions as files (JSONL transcripts) or SQLite.

What it does:
  1. Finds the active session(s) of your AI tool.
  2. Measures session AGE (days since it started) and LOAD (prompt tokens / file size).
  3. Compares against configurable thresholds.
  4. Writes a small status report that can be piped to a cron job, a
     notification channel (Telegram/Slack), or just printed.

Exit codes:
  0 = healthy (or nothing found)
  1 = stale/overloaded session found (action recommended)
  2 = could not inspect anything (config error)

This file has ZERO third-party dependencies — stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DEFAULTS = {
    # Age of a session before it's considered "stale" (hours).
    "max_age_hours": 48,
    # Approximate prompt-token budget before "overloaded". Token counts are
    # taken from tool-specific sources when available, else estimated from
    # transcript size (1 token ~= 4 chars).
    "max_prompt_tokens": 60000,
}

# ---------------------------------------------------------------------------
# Tool backends. Each returns a list of dicts:
#   {"tool": str, "session_id": str, "started_at": float, "last_active": float,
#    "tokens": int|None, "detail": str}
# Backends fail silently (return []) when the tool isn't installed — that's
# the point: one script, many machines.
# ---------------------------------------------------------------------------


def _estimate_tokens_from_chars(n_chars: int) -> int:
    return n_chars // 4


def check_hermes(home: Path | None = None) -> list[dict]:
    """Hermes Agent: state.db + gateway_routing (or per-profile state.db)."""
    results = []
    candidates = []
    if home:
        candidates.append(home / "state.db")
    else:
        default = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        candidates.append(default / "state.db")
        profiles = default / "profiles"
        if profiles.is_dir():
            for p in profiles.iterdir():
                if p.is_dir():
                    candidates.append(p / "state.db")

    for db_path in candidates:
        if not db_path.is_file():
            continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cur = con.cursor()
            # gateway_routing holds live routed sessions (messaging gateway)
            cur.execute("SELECT session_key, entry_json FROM gateway_routing")
            rows = cur.fetchall()
            for key, entry_json in rows:
                try:
                    entry = json.loads(entry_json)
                except Exception:
                    continue
                created = entry.get("created_at")
                updated = entry.get("updated_at")
                tokens = entry.get("last_prompt_tokens") or 0
                # parse ISO timestamps written by Hermes
                def _ts(v):
                    if not v:
                        return None
                    try:
                        from datetime import datetime
                        return datetime.fromisoformat(v).timestamp()
                    except Exception:
                        return None
                started = _ts(created) or 0
                last = _ts(updated) or started
                results.append({
                    "tool": "hermes",
                    "session_id": entry.get("session_id", "?"),
                    "started_at": started,
                    "last_active": last,
                    "tokens": tokens,
                    "detail": f"session_key={key} db={db_path}",
                })
            # Inspect plain CLI sessions that are still open. Skip any that
            # are already represented by a gateway_routing entry (same id)
            # so a session isn't double-reported.
            try:
                seen_ids = {r["session_id"] for r in results}
                cur.execute(
                    "SELECT id, started_at, ended_at, input_tokens FROM sessions "
                    "WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 5"
                )
                for sid, started, _ended, itok in cur.fetchall():
                    if sid in seen_ids:
                        continue
                    results.append({
                        "tool": "hermes-cli",
                        "session_id": sid,
                        "started_at": started or 0,
                        "last_active": time.time(),
                        "tokens": itok or 0,
                        "detail": "active CLI session",
                    })
            except sqlite3.OperationalError:
                pass
            con.close()
        except sqlite3.Error:
            continue
    return results


def check_claude_code() -> list[dict]:
    """Claude Code: ~/.claude/projects/**/*.jsonl transcripts."""
    results = []
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return results
    cutoff = time.time() - 7 * 86400  # last 7 days only
    for jl in base.rglob("*.jsonl"):
        try:
            st = jl.stat()
            if st.st_mtime < cutoff or st.st_size == 0:
                continue
            size_tokens = _estimate_tokens_from_chars(st.st_size)
            # first line has a timestamp field in most versions
            started = st.st_ctime
            with open(jl, "rb") as f:
                head = f.readline(4096)
            try:
                first = json.loads(head)
                ts = first.get("timestamp") or first.get("ts")
                if ts:
                    started = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                pass
            results.append({
                "tool": "claude-code",
                "session_id": jl.stem,
                "started_at": started,
                "last_active": st.st_mtime,
                "tokens": size_tokens,
                "detail": str(jl),
            })
        except OSError:
            continue
    return results


def check_codex() -> list[dict]:
    """Codex CLI: ~/.codex/sessions/*.jsonl (rollout files)."""
    results = []
    base = Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        return results
    cutoff = time.time() - 7 * 86400
    for jl in base.rglob("*.jsonl"):
        try:
            st = jl.stat()
            if st.st_mtime < cutoff or st.st_size == 0:
                continue
            results.append({
                "tool": "codex",
                "session_id": jl.stem,
                "started_at": st.st_ctime,
                "last_active": st.st_mtime,
                "tokens": _estimate_tokens_from_chars(st.st_size),
                "detail": str(jl),
            })
        except OSError:
            continue
    return results


def check_opencode() -> list[dict]:
    """OpenCode: ~/.local/share/opencode/storage/session (JSON files)."""
    results = []
    base = Path.home() / ".local" / "share" / "opencode" / "storage"
    if not base.is_dir():
        return results
    cutoff = time.time() - 7 * 86400
    for jf in base.rglob("*.json"):
        try:
            st = jf.stat()
            if st.st_mtime < cutoff:
                continue
            try:
                data = json.loads(jf.read_text(errors="ignore"))
            except Exception:
                data = {}
            results.append({
                "tool": "opencode",
                "session_id": data.get("id", jf.stem),
                "started_at": st.st_ctime,
                "last_active": st.st_mtime,
                "tokens": _estimate_tokens_from_chars(st.st_size),
                "detail": str(jf),
            })
        except OSError:
            continue
    return results


def check_generic_dir(path: str, label: str) -> list[dict]:
    """Fallback: any directory of session files."""
    results = []
    base = Path(path).expanduser()
    if not base.is_dir():
        return results
    cutoff = time.time() - 7 * 86400
    for f in base.iterdir():
        if not f.is_file() or f.stat().st_mtime < cutoff:
            continue
        st = f.stat()
        results.append({
            "tool": label,
            "session_id": f.stem,
            "started_at": st.st_ctime,
            "last_active": st.st_mtime,
            "tokens": _estimate_tokens_from_chars(st.st_size),
            "detail": str(f),
        })
    return results


BACKENDS = {
    "hermes": lambda: check_hermes(),
    "claude-code": check_claude_code,
    "codex": check_codex,
    "opencode": check_opencode,
}

# ---------------------------------------------------------------------------
# Evaluation + output
# ---------------------------------------------------------------------------


def evaluate(sessions: list[dict], max_age_hours: float, max_tokens: int) -> list[dict]:
    """Attach a verdict to each session."""
    now = time.time()
    out = []
    for s in sessions:
        age_h = max((now - s["started_at"]) / 3600.0, 0.0)
        idle_h = max((now - s["last_active"]) / 3600.0, 0.0)
        tokens = s.get("tokens") or 0
        stale = age_h > max_age_hours
        overloaded = tokens > max_tokens
        # Only sessions that were recently active matter; ancient dead
        # sessions are noise.
        if idle_h > 48:
            verdict = "idle"
        elif stale or overloaded:
            verdict = "warning"
        else:
            verdict = "healthy"
        out.append({**s, "age_hours": round(age_h, 1), "idle_hours": round(idle_h, 1),
                    "verdict": verdict,
                    "reasons": [r for r, hit in (("age", stale), ("tokens", overloaded)) if hit]})
    return out


def summarize(rows: list[dict]) -> tuple[str, int]:
    """Human summary + exit code."""
    warnings = [r for r in rows if r["verdict"] == "warning"]
    if not warnings:
        return "context-hygiene: all healthy ✓", 0
    lines = ["context-hygiene: ⚠ sessions need attention:"]
    for w in warnings:
        lines.append(
            f"  [{w['tool']}] {w['session_id'][:24]} "
            f"age={w['age_hours']}h tokens~{w['tokens']} "
            f"({', '.join(w['reasons'])})"
        )
    lines.append("  → start a fresh session (/new or new chat) and carry over")
    lines.append("    only a 3-sentence summary. Prevents hallucination drift.")
    return "\n".join(lines), 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Context-hygiene checker for AI agent sessions")
    ap.add_argument("--tools", default="auto",
                    help="comma list: hermes,claude-code,codex,opencode (default: auto-detect all)")
    ap.add_argument("--max-age-hours", type=float, default=DEFAULTS["max_age_hours"])
    ap.add_argument("--max-prompt-tokens", type=int, default=DEFAULTS["max_prompt_tokens"])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--extra-dir", action="append", default=[],
                    help="extra dir to scan (repeatable), label=path")
    args = ap.parse_args()

    sessions: list[dict] = []
    if args.tools == "auto":
        tools = list(BACKENDS)
    else:
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    for t in tools:
        fn = BACKENDS.get(t)
        if fn:
            try:
                sessions.extend(fn())
            except Exception:
                pass
    for spec in args.extra_dir:
        label, _, path = spec.partition("=")
        sessions.extend(check_generic_dir(path or label, label or "extra"))

    rows = evaluate(sessions, args.max_age_hours, args.max_prompt_tokens)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if any(r["verdict"] == "warning" for r in rows) else 0
    text, code = summarize(rows)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
