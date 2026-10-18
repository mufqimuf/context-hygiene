---
name: context-hygiene
description: "Use when sessions run for days, the agent feels dumber, forgets things mid-session, or hallucinates after long conversations. Checks session age and context load against thresholds, reports which sessions need a fresh reset, and explains the fix (start new session, carry a 3-sentence summary)."
version: 1.0.0
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sessions, memory, context, debugging, maintenance]
---

# Context Hygiene

Long-lived AI sessions degrade: older context gets compressed, summaries drift,
and the model starts hallucinating. The fix is cheap and boring — **start fresh
sessions regularly and keep permanent memory small and curated.** This skill
makes that routine instead of reactive.

## The mental model

- **Session** = short-term working memory. Healthy when young and light.
- **Memory files (MEMORY.md / USER.md)** = long-term notes. Curated facts only.
- **Skills** = procedures that load when relevant.
- If you find yourself "remembering" something from a 5-day-old conversation —
  you're probably reconstructing it from a compressed summary. Verify or ask.

## Quick check

Run the bundled script (no dependencies):

```bash
python3 scripts/context_hygiene.py                 # human summary
python3 scripts/context_hygiene.py --json          # machine-readable
python3 context_hygiene.py --max-age-hours 24 --max-prompt-tokens 40000
```

Exit code 1 = at least one active session is stale or overloaded.

## Thresholds (defaults, adjust per user)

| Signal | Default | Why |
|---|---|---|
| Session age | > 48 h | Compression chains get long; drift compounds |
| Prompt tokens | > 60 k | Context pressure → compression → lossy recall |
| Idle time | > 48 h | Old sessions that nobody uses are noise, not warnings |

## When a warning fires

1. Tell the user plainly: "session ini sudah X jam / ~Y ribu token; sarannya
   ketik `/new` biar aku mulai fresh."
2. Offer to save anything important first: "ada yang mau diingetin sebelum
   reset?" — then write it to memory (curated, one line).
3. NEVER auto-reset a live session yourself. The user types `/new` (or `/reset`).

## Standing rules for the agent

- At the start of a NEW session, don't fabricate continuity. If the user
  references past work, use session search — don't guess.
- Keep MEMORY.md under capacity: consolidate stale entries when adding new ones.
- Repeat setups belong in skills, not memory; raw discussion detail belongs
  nowhere — memory holds facts, not transcripts.
- When the user reports "you feel dumber", run the checker and show numbers.
  Diagnose with data, not vibes.

## Automation (cron)

Daily reminder pattern (Hermes cron example):

```
hermes cron create "0 9 * * *" "<prompt that runs the checker and, on exit 1,
notifies the user to type /new>" --name "context-hygiene" --deliver telegram:<chat_id>
```

For non-Hermes tools, any scheduler works (cron, Task Scheduler, n8n) — the
script only needs Python 3.9+.
