import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "match_and_save.py"
SPEC = importlib.util.spec_from_file_location("match_and_save", MODULE_PATH)
match_and_save = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(match_and_save)


def test_get_current_term_uses_latest_end_date():
    legislator = {
        "id": {"bioguide": "X001"},
        "terms": [
            {"end": "2024-01-03", "url": "https://old.house.gov"},
            {"end": "2030-01-03", "url": "https://new.house.gov"},
            {"end": "2026-01-03", "url": "https://mid.house.gov"},
        ],
    }

    today = dt.date(2026, 1, 1)
    term = match_and_save.get_current_term(legislator, today=today)

    assert term is not None
    assert term["url"] == "https://new.house.gov"


def test_build_legislator_index_uses_current_term_url_and_ids():
    legislators = [
        {
            "id": {"bioguide": "I000056", "thomas": "01713"},
            "terms": [
                {"end": "2024-01-03", "url": "https://old.issa.house.gov"},
                {"end": "2029-01-03", "url": "https://www.issa.house.gov"},
            ],
        }
    ]

    index = match_and_save.build_legislator_index(legislators)
    assert "issa.house.gov" in index
    assert index["issa.house.gov"][0]["bioguide_id"] == "I000056"
    assert index["issa.house.gov"][0]["thomas_id"] == "01713"
    assert index["issa.house.gov"][0]["lis_id"] is None


def test_normalize_domain_handles_www_and_case():
    assert match_and_save.normalize_domain("HTTPS://WWW.Example.House.Gov/path?a=1") == "example.house.gov"
    assert match_and_save.normalize_domain("www.ISSA.house.gov") == "issa.house.gov"


def test_match_member_unique_domain():
    index = {
        "issa.house.gov": [
            {
                "bioguide_id": "I000056",
                "thomas_id": "01713",
                "lis_id": None,
                "term_domain": "issa.house.gov",
            }
        ]
    }
    release = {"domain": "www.issa.house.gov", "url": "https://issa.house.gov/media/x"}

    member = match_and_save.match_member(release, index)

    assert member
    assert member["bioguide_id"] == "I000056"


def test_match_member_unresolved_when_missing_domain_match():
    index = {
        "cantwell.senate.gov": [
            {"bioguide_id": "C000127", "thomas_id": "00172", "lis_id": "S275", "term_domain": "cantwell.senate.gov"}
        ]
    }
    release = {"domain": "unknown.house.gov", "url": "https://unknown.house.gov/media/x"}

    assert match_and_save.match_member(release, index) is None


@pytest.mark.parametrize(
    "html",
    [
        '<meta property="article:published_time" content="2025-06-01T12:00:00Z">',
        '<meta name="publishdate" content="June 1, 2025">',
        '<time datetime="2025-06-01T12:00:00Z"></time>',
        '<time>June 1, 2025</time>',
        '<script type="application/ld+json">{"datePublished":"2025-06-01"}</script>',
    ],
)
def test_parse_date_from_html_sources(html):
    wrapped = f"<html><head>{html}</head><body></body></html>"
    parsed = match_and_save.parse_date_from_html(wrapped)
    assert parsed == dt.date(2025, 6, 1)


def test_process_releases_skips_when_no_date(tmp_path, monkeypatch):
    releases = [{"url": "https://issa.house.gov/media/pr1", "source": "https://issa.house.gov/media/press-releases", "domain": "issa.house.gov", "date": None}]
    index = {
        "issa.house.gov": [
            {"bioguide_id": "I000056", "thomas_id": "01713", "lis_id": None, "term_domain": "issa.house.gov"}
        ]
    }

    monkeypatch.setattr(match_and_save, "recover_publish_date", lambda **kwargs: None)

    summary = match_and_save.process_releases(releases, index, tmp_path, timeout=1, max_retries=0)

    assert summary["skipped_no_date"] == 1
    assert summary["written"] == 0


def test_build_output_path_format():
    url = "https://issa.house.gov/media/pr1"
    publish_date = dt.date(2025, 1, 2)
    out_path = match_and_save.build_output_path(Path("data"), publish_date, url)
    expected_hash = hashlib.md5(url.encode("utf-8")).hexdigest()

    assert str(out_path).endswith(f"data/2025/01/02/{expected_hash}.json")


def test_existing_file_is_skipped(tmp_path, monkeypatch):
    url = "https://issa.house.gov/media/pr1"
    publish_date = dt.date(2025, 1, 2)

    releases = [{"url": url, "source": "https://issa.house.gov/media/press-releases", "domain": "issa.house.gov", "date": publish_date}]
    index = {
        "issa.house.gov": [
            {"bioguide_id": "I000056", "thomas_id": "01713", "lis_id": None, "term_domain": "issa.house.gov"}
        ]
    }

    out_path = match_and_save.build_output_path(tmp_path, publish_date, url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("already-here", encoding="utf-8")

    monkeypatch.setattr(match_and_save, "recover_publish_date", lambda **kwargs: dt.date(2025, 1, 2))

    summary = match_and_save.process_releases(releases, index, tmp_path, timeout=1, max_retries=0)

    assert summary["skipped_exists"] == 1
    assert out_path.read_text(encoding="utf-8") == "already-here"


def test_written_payload_contains_required_fields(tmp_path):
    url = "https://issa.house.gov/media/pr1"
    releases = [
        {
            "url": url,
            "source": "https://issa.house.gov/media/press-releases",
            "domain": "issa.house.gov",
            "date": dt.date(2025, 3, 4),
        }
    ]
    index = {
        "issa.house.gov": [
            {"bioguide_id": "I000056", "thomas_id": "01713", "lis_id": None, "term_domain": "issa.house.gov"}
        ]
    }

    summary = match_and_save.process_releases(releases, index, tmp_path, timeout=1, max_retries=0)
    assert summary["written"] == 1

    out_path = match_and_save.build_output_path(tmp_path, dt.date(2025, 3, 4), url)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    assert payload == {
        "bioguide_id": "I000056",
        "lis_id": None,
        "publish_date": "2025-03-04",
        "source": "https://issa.house.gov/media/press-releases",
        "thomas_id": "01713",
        "url": url,
    }


def test_summary_counters_mixed_outcomes(tmp_path, monkeypatch):
    releases = [
        {"url": "", "domain": "issa.house.gov"},
        {"url": "https://no-member.house.gov/pr", "domain": "no-member.house.gov", "date": dt.date(2025, 1, 1)},
        {"url": "https://issa.house.gov/no-date", "domain": "issa.house.gov", "date": None},
        {"url": "https://issa.house.gov/write", "domain": "issa.house.gov", "date": dt.date(2025, 1, 1), "source": "https://issa.house.gov/media/press-releases"},
    ]
    index = {
        "issa.house.gov": [
            {"bioguide_id": "I000056", "thomas_id": "01713", "lis_id": None, "term_domain": "issa.house.gov"}
        ]
    }

    def fake_recover(url, **kwargs):
        if url.endswith("no-date"):
            return None
        return dt.date(2025, 1, 1)

    monkeypatch.setattr(match_and_save, "recover_publish_date", fake_recover)

    summary = match_and_save.process_releases(releases, index, tmp_path, timeout=1, max_retries=0)

    assert summary == {
        "scraped": 4,
        "matched": 2,
        "written": 1,
        "skipped_exists": 0,
        "skipped_no_date": 1,
        "skipped_no_member": 1,
        "skipped_invalid_url": 1,
    }


def test_run_loads_legislators_file_and_prints_summary(tmp_path, monkeypatch, capsys):
    legislators_file = tmp_path / "legislators-current.json"
    legislators_file.write_text(
        json.dumps(
            [
                {
                    "id": {"bioguide": "I000056"},
                    "terms": [{"end": "2030-01-03", "url": "https://issa.house.gov"}],
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(match_and_save.Scraper, "member_scrapers", lambda: [])

    exit_code = match_and_save.run(
        [
            "--legislators-file",
            str(legislators_file),
            "--output-dir",
            str(tmp_path / "data"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "scraped=0" in captured.out
    assert "written=0" in captured.out
