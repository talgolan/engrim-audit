---
name: engrim-audit
description: Audit an engrim project memory store for rot — unresolved supersede references, stale state stacks, orphaned/mistagged project tags, and (opt-in) near-duplicate or oversized active sets — and propose approval-gated fixes. One slash command: /engrim-audit.
---

# engrim-audit

## When to invoke

| User says | Action |
|---|---|
| "/engrim-audit", "audit my engrim memory", "check engrim for rot" | Run the audit + report |
| "is my engrim memory clean/organized", "clean up engrim" | Run the audit, then propose fixes |
| After a session that wrote several `engrim add`/`engrim supersede` calls | Offer to run it |

Do not confuse this with `engrim doctor` (if the installed CLI has it) —
`doctor` checks infrastructure (SQLite integrity, WAL mode, hook paths).
This skill checks memory *content*: are the records themselves internally
consistent. The two do not overlap.

## The split (why this is safe)

- **The script is READ-ONLY.** `scripts/engrim-audit.py` only calls `engrim
  list`, `engrim projects` — commands that read. It never calls `engrim add`
  or `engrim supersede` itself, and never touches the SQLite file directly.
- **You (Claude) interpret + propose.** Turn findings into concrete fixes
  and present them for approval. NEVER run `engrim supersede` (the only
  mutation this skill should ever perform) without the user's explicit OK.
  Mutating memory the user can't see is the exact failure mode this tool
  exists to prevent.
- **There is no "move" or "edit" command in engrim.** If a finding calls for
  moving a record to a different project tag, the only real mechanism is:
  `engrim add` a corrected record under the right project tag (mention the
  original id in its detail for provenance), then `engrim supersede --id
  <original> --status superseded` on the original. Never describe this as
  "re-tagging in place" — no such operation exists.

## How to run it

```sh
python3 scripts/engrim-audit.py                              # current project, human report
python3 scripts/engrim-audit.py --project /path/to/repo       # explicit project tag
python3 scripts/engrim-audit.py --all                         # every project engrim knows about
python3 scripts/engrim-audit.py --global                      # current project + __global__
python3 scripts/engrim-audit.py --json                        # machine output
python3 scripts/engrim-audit.py --near-duplicates              # also run the opt-in heuristic checks
python3 scripts/engrim-audit.py --near-duplicates --max-active 60
```

With no `--project`, it resolves the project tag the same way `engrim`
itself does: `--project` flag > `$ENGRIM_PROJECT` > git root of the current
directory > current directory.

Requirements: Python 3 (engrim itself requires 3.10+, so this adds no new
dependency), the `engrim` CLI on `PATH`. No other packages, no build step.

## What it checks

| Category | Meaning | Default |
|---|---|---|
| `unresolved_supersede` | A record's summary/detail says "Supersedes engrim N" but record N is still `status=active` | on |
| `stale_state_stack` | 2+ active `type=state` records in one project — a state record is a point-in-time snapshot, so more than one active means an older one was never superseded | on |
| `orphaned_project_tag` | A project tag that is a strict path-ancestor of another tag engrim has recorded against, with far fewer active records — usually the project auto-detection landing one directory up (a known engrim resolver quirk, e.g. under Cursor), not an intentional monorepo root | on |
| `near_duplicate_active` | Two active same-type records in one project whose summaries share ≥55% of their (stopword-filtered) words | opt-in (`--near-duplicates`) |
| `oversize_project` | A project's active-record count exceeds `--max-active` (default 40) | opt-in (`--near-duplicates`) |

The two opt-in checks are heuristic and carry more false-positive risk than
the other three — they flag candidates for a human to look at, never
anything to auto-merge.

The `orphaned_project_tag` check always considers every project tag engrim
knows about, even when auditing a single project — detecting a mistag
requires seeing the sibling the records were meant to land in.

## How to act on the report

1. Run the script; read the findings.
2. For **`unresolved_supersede`**: check the target record's content. If the
   citing record's claim is correct (the target really is stale/replaced),
   propose `engrim supersede --id <target> --status superseded`. If the
   claim itself was wrong, say so — don't blindly apply it.
3. For **`stale_state_stack`**: propose superseding every record except the
   most recent, unless the user tells you an older one is still relevant for
   a reason the timestamps don't capture (e.g. two independently-tracked
   subsystems both using `type=state` in one project).
4. For **`orphaned_project_tag`**: present both options and let the user
   choose — don't pick silently:
   - Supersede the misfiled records in place (simplest; they stay invisible
     to normal recall for that project, but stop asserting stale/duplicate
     claims).
   - Recreate them under the correct project tag via `engrim add` (referencing
     the original id), then supersede the originals — use this when the
     content is still worth having reachable, not just inert.
5. For **`near_duplicate_active`**: read both records. If they really say
   the same thing, propose superseding the older/less-detailed one, or offer
   to write one merged record and supersede both — ask which the user
   prefers rather than assuming.
6. For **`oversize_project`**: not a fix on its own — flag it as a prompt to
   look for `stale_state_stack` and `near_duplicate_active` findings in that
   same project; the fix is elsewhere.
7. Apply ONLY what the user approves, via `engrim supersede` and (when
   recreating under a different project) `engrim add`. Never call any
   command that erases a row — engrim has none; supersede is the only
   "remove" primitive, and it preserves history by design.
8. Re-run the script after applying fixes to confirm a clean report.

## Boundaries

- Read-only detection by default. No `engrim supersede` or `engrim add` call
  without explicit approval for that specific record.
- Don't fabricate the "correct" project tag when proposing a recreate-fix —
  use the sibling tag the check surfaced, and say if you're guessing.
- Verify any code/file claim inside a record's content against the real
  source before asserting it's still accurate — memory is a snapshot, not
  ground truth.
