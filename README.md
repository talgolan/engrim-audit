# engrim-audit

A Claude Code plugin that audits an [engrim](https://github.com/timgordontg/engrim)
project memory store — a local SQLite database of decisions, facts, feedback,
and state that persists across sessions — for rot, and proposes fixes you
approve before anything is written.

One slash command: **`/engrim-audit`**.

---

## Why this exists

engrim already ships `engrim doctor` for infrastructure health (SQLite
integrity, WAL mode, hook paths). It has nothing for memory *content* health.
Content drifts: an agent writes "Supersedes engrim 329" in a summary and
never actually runs `engrim supersede`, so both records stay active forever.
A project auto-resolves to the wrong directory once (a known quirk under some
harnesses) and three records become permanently invisible to normal recall.
Six state snapshots pile up because each new one never retires the last.
None of it is caught by an infrastructure check, because the database is
perfectly healthy — the *claims inside it* are what's wrong.

This plugin was built directly from real findings: auditing one project's
engrim store by hand surfaced a live unresolved supersede chain, three
records misfiled under a path one directory above the intended project, and
a state-record stack six deep — all confirmed against the actual repository
before anything was changed. The script below reproduces (and, on the
supersede check, exceeds) that manual audit automatically.

## What it checks

| Category | Meaning | Default |
|---|---|---|
| `unresolved_supersede` | A record's summary/detail says "Supersedes engrim N" but record N is still `status=active` | on |
| `stale_state_stack` | 2+ active `type=state` records in one project | on |
| `orphaned_project_tag` | A project tag that's a strict path-ancestor of another tag with far fewer active records — usually a resolver mistag | on |
| `near_duplicate_active` | Two active same-type records in one project with ≥55% summary word overlap | opt-in |
| `oversize_project` | A project's active-record count exceeds a threshold (default 40) | opt-in |

## Design — read-only detection, approval-gated fixes

- **`scripts/engrim-audit.py` is READ-ONLY.** It only calls `engrim list`
  and `engrim projects`. It never calls `engrim add` or `engrim supersede`,
  and never touches the SQLite file directly. Run it as often as you like.
- **The skill proposes; you approve.** Claude interprets the findings,
  proposes concrete fixes, and applies only what you OK via `engrim
  supersede` — the only mutation this plugin ever performs. engrim has no
  delete or move command, so a "re-tag" fix is really: `engrim add` a
  corrected record under the right project, then supersede the original.

## Usage

After installing (below), in any project with an engrim store:

```
/engrim-audit
```

Or run the script directly:

```sh
python3 scripts/engrim-audit.py                          # current project, human report
python3 scripts/engrim-audit.py --project /path/to/repo   # explicit project tag
python3 scripts/engrim-audit.py --all                     # every project tag engrim knows about
python3 scripts/engrim-audit.py --global                  # current project + __global__
python3 scripts/engrim-audit.py --json                    # machine output
python3 scripts/engrim-audit.py --near-duplicates          # also run the opt-in heuristic checks
```

With no `--project`, it resolves the tag the same way `engrim` itself does:
`--project` > `$ENGRIM_PROJECT` > git root of the current directory > cwd.

Requirements: Python 3 (engrim itself requires 3.10+), the `engrim` CLI on
`PATH`. No other dependencies, no build step.

### Version compatibility

Built and tested against engrim 1.3.0 and 1.4.11. `engrim projects --json`
(added by 1.4.x) is tried first; the script falls back to parsing 1.3.0's
plain-text table if that flag isn't recognized. Deliberately avoids
commands only documented for later versions that weren't present on 1.3.0
(`doctor`, `retire`, `merge`).

## Install (via marketplace)

```
/plugin marketplace add talgolan/claude-plugins
/plugin install engrim-audit@talgolan
```

Update later:

```
/plugin marketplace update talgolan
```

## License

MIT — see [LICENSE](LICENSE).
