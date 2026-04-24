#!/usr/bin/env python3
"""
generate.py — GitHub PRs -> YAML intermediate file
====================================================
Reads PR activity from GitHub for the given month and produces a
human-reviewable YAML file (clockify_YYYY-MM.yaml) that push.py
later sends to Clockify.

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
class PRInfo:
    number: int
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

def load_config(path: Path) -> dict:
    """Load and minimally validate config.yaml."""
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
    return cfg


# -- GitHub API ---------------------------------------------------------------

GITHUB_BASE = "https://api.github.com"


def _github_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def check_github_response(resp: requests.Response) -> None:
    """Exit with a descriptive message on non-2xx GitHub responses."""
    if resp.ok:
        return
    code = resp.status_code
    if code == 401:
        sys.exit(
            "ERROR: GitHub token invalid or expired (401). "
            "Check 'github_token' in config."
        )
    if code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining", "?")
        reset_ts = resp.headers.get("X-RateLimit-Reset", "?")
        if remaining == "0":
            sys.exit(
                f"ERROR: GitHub rate limit exceeded (403). "
                f"Resets at unix timestamp {reset_ts}."
            )
        sys.exit(
            f"ERROR: GitHub returned 403 Forbidden for {resp.url}. "
            "Check that your token has 'repo' scope."
        )
    if code == 404:
        sys.exit(
            f"ERROR: GitHub repo not found (404): {resp.url}. "
            "Check 'github_repo' in config."
        )
    sys.exit(
        f"ERROR: GitHub API returned {code} for {resp.url}.\n"
        f"{resp.text[:300]}"
    )


def fetch_prs_for_repo(
    owner: str,
    repo: str,
    user: str,
    token: str,
    month_start: date,
    month_end: date,
    date_field: str,
    max_pages: int,
) -> list[dict]:
    """
    Fetch PRs authored by `user` whose `date_field` falls within
    [month_start, month_end]. Stops early for 'created_at' sorting.
    """
    headers = _github_headers(token)
    full_repo = f"{owner}/{repo}"

    if date_field == "created_at":
        extra = {"sort": "created", "direction": "desc"}
    else:
        extra = {"sort": "updated", "direction": "desc"}

    collected: list[dict] = []

    for page in range(1, max_pages + 1):
        params = {"state": "all", "per_page": 100, "page": page, **extra}
        log.debug(f"  Fetching {full_repo} page {page} ...")

        resp = requests.get(
            f"{GITHUB_BASE}/repos/{full_repo}/pulls",
            headers=headers,
            params=params,
            timeout=30,
        )
        check_github_response(resp)

        prs: list[dict] = resp.json()
        if not prs:
            break

        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining and int(remaining) < 20:
            log.warning(
                f"GitHub rate limit low: {remaining} requests remaining."
            )

        for pr in prs:
            if pr.get("user", {}).get("login") != user:
                continue
            raw = pr.get(date_field)
            if raw is None:
                continue
            pr_date = datetime.fromisoformat(
                raw.replace("Z", "+00:00")
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

    log.info(f"  {full_repo}: {len(collected)} PR(s) found.")
    return collected


# -- Grouping & distribution --------------------------------------------------

def build_pr_infos(
    raw_prs: list[dict],
    repo: str,
    project: str,
    date_field: str,
) -> list[PRInfo]:
    """Convert raw GitHub PR dicts to typed PRInfo objects."""
    infos: list[PRInfo] = []
    for pr in raw_prs:
        raw_date = pr.get(date_field)
        if raw_date is None:
            continue
        pr_date = datetime.fromisoformat(
            raw_date.replace("Z", "+00:00")
        ).date()
        infos.append(PRInfo(
            number=pr["number"],
            title=pr["title"].strip(),
            repo=repo,
            project=project,
            date=pr_date,
        ))
    return infos


def build_description(prs: list[PRInfo]) -> str:
    """Build 'PR #N Title | PR #M Other title' string."""
    parts = [
        f"PR #{pr.number} {pr.title}"
        for pr in sorted(prs, key=lambda p: p.number)
    ]
    return " | ".join(parts)


def group_by_date(
    all_prs: list[PRInfo],
) -> dict[date, dict[str, list[PRInfo]]]:
    """Return {date: {project_name: [PRInfo, ...]}}."""
    grouped: dict[date, dict[str, list[PRInfo]]] = {}
    for pr in all_prs:
        grouped.setdefault(pr.date, {}).setdefault(pr.project, []).append(pr)
    return grouped


def make_day_entries(
    prs_by_project: dict[str, list[PRInfo]],
    all_project_names: list[str],
    default_hours: float,
    split_strategy: str,
) -> list[YAMLEntry]:
    """
    Compute YAMLEntry objects for one day.
    - 1 active project  -> default_hours to that project
    - 2 active projects -> split equally or set null (manual)
    """
    active = {proj: prs for proj, prs in prs_by_project.items() if prs}
    n = len(active)

    if n == 0:
        return []

    if n == 1:
        proj, prs = next(iter(active.items()))
        return [YAMLEntry(
            project=proj,
            hours=float(default_hours),
            description=build_description(prs),
        )]

    hours_each: Optional[float] = (
        round(default_hours / n, 2)
        if split_strategy == "equal"
        else None
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

def build_yaml_document(
    month_str: str,
    day_outputs: list[DayOutput],
) -> dict:
    """Build the dict that will be serialised to YAML."""
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
        description="Generate a Clockify-ready YAML from GitHub PR activity.",
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

    # Parse month
    try:
        year, month = (int(x) for x in args.month.split("-"))
        _, days_in_month = calendar.monthrange(year, month)
        month_start = date(year, month, 1)
        month_end = date(year, month, days_in_month)
    except (ValueError, TypeError):
        sys.exit(
            f"ERROR: --month must be YYYY-MM, got '{args.month}'."
        )

    month_str = f"{year}-{month:02d}"
    log.info(f"=== generate.py | month={month_str} ===")

    # Load config
    cfg = load_config(args.config)
    github_user = cfg["github_user"]
    github_token = cfg["github_token"]
    date_field = cfg["pr_date_field"]
    default_hours = float(cfg["default_hours"])
    split_strat = cfg["split_strategy"]
    max_pages = int(cfg["max_pages"])
    repos_cfg: list[dict] = cfg["repos"]

    all_project_names = [r["clockify_project"] for r in repos_cfg]

    # Fetch PRs
    all_prs: list[PRInfo] = []
    for repo_entry in repos_cfg:
        github_repo = repo_entry["github_repo"]
        project_name = repo_entry["clockify_project"]
        owner, repo = github_repo.split("/", 1)

        log.info(f"Fetching PRs from {github_repo} -> '{project_name}' ...")
        raw_prs = fetch_prs_for_repo(
            owner=owner,
            repo=repo,
            user=github_user,
            token=github_token,
            month_start=month_start,
            month_end=month_end,
            date_field=date_field,
            max_pages=max_pages,
        )
        all_prs.extend(
            build_pr_infos(raw_prs, github_repo, project_name, date_field)
        )

    if not all_prs:
        log.warning(
            f"No PRs found for {github_user} in {month_str}. "
            "Output file will be empty."
        )

    # Determine cutoff from SQLite sync state
    db.init_db()
    if args.all:
        cutoff = month_start
    else:
        last_sync = db.get_last_sync_date()
        if last_sync:
            cutoff = last_sync + timedelta(days=1)
            log.info(
                f"Last sync: {last_sync} -> generating from {cutoff} onwards."
                " (--all to include everything)"
            )
        else:
            cutoff = month_start

    # Group and build entries
    grouped = group_by_date(all_prs)
    day_outputs: list[DayOutput] = []

    for day, prs_by_project in sorted(grouped.items()):
        if day < cutoff:
            log.debug(f"  {day}: skipped (already synced)")
            continue
        entries = make_day_entries(
            prs_by_project=prs_by_project,
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

    # Warn about manual entries
    null_count = sum(
        1 for d in day_outputs for e in d.entries if e.hours is None
    )
    if null_count:
        log.warning(
            f"{null_count} entry(ies) have hours=null "
            "(split_strategy=manual). Edit the YAML before push.py."
        )

    # Write YAML
    output_path = Path(f"clockify_{month_str}.yaml")
    document = build_yaml_document(month_str, day_outputs)

    with output_path.open("w", encoding="utf-8") as f:
        yaml.dump(
            document,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    log.info(f"Written {len(day_outputs)} day(s) -> {output_path}")
    print(f"\nFile ready: {output_path}")
    if null_count:
        print(f"  {null_count} entry(ies) need manual hours before pushing.")
    print(f"  Review and edit, then run:\n    python push.py --file {output_path}")


if __name__ == "__main__":
    main()
