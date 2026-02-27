#!/usr/bin/env python3
"""Match scraped press releases to legislators and save enriched records."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

# Ensure local package imports work when running as `python/uv run scripts/...`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from python_statement import Scraper


def normalize_domain(value: str | None) -> str | None:
    """Normalize a domain or URL to its bare lowercase hostname without www."""
    if not value:
        return None

    raw = value.strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    hostname = parsed.hostname

    if hostname is None:
        parsed = urlparse(f"https://{raw}")
        hostname = parsed.hostname

    if not hostname:
        return None

    host = hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def parse_date_value(value: Any) -> dt.date | None:
    """Parse date-like values into datetime.date."""
    if value is None:
        return None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date_parser.parse(text, fuzzy=True).date()
        except (ValueError, TypeError, OverflowError):
            return None
    return None


def get_current_term(legislator: dict[str, Any], today: dt.date | None = None) -> dict[str, Any] | None:
    """Get the latest current term where end >= today."""
    if today is None:
        today = dt.date.today()

    current_terms: list[tuple[dt.date, dict[str, Any]]] = []
    for term in legislator.get("terms", []):
        end_text = term.get("end")
        end_date = parse_date_value(end_text)
        if end_date is None:
            continue
        if end_date >= today:
            current_terms.append((end_date, term))

    if not current_terms:
        return None

    current_terms.sort(key=lambda item: item[0])
    return current_terms[-1][1]


def build_legislator_index(legislators: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build a normalized domain -> member metadata index for current legislators."""
    domain_index: dict[str, list[dict[str, Any]]] = {}

    for legislator in legislators:
        current_term = get_current_term(legislator)
        if not current_term:
            continue

        term_url = current_term.get("url")
        term_domain = normalize_domain(term_url)
        if not term_domain:
            continue

        leg_id = legislator.get("id", {})
        member = {
            "bioguide_id": leg_id.get("bioguide"),
            "thomas_id": leg_id.get("thomas"),
            "lis_id": leg_id.get("lis"),
            "term_domain": term_domain,
        }

        if not member["bioguide_id"]:
            continue

        domain_index.setdefault(term_domain, []).append(member)

    return domain_index


def extract_release_domain(release: dict[str, Any]) -> str | None:
    """Determine the best domain for member matching."""
    return (
        normalize_domain(release.get("domain"))
        or normalize_domain(release.get("source"))
        or normalize_domain(release.get("url"))
    )


def match_member(
    release: dict[str, Any], domain_index: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """Match a release to a member using domain-based logic."""
    primary_domain = extract_release_domain(release)
    if not primary_domain:
        return None

    candidates = domain_index.get(primary_domain, [])
    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    source_domain = normalize_domain(release.get("source"))
    if source_domain:
        narrowed = [c for c in candidates if c.get("term_domain") == source_domain]
        if len(narrowed) == 1:
            return narrowed[0]

    return None


def parse_date_from_html(html: str) -> dt.date | None:
    """Recover a publish date from a release page's HTML."""
    soup = BeautifulSoup(html, "lxml")

    def meta_content(attrs: dict[str, str]) -> str | None:
        tag = soup.find("meta", attrs=attrs)
        if tag:
            value = tag.get("content")
            if value:
                return str(value)
        return None

    fields: list[str] = []

    article_published = meta_content({"property": "article:published_time"})
    if article_published:
        fields.append(article_published)

    for meta_name in ["pubdate", "publishdate", "date"]:
        meta_val = meta_content({"name": meta_name})
        if meta_val:
            fields.append(meta_val)

    for time_tag in soup.find_all("time"):
        if time_tag.get("datetime"):
            fields.append(str(time_tag.get("datetime")))

    for time_tag in soup.find_all("time"):
        text = time_tag.get_text(" ", strip=True)
        if text:
            fields.append(text)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for item in _iter_jsonld_nodes(parsed):
            date_published = item.get("datePublished") if isinstance(item, dict) else None
            if date_published:
                fields.append(str(date_published))

    for field in fields:
        parsed_date = parse_date_value(field)
        if parsed_date:
            return parsed_date

    return None


def _iter_jsonld_nodes(node: Any):
    """Yield dict nodes from JSON-LD structures recursively."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_jsonld_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_jsonld_nodes(item)


def recover_publish_date(
    url: str, timeout: int = 10, max_retries: int = 1, verbose: bool = False
) -> dt.date | None:
    """Fetch a release page and try to recover a publish date."""
    attempts = max(1, max_retries + 1)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            recovered = parse_date_from_html(response.text)
            if recovered:
                return recovered
        except requests.RequestException as exc:
            last_error = exc
            if verbose:
                print(f"date_recovery_request_error url={url} attempt={attempt} error={exc}")

    if verbose and last_error:
        print(f"date_recovery_failed url={url} error={last_error}")

    return None


def build_output_path(output_dir: Path, publish_date: dt.date, url: str) -> Path:
    """Build output path data/YYYY/MM/DD/md5(url).json."""
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()
    year = f"{publish_date.year:04d}"
    month = f"{publish_date.month:02d}"
    day = f"{publish_date.day:02d}"
    return output_dir / year / month / day / f"{digest}.json"


def process_releases(
    releases: list[dict[str, Any]],
    domain_index: dict[str, list[dict[str, Any]]],
    output_dir: Path,
    timeout: int,
    max_retries: int,
    verbose: bool = False,
) -> dict[str, int]:
    """Process releases and persist enriched JSON payloads."""
    summary = {
        "scraped": len(releases),
        "matched": 0,
        "written": 0,
        "skipped_exists": 0,
        "skipped_no_date": 0,
        "skipped_no_member": 0,
        "skipped_invalid_url": 0,
    }

    for idx, release in enumerate(releases, start=1):
        if not isinstance(release, dict):
            if verbose:
                print(f"skip_non_dict_release idx={idx}")
            continue

        url = release.get("url")
        if not isinstance(url, str) or not url.strip() or normalize_domain(url) is None:
            summary["skipped_invalid_url"] += 1
            if verbose:
                print(f"skip_invalid_url idx={idx} url={url}")
            continue

        member = match_member(release, domain_index)
        if not member:
            summary["skipped_no_member"] += 1
            if verbose:
                print(f"skip_no_member idx={idx} url={url}")
            continue

        summary["matched"] += 1

        publish_date = parse_date_value(release.get("date"))
        if publish_date is None:
            publish_date = recover_publish_date(
                url=url,
                timeout=timeout,
                max_retries=max_retries,
                verbose=verbose,
            )

        if publish_date is None:
            summary["skipped_no_date"] += 1
            if verbose:
                print(f"skip_no_date idx={idx} url={url}")
            continue

        out_path = build_output_path(output_dir, publish_date, url)
        if out_path.exists():
            summary["skipped_exists"] += 1
            if verbose:
                print(f"skip_exists idx={idx} path={out_path}")
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "bioguide_id": member.get("bioguide_id"),
            "thomas_id": member.get("thomas_id"),
            "lis_id": member.get("lis_id"),
            "url": url,
            "publish_date": publish_date.isoformat(),
            "source": release.get("source"),
        }

        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary["written"] += 1

        if verbose:
            print(f"written idx={idx} path={out_path}")

    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legislators-file", default="legislators-current.json")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    """Entrypoint runner for CLI usage."""
    args = parse_args(argv)

    legislators_path = Path(args.legislators_file)
    try:
        legislators = json.loads(legislators_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"error: legislators file not found: {legislators_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in legislators file {legislators_path}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: unable to read legislators file {legislators_path}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(legislators, list):
        print(
            f"error: legislators file {legislators_path} must contain a JSON list",
            file=sys.stderr,
        )
        return 1

    domain_index = build_legislator_index(legislators)

    try:
        releases = Scraper.member_scrapers()
    except Exception as exc:
        print(f"error: Scraper.member_scrapers() failed: {exc}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 1

    if not isinstance(releases, list):
        print("error: Scraper.member_scrapers() did not return a list", file=sys.stderr)
        return 1

    summary = process_releases(
        releases=releases,
        domain_index=domain_index,
        output_dir=Path(args.output_dir),
        timeout=args.timeout,
        max_retries=args.max_retries,
        verbose=args.verbose,
    )

    print(f"scraped={summary['scraped']}")
    print(f"matched={summary['matched']}")
    print(f"written={summary['written']}")
    print(f"skipped_exists={summary['skipped_exists']}")
    print(f"skipped_no_date={summary['skipped_no_date']}")
    print(f"skipped_no_member={summary['skipped_no_member']}")
    print(f"skipped_invalid_url={summary['skipped_invalid_url']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
