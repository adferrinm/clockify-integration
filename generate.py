#!/usr/bin/env python3
"""
generate.py — GitHub activity -> YAML intermediate file
========================================================
Fetches activity from GitHub for the given month and produces a
human-reviewable YAML file (clockify_YYYY-MM.yaml) that push.py
later sends to Clockify.

Activity sources (configurable via 'activity_sources' in config.yaml):
  prs_opened     - PRs opened/authored by you (default)
  commits        - commits authored by you
  issues_created - issues you created
  prs_reviewed   - PRs you reviewed or approved

Only days after the last sync date (stored in clockify_sync.db) are
included in the output, so re-running mid-month is safe.

Usage:
    python generate.py
    python generate.py --month 2026-04
    python generate.py --month 2026-04 --config /path/to/config.yaml
    python generate.py --month 2026-04 --verbose
    python generate.py --month 2026-04 --all   # ignore last sync date
"""

from __future__ import annotations

import argparse
import calendar
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import requests
    import yaml
except ImportError as exc:
    sys.exit(
        f"ERROR: Missing dependency — {exc}. "
        "Run: pip install -r requirements.txt"
    )

import db


# -- Logging ------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%H:%M:%S",
    )


log = logging.getLogger(__name__)


# -- Data structures ----------------------------------------------------------

@dataclass
class ActivityItem:
    source: str   # "pr_opened" | "commit" | "issue_created" | "pr_reviewed"
    ref: str      # "#43", "abc1234", "#12"
    title: str
    repo: str
    project: str
    date: date


@dataclass
class YAMLEntry:
    project: str
    hours: Optional[float]
    description: str


@dataclass
class DayOutput:
    date: date
    entries: list[YAMLEntry] = field(default_factory=list)


# -- Config -------------------------------------------------------------------

VALID_SOURCES = {"prs_opened", "commits", "issues_created", "prs_reviewed"}


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"ERROR: Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    required = (
        "github_user", "github_token",
        "clockify_api_key", "clockify_workspace_id",
        "repos",
    )
    for key in required:
        if not cfg.get(key):
            sys.exit(f"ERROR: Missing required config field: '{key}'")

    if not cfg["repos"]:
        sys.exit("ERROR: 'repos' must contain at least one entry.")

    for repo_cfg in cfg["repos"]:
        if not repo_cfg.get("github_repo") or not repo_cfg.get("clockify_project"):
            sys.exit(
                "ERROR: Each repo entry needs "
                "'github_repo' and 'clockify_project'."
            )

    cfg.setdefault("default_hours", 8.0)
    cfg.setdefault("split_strategy", "equal")
    cfg.setdefault("pr_date_field", "created_at")
    cfg.setdefault("max_pages", 10)
    cfg.setdefault("activity_sources", ["prs_opened"])

    unknown = set(cfg["activity_sources"]) - VALID_SOURCES
    if unknown:
        sys.exit(
            f"ERROR: Unknown activity_sources: {sorted(unknown)}. "
            f"Valid: {sorted(VALID_SOURCES)}"
        )

    return cfg


# -- GitHub API ---------------------------------------------------------------

GITHUB_BASE = "https://api.github.com"


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _check_rate_limit(resp: requests.Response) -> None:
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining and int(remaining) < 20:
        log.warning(f"GitHub rate limit low: {remaining} requests remaining.")


def check_github_response(resp: requests.Response, context: str = "") -> None:
    if resp.ok:
        return
    code = resp.status_code
    loc = f" [{context}]" if context else ""
    if code == 401:
        sys.exit(
            f"ERROR: GitHub token invalid or expired (401){loc}. "
            "Check 'github_token' in config."
        )
    if code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining", "?")
        reset_ts = resp.headers.get("X-RateLimit-Reset", "?")
        if remaining == "0":
            sys.exit(
                f"ERROR: GitHub rate limit exceeded (403){loc}. "
                f"Resets at unix timestamp {reset_ts}."
            )
        sys.exit(
            f"ERROR: GitHub returned 403 Forbidden{loc}: {resp.url}. "
            "Check that your token has 'repo' scope."
        )
    if code == 404:
        sys.exit(
            f"ERROR: GitHub repo not found (404){loc}: {resp.url}. "
            "Check 'github_repo' in config."
        )
    sys.exit(
        f"ERROR: GitHub API returned {code}{loc} for {resp.url}.\n"
        f"{resp.text[:300]}"
    )


def _month_iso_range(month_start: date, month_end: date) -> tuple[str, str]:
    return (
        f"{month_start.isoformat()}T00:00:00Z",
        f"{month_end.isoformat()}T23:59:59Z",
    )


# ── PRs opened ───────────────────────────────────────────────────────────────

def fetch_prs_for_repo(
    owner: str, repo: str, user: str, token: str,
    month_start: date, month_end: date, date_field: str, max_pages: int,
) -> list[dict]:
    headers = _github_headers(token)
    full_repo = f"{owner}/{repo}"

    extra = (
        {"sort": "created", "direction": "desc"}
        if date_field == "created_at"
        else {"sort": "updated", "direction": "desc"}
    )

    collected: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"state": "all", "per_page": 100, "page": page, **extra}
        log.debug(f"  Fetching PRs {full_repo} page {page} ...")
        resp = requests.get(
            f"{GITHUB_BASE}/repos/{full_repo}/pulls",
            headers=headers, params=params, timeout=30,
        )
        check_github_response(resp, f"PRs {full_repo}")
        _check_rate_limit(resp)

        prs: list[dict] = resp.json()
        if not prs:
            break

        for pr in prs:
            if pr.get("user", {}).get("login") != user:
                continue
            raw_date = pr.get(date_field)
            if raw_date is None:
                continue
            pr_date = datetime.fromisoformat(
                raw_date.replace("Z", "+00:00")
            ).date()
            if month_start <= pr_date <= month_end:
                collected.append(pr)

        if date_field == "created_at":
            oldest = min(
                datetime.fromisoformat(
                    p["created_at"].replace("Z", "+00:00")
                ).date()
                for p in prs
            )
            if oldest < month_start:
                log.debug("  Early stop: oldest PR is before month start.")
                break

        if len(prs) < 100:
            break

    log.info(f"  {full_repo}: {len(collected)} PR(s) opened/authored.")
    return collected


def _prs_to_activity(
    raw_prs: list[dict], full_repo: str, project: str, date_field: str,
) -> list[ActivityItem]:
    items = []
    for pr in raw_prs:
        raw_date = pr.get(date_field)
        if raw_date is None:
            continue
        pr_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
        items.append(ActivityItem(
            source="pr_opened",
            ref=f"#{pr['number']}",
            title=pr["title"].strip(),
            repo=full_repo,
            project=project,
            date=pr_date,
        ))
    return items


# ── Commits ──────────────────────────────────────────────────────────────────

def fetch_commits_for_repo(
    owner: str, repo: str, user: str, token: str,
    month_start: date, month_end: date, max_pages: int,
) -> list[ActivityItem]:
    headers = _github_headers(token)
    full_repo = f"{owner}/{repo}"
    since, until = _month_iso_range(month_start, month_end)

    items: list[ActivityItem] = []
    for page in range(1, max_pages + 1):
        params = {
            "author": user, "since": since, "until": until,
            "per_page": 100, "page": page,
        }
        log.debug(f"  Fetching commits {full_repo} page {page} ...")
        resp = requests.get(
            f"{GITHUB_BASE}/repos/{full_repo}/commits",
            headers=headers, params=params, timeout=30,
        )
        check_github_response(resp, f"commits {full_repo}")
        _check_rate_limit(resp)

        commits: list[dict] = resp.json()
        if not commits:
            break

        for c in commits:
            author_date = (
                c.get("commit", {}).get("author", {}).get("date")
                or c.get("commit", {}).get("committer", {}).get("date")
            )
            if not author_date:
                continue
            c_date = datetime.fromisoformat(
                author_date.replace("Z", "+00:00")
            ).date()
            if not (month_start <= c_date <= month_end):
                continue
            message = (
                c.get("commit", {}).get("message", "").split("\n")[0].strip()
            )
            sha = (c.get("sha") or "")[:7]
            items.append(ActivityItem(
                source="commit",
                ref=sha,
                title=message,
                repo=full_repo,
                project="",  # set by caller
                date=c_date,
            ))

        if len(commits) < 100:
            break

    log.info(f"  {full_repo}: {len(items)} commit(s) authored.")
    return items


# ── Issues created ───────────────────────────────────────────────────────────

def fetch_issues_for_repo(
    owner: str, repo: str, user: str, token: str,
    month_start: date, month_end: date, max_pages: int,
) -> list[ActivityItem]:
    headers = _github_headers(token)
    full_repo = f"{owner}/{repo}"
    since, _ = _month_iso_range(month_start, month_end)

    items: list[ActivityItem] = []
    for page in range(1, max_pages + 1):
        params = {
            "creator": user, "state": "all",
            "since": since, "per_page": 100, "page": page,
        }
        log.debug(f"  Fetching issues {full_repo} page {page} ...")
        resp = requests.get(
            f"{GITHUB_BASE}/repos/{full_repo}/issues",
            headers=headers, params=params, timeout=30,
        )
        check_github_response(resp, f"issues {full_repo}")
        _check_rate_limit(resp)

        issues: list[dict] = resp.json()
        if not issues:
            break

        for iss in issues:
            if "pull_request" in iss:
                continue  # GitHub returns PRs via issues endpoint; skip them
            raw_date = iss.get("created_at")
            if not raw_date:
                continue
            i_date = datetime.fromisoformat(
                raw_date.replace("Z", "+00:00")
            ).date()
            if not (month_start <= i_date <= month_end):
                continue
            items.append(ActivityItem(
                source="issue_created",
                ref=f"#{iss['number']}",
                title=iss["title"].strip(),
                repo=full_repo,
                project="",  # set by caller
                date=i_date,
            ))

        if len(issues) < 100:
            break

    log.info(f"  {full_repo}: {len(items)} issue(s) created.")
    return items


# ── PRs reviewed ─────────────────────────────────────────────────────────────

def fetch_reviewed_prs_for_repo(
    owner: str, repo: str, user: str, token: str,
    month_start: date, month_end: date, max_pages: int,
) -> list[ActivityItem]:
    """
    Two-step: search for PRs reviewed-by the user, then fetch each PR's
    reviews to get the exact review date within the month range.
    """
    headers = _github_headers(token)
    full_repo = f"{owner}/{repo}"

    # Step 1: search API to find candidate PRs reviewed by this user
    query = f"is:pr reviewed-by:{user} repo:{full_repo}"
    candidates: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {
            "q": query, "sort": "updated", "order": "desc",
            "per_page": 100, "page": page,
        }
        log.debug(f"  Searching reviewed PRs {full_repo} page {page} ...")
        resp = requests.get(
            f"{GITHUB_BASE}/search/issues",
            headers=headers, params=params, timeout=30,
        )
        check_github_response(resp, f"search reviewed PRs {full_repo}")
        _check_rate_limit(resp)

        page_items = resp.json().get("items", [])
        candidates.extend(page_items)
        if len(page_items) < 100:
            break

    log.debug(f"  {full_repo}: {len(candidates)} candidate reviewed PR(s).")

    # Step 2: fetch reviews per PR and find ones in the month range
    items: list[ActivityItem] = []
    for pr in candidates:
        pr_number = pr["number"]
        pr_title = pr["title"].strip()

        resp = requests.get(
            f"{GITHUB_BASE}/repos/{full_repo}/pulls/{pr_number}/reviews",
            headers=headers, timeout=30,
        )
        if not resp.ok:
            log.debug(
                f"  Skipping PR #{pr_number} reviews (status {resp.status_code})"
            )
            continue
        _check_rate_limit(resp)

        for review in resp.json():
            if review.get("user", {}).get("login") != user:
                continue
            raw_date = review.get("submitted_at")
            if not raw_date:
                continue
            r_date = datetime.fromisoformat(
                raw_date.replace("Z", "+00:00")
            ).date()
            if month_start <= r_date <= month_end:
                items.append(ActivityItem(
                    source="pr_reviewed",
                    ref=f"#{pr_number}",
                    title=pr_title,
                    repo=full_repo,
                    project="",  # set by caller
                    date=r_date,
                ))
                break  # one entry per PR is enough

    log.info(f"  {full_repo}: {len(items)} PR(s) reviewed.")
    return items


# -- Grouping & distribution --------------------------------------------------

def build_description(items: list[ActivityItem]) -> str:
    parts: list[str] = []

    prs_opened = sorted(
        [i for i in items if i.source == "pr_opened"], key=lambda x: x.ref
    )
    for i in prs_opened:
        parts.append(f"PR {i.ref} {i.title}")

    commits = [i for i in items if i.source == "commit"]
    if commits:
        if len(commits) == 1:
            parts.append(f"1 commit: {commits[0].title}")
        else:
            titles = ", ".join(c.title for c in commits[:3])
            extra = f" (+{len(commits) - 3} more)" if len(commits) > 3 else ""
            parts.append(f"{len(commits)} commits: {titles}{extra}")

    issues = sorted(
        [i for i in items if i.source == "issue_created"], key=lambda x: x.ref
    )
    for i in issues:
        parts.append(f"Issue {i.ref} {i.title}")

    reviewed = sorted(
        [i for i in items if i.source == "pr_reviewed"], key=lambda x: x.ref
    )
    for i in reviewed:
        parts.append(f"Reviewed PR {i.ref} {i.title}")

    return " | ".join(parts)


def group_by_date(
    all_items: list[ActivityItem],
) -> dict[date, dict[str, list[ActivityItem]]]:
    grouped: dict[date, dict[str, list[ActivityItem]]] = {}
    for item in all_items:
        grouped.setdefault(item.date, {}).setdefault(item.project, []).append(item)
    return grouped


def make_day_entries(
    items_by_project: dict[str, list[ActivityItem]],
    all_project_names: list[str],
    default_hours: float,
    split_strategy: str,
) -> list[YAMLEntry]:
    active = {proj: items for proj, items in items_by_project.items() if items}
    n = len(active)

    if n == 0:
        return []

    if n == 1:
        proj, items = next(iter(active.items()))
        return [YAMLEntry(
            project=proj,
            hours=float(default_hours),
            description=build_description(items),
        )]

    hours_each: Optional[float] = (
        round(default_hours / n, 2) if split_strategy == "equal" else None
    )
    return [
        YAMLEntry(
            project=proj,
            hours=hours_each,
            description=build_description(active[proj]),
        )
        for proj in all_project_names
        if proj in active
    ]


# -- YAML output --------------------------------------------------------------

def build_yaml_document(month_str: str, day_outputs: list[DayOutput]) -> dict:
    entries = []
    for day in sorted(day_outputs, key=lambda d: d.date):
        day_entries = [
            {
                "project": e.project,
                "hours": e.hours,
                "description": e.description,
            }
            for e in day.entries
        ]
        entries.append({"date": str(day.date), "entries": day_entries})

    return {
        "month": month_str,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "entries": entries,
    }


# -- CLI & main ---------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    today = date.today()
    parser = argparse.ArgumentParser(
        prog="generate",
        description="Generate a Clockify-ready YAML from GitHub activity.",
    )
    parser.add_argument(
        "--month", metavar="YYYY-MM",
        default=today.strftime("%Y-%m"),
        help="Month to process (default: current month)",
    )
    parser.add_argument(
        "--config", metavar="FILE", type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Include all days, ignoring last sync date",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)

    try:
        year, month = (int(x) for x in args.month.split("-"))
        _, days_in_month = calendar.monthrange(year, month)
        month_start = date(year, month, 1)
        month_end = date(year, month, days_in_month)
    except (ValueError, TypeError):
        sys.exit(f"ERROR: --month must be YYYY-MM, got '{args.month}'.")

    month_str = f"{year}-{month:02d}"
    log.info(f"=== generate.py | month={month_str} ===")

    cfg = load_config(args.config)
    github_user  = cfg["github_user"]
    github_token = cfg["github_token"]
    date_field   = cfg["pr_date_field"]
    default_hours = float(cfg["default_hours"])
    split_strat  = cfg["split_strategy"]
    max_pages    = int(cfg["max_pages"])
    repos_cfg: list[dict] = cfg["repos"]
    sources: list[str]    = cfg["activity_sources"]

    log.info(f"Activity sources: {sources}")

    all_project_names = [r["clockify_project"] for r in repos_cfg]
    all_items: list[ActivityItem] = []

    for repo_entry in repos_cfg:
        github_repo  = repo_entry["github_repo"]
        project_name = repo_entry["clockify_project"]
        owner, repo  = github_repo.split("/", 1)

        log.info(f"--- {github_repo} -> '{project_name}' ---")

        if "prs_opened" in sources:
            raw_prs = fetch_prs_for_repo(
                owner=owner, repo=repo, user=github_user, token=github_token,
                month_start=month_start, month_end=month_end,
                date_field=date_field, max_pages=max_pages,
            )
            all_items.extend(
                _prs_to_activity(raw_prs, github_repo, project_name, date_field)
            )

        if "commits" in sources:
            commit_items = fetch_commits_for_repo(
                owner=owner, repo=repo, user=github_user, token=github_token,
                month_start=month_start, month_end=month_end, max_pages=max_pages,
            )
            for item in commit_items:
                item.project = project_name
            all_items.extend(commit_items)

        if "issues_created" in sources:
            issue_items = fetch_issues_for_repo(
                owner=owner, repo=repo, user=github_user, token=github_token,
                month_start=month_start, month_end=month_end, max_pages=max_pages,
            )
            for item in issue_items:
                item.project = project_name
            all_items.extend(issue_items)

        if "prs_reviewed" in sources:
            review_items = fetch_reviewed_prs_for_repo(
                owner=owner, repo=repo, user=github_user, token=github_token,
                month_start=month_start, month_end=month_end, max_pages=max_pages,
            )
            for item in review_items:
                item.project = project_name
            all_items.extend(review_items)

    if not all_items:
        log.warning(
            f"No activity found for {github_user} in {month_str}. "
            "Output file will be empty."
        )

    # Deduplicate (same source + ref + project + date shouldn't appear twice)
    seen: set[tuple] = set()
    deduped: list[ActivityItem] = []
    for item in all_items:
        key = (item.source, item.ref, item.project, item.date)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    all_items = deduped

    # Determine cutoff from SQLite sync state
    db.init_db()
    if args.all:
        cutoff = month_start
    else:
        last_sync = db.get_last_sync_date()
        if last_sync:
            cutoff = last_sync + timedelta(days=1)
            log.info(
                f"Last sync: {last_sync} -> generating from {cutoff} onwards. "
                "(--all to include everything)"
            )
        else:
            cutoff = month_start

    # Group and build YAML entries
    grouped = group_by_date(all_items)
    day_outputs: list[DayOutput] = []

    for day, items_by_project in sorted(grouped.items()):
        if day < cutoff:
            log.debug(f"  {day}: skipped (already synced)")
            continue
        entries = make_day_entries(
            items_by_project=items_by_project,
            all_project_names=all_project_names,
            default_hours=default_hours,
            split_strategy=split_strat,
        )
        if entries:
            day_outputs.append(DayOutput(date=day, entries=entries))
            log.debug(
                f"  {day}: {len(entries)} entry(ies) — "
                f"{[e.project for e in entries]}"
            )

    null_count = sum(
        1 for d in day_outputs for e in d.entries if e.hours is None
    )
    if null_count:
        log.warning(
            f"{null_count} entry(ies) have hours=null "
            "(split_strategy=manual). Edit the YAML before push.py."
        )

    output_path = Path(f"clockify_{month_str}.yaml")
    document = build_yaml_document(month_str, day_outputs)

    with output_path.open("w", encoding="utf-8") as f:
        yaml.dump(
            document, f,
            default_flow_style=False, allow_unicode=True, sort_keys=False,
        )

    log.info(f"Written {len(day_outputs)} day(s) -> {output_path}")
    print(f"\nFile ready: {output_path}")
    if null_count:
        print(f"  {null_count} entry(ies) need manual hours before pushing.")
    print(f"  Review and edit, then run:\n    python push.py --file {output_path}")


if __name__ == "__main__":
    main()
