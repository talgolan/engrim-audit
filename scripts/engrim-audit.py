#!/usr/bin/env python3
"""engrim-audit.py — READ-ONLY auditor for an engrim project memory store.

Scans the records engrim already has (via the `engrim` CLI) and reports rot:
unresolved supersede references, stale state stacks, orphaned/mistagged
project tags, and (opt-in) near-duplicate or oversized active sets. It NEVER
mutates anything — it prints findings; the /engrim-audit skill interprets
them and proposes fixes (via `engrim supersede`) for the user to approve.

Usage:
  engrim-audit.py [--project TAG] [--all] [--global] [--json]
                   [--near-duplicates] [--max-active N]

  --project TAG      Project tag to audit. Default: resolved the same way
                      engrim itself does (--project > $ENGRIM_PROJECT >
                      git root of cwd > cwd).
  --all              Audit every project tag engrim has ever seen
                      (`engrim projects`), including __global__.
  --global           In single-project mode, also include the __global__
                      tag alongside the resolved project. Ignored with --all.
  --json             Emit machine-readable JSON instead of a human report.
  --near-duplicates  Also run the near-duplicate + oversized-project checks
                      (opt-in: heuristic, higher false-positive risk than
                      the other three).
  --max-active N     Active-record threshold for the oversize check
                      (default 40). Only used with --near-duplicates.

Checks (each is a finding category):
  unresolved_supersede  — record text says "Supersedes engrim N" (or "N and
                          M") but the referenced id is still status=active.
  stale_state_stack     — 2+ active type=state records in one project; only
                          the most recent should stay active.
  orphaned_project_tag  — a project tag that is a strict path-ancestor of
                          another tag engrim has recorded against, with far
                          fewer active records — usually a resolver bug
                          (project auto-detection landing one directory up),
                          not an intentional monorepo root. This check
                          always looks across every project tag engrim
                          knows about, even in single-project mode, because
                          detecting a mistag requires seeing the sibling it
                          was meant to land in.
  near_duplicate_active — (opt-in) two active same-type records in one
                          project whose summaries have high word overlap.
  oversize_project      — (opt-in) a project's active record count exceeds
                          --max-active.

Exit code: 0 always (a report, not a gate). Finding counts are in the output.

Requires: the `engrim` CLI on PATH. Tested against engrim 1.3.0 (`list
--include-stale --json -k N`; `projects` plain text) and 1.4.11 (`projects
--json`, tried first and falling back to plain-text parsing for older
installs). Deliberately avoids commands only documented for later versions
that were not present on 1.3.0 (`doctor`, `retire`, `merge`).
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict

LARGE_K = 100_000
SUPERSEDE_RE = re.compile(r"supersedes?\s+(?:engrim\s+)?(#?\d+(?:\s*(?:,|and)\s*#?\d+)*)", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d+")
PROJECTS_LINE_RE = re.compile(r"^\s*(\d+)\s*\((\d+)\s*active\)\s*last\s*(\S+)\s*(.+?)\s*$")
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "this", "that", "it", "as", "at",
    "by", "from", "into", "not", "no", "so", "if", "then", "than", "its",
}


def run_engrim(args):
    try:
        proc = subprocess.run(["engrim", *args], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        print("engrim-audit: the `engrim` CLI is not on PATH.", file=sys.stderr)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        print(f"engrim-audit: `engrim {' '.join(args)}` timed out.", file=sys.stderr)
        sys.exit(2)
    if proc.returncode != 0:
        print(f"engrim-audit: `engrim {' '.join(args)}` failed:\n{proc.stderr}", file=sys.stderr)
        sys.exit(2)
    return proc.stdout


def resolve_default_project():
    """Mirror engrim's own precedence: --project > $ENGRIM_PROJECT > git root of cwd > cwd."""
    import os

    env = os.environ.get("ENGRIM_PROJECT")
    if env:
        return env
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except FileNotFoundError:
        pass
    return os.getcwd()


def fetch_records(project_tag):
    raw = run_engrim(["list", "-p", project_tag, "--include-stale", "--json", "-k", str(LARGE_K)])
    try:
        return json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        print(f"engrim-audit: could not parse `engrim list` output for {project_tag!r}.", file=sys.stderr)
        return []


def fetch_projects_universe():
    """`engrim projects --json` (1.4.x+); falls back to the plain-text table
    parser for older installs (verified against 1.3.0, which rejects --json
    on this subcommand)."""
    proc = subprocess.run(["engrim", "projects", "--json"], capture_output=True, text=True, timeout=30)
    if proc.returncode == 0:
        try:
            rows = json.loads(proc.stdout)
            return [
                {"project": r["project"], "total": r["records"], "active": r["active"], "last_ts": r["last"]}
                for r in rows
            ]
        except (json.JSONDecodeError, KeyError):
            pass  # fall through to plain-text parsing below

    raw = run_engrim(["projects"])
    universe = []
    for line in raw.splitlines():
        m = PROJECTS_LINE_RE.match(line)
        if not m:
            continue
        total, active, last_ts, project = m.groups()
        universe.append({
            "project": project,
            "total": int(total),
            "active": int(active),
            "last_ts": last_ts,
        })
    return universe


def check_unresolved_supersede(records):
    by_id = {r["id"]: r for r in records}
    findings = []
    for r in records:
        if r["status"] != "active":
            continue
        text = f"{r['summary']} {r.get('detail') or ''}"
        for m in SUPERSEDE_RE.finditer(text):
            ref_ids = [int(n) for n in NUMBER_RE.findall(m.group(1))]
            for ref_id in ref_ids:
                target = by_id.get(ref_id)
                if target is None:
                    findings.append({
                        "check": "unresolved_supersede",
                        "record_id": r["id"],
                        "target_id": ref_id,
                        "detail": f"#{r['id']} claims to supersede #{ref_id}, which is not in this project's fetched set (may be elsewhere, or the claim is wrong).",
                    })
                elif target["status"] == "active":
                    findings.append({
                        "check": "unresolved_supersede",
                        "record_id": r["id"],
                        "target_id": ref_id,
                        "detail": f"#{r['id']} claims to supersede #{ref_id}, but #{ref_id} is still status=active.",
                    })
    return findings


def check_stale_state_stack(records):
    by_project_type = defaultdict(list)
    for r in records:
        if r["status"] == "active" and r["type"] == "state":
            by_project_type[r["project"]].append(r)
    findings = []
    for project, states in by_project_type.items():
        if len(states) < 2:
            continue
        states_sorted = sorted(states, key=lambda r: r["ts"], reverse=True)
        current = states_sorted[0]
        for stale in states_sorted[1:]:
            findings.append({
                "check": "stale_state_stack",
                "project": project,
                "record_id": stale["id"],
                "current_id": current["id"],
                "detail": f"#{stale['id']} ({stale['ts']}) is an older active state record; #{current['id']} ({current['ts']}) is more recent for {project}.",
            })
    return findings


def check_orphaned_project_tags(universe):
    path_tags = [p for p in universe if p["project"].startswith("/")]
    findings = []
    for candidate in path_tags:
        descendants = [
            p for p in path_tags
            if p["project"] != candidate["project"] and p["project"].startswith(candidate["project"] + "/")
        ]
        if not descendants:
            continue
        biggest = max(descendants, key=lambda p: p["active"])
        if candidate["active"] < biggest["active"]:
            findings.append({
                "check": "orphaned_project_tag",
                "project": candidate["project"],
                "sibling": biggest["project"],
                "detail": f"{candidate['project']!r} ({candidate['active']} active) is a path-ancestor of {biggest['project']!r} ({biggest['active']} active) — possible resolver mistag rather than an intentional monorepo root.",
            })
    return findings


def _wordset(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def check_near_duplicates(records):
    by_project_type = defaultdict(list)
    for r in records:
        if r["status"] == "active":
            by_project_type[(r["project"], r["type"])].append(r)
    findings = []
    for (project, rtype), group in by_project_type.items():
        for i, a in enumerate(group):
            wa = _wordset(a["summary"])
            if len(wa) < 8:
                continue
            for b in group[i + 1:]:
                wb = _wordset(b["summary"])
                if len(wb) < 8:
                    continue
                overlap = len(wa & wb) / len(wa | wb)
                if overlap >= 0.55:
                    findings.append({
                        "check": "near_duplicate_active",
                        "project": project,
                        "type": rtype,
                        "record_id": a["id"],
                        "other_id": b["id"],
                        "overlap": round(overlap, 2),
                        "detail": f"#{a['id']} and #{b['id']} ({rtype}, {project}) share {round(overlap * 100)}% of their summary words.",
                    })
    return findings


def check_oversize(universe, audited_tags, max_active):
    findings = []
    for p in universe:
        if p["project"] not in audited_tags:
            continue
        if p["active"] > max_active:
            findings.append({
                "check": "oversize_project",
                "project": p["project"],
                "active": p["active"],
                "threshold": max_active,
                "detail": f"{p['project']!r} has {p['active']} active records, over the {max_active} threshold.",
            })
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--global", dest="include_global", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--near-duplicates", action="store_true")
    ap.add_argument("--max-active", type=int, default=40)
    args = ap.parse_args()

    universe = fetch_projects_universe()

    if args.all:
        audited_tags = [p["project"] for p in universe] or [resolve_default_project()]
    else:
        tag = args.project or resolve_default_project()
        audited_tags = [tag]
        if args.include_global and "__global__" not in audited_tags:
            audited_tags.append("__global__")

    records = []
    for tag in audited_tags:
        records.extend(fetch_records(tag))

    findings = []
    findings += check_unresolved_supersede(records)
    findings += check_stale_state_stack(records)
    findings += check_orphaned_project_tags(universe)
    if args.near_duplicates:
        findings += check_near_duplicates(records)
        findings += check_oversize(universe, audited_tags, args.max_active)

    meta = {
        "audited_tags": audited_tags,
        "records_scanned": len(records),
        "total_findings": len(findings),
    }

    if args.json:
        print(json.dumps({"meta": meta, "findings": findings}, indent=2))
        return 0

    print(f"engrim-audit — {', '.join(audited_tags)}")
    print(f"  records scanned: {meta['records_scanned']}   findings: {meta['total_findings']}")
    print()

    by_check = defaultdict(list)
    for f in findings:
        by_check[f["check"]].append(f)

    labels = {
        "unresolved_supersede": "unresolved supersede references",
        "stale_state_stack": "stale state stack entries",
        "orphaned_project_tag": "orphaned/mistagged project tags",
        "near_duplicate_active": "near-duplicate active records",
        "oversize_project": "oversized projects",
    }
    checks_run = ["unresolved_supersede", "stale_state_stack", "orphaned_project_tag"]
    if args.near_duplicates:
        checks_run += ["near_duplicate_active", "oversize_project"]

    for check in checks_run:
        group = by_check.get(check, [])
        label = labels[check]
        if not group:
            print(f"  ✓ {label}: none")
            continue
        print(f"  ✗ {label} ({len(group)}):")
        for f in group:
            print(f"      - {f['detail']}")
    print()
    if meta["total_findings"] == 0:
        print("  Clean — no rot detected.")
    else:
        print(f"  {meta['total_findings']} finding(s). The /engrim-audit skill will propose approval-gated fixes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
