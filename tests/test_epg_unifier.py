from __future__ import annotations

import datetime as dt
import gzip
import json
import sys
from pathlib import Path

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import epg_unifier as eu  # noqa: E402

UTC = dt.timezone.utc


def write_xml(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def base_config() -> dict:
    return {
        "settings": {
            "future_days": 7,
            "past_hours": 6,
            "minimum_source_success_ratio": 0.5,
            "maximum_channel_drop_ratio": 0.9,
            "maximum_programme_drop_ratio": 0.9,
            "logo_api": {"enabled": False},
        },
        "outputs": [
            {"name": "global", "filename": "epg.xml", "include_regions": ["*"]},
            {"name": "europe", "filename": "epg-europe.xml", "include_regions": ["europe"]},
        ],
        "sources": [
            {"name": "p1", "url": "https://example.invalid/p1", "priority": 1, "order": 0, "region": "europe"},
            {"name": "p2", "url": "https://example.invalid/p2", "priority": 2, "order": 1, "region": "europe"},
        ],
    }


def test_exact_id_priority_variants_icon_fallback_and_window(tmp_path, monkeypatch):
    p1 = tmp_path / "p1.xml"
    p2 = tmp_path / "p2.xml"
    write_xml(
        p1,
        """<?xml version="1.0"?><tv>
        <channel id="BBCOne.uk"><display-name>BBC One</display-name></channel>
        <channel id="Same.ID"><display-name>Priority One</display-name></channel>
        <programme channel="BBCOne.uk" start="20260101120000 +0000" stop="20260101130000 +0000"><title>P1 BBC</title></programme>
        <programme channel="Same.ID" start="20260101130000 +0000" stop="20260101140000 +0000"><title>P1 wins</title></programme>
        </tv>""",
    )
    write_xml(
        p2,
        """<?xml version="1.0"?><tv>
        <channel id="Same.ID"><display-name>Priority Two</display-name><icon src="https://logo.test/same.png"/></channel>
        <channel id="bbc1.uk"><display-name>BBC One variant</display-name></channel>
        <channel id="BBC.ONE.HD"><display-name>BBC One HD variant</display-name></channel>
        <programme channel="Same.ID" start="20260101150000 +0000" stop="20260101160000 +0000"><title>Must be discarded</title></programme>
        <programme channel="bbc1.uk" start="20260101160000 +0000" stop="20260101170000 +0000"><title>Variant kept</title></programme>
        <programme channel="BBC.ONE.HD" start="20260109160000 +0000" stop="20260109170000 +0000"><title>Outside seven days</title></programme>
        </tv>""",
    )
    sources = eu.build_sources(base_config())
    results = [
        eu.FetchResult(sources[0], p1, "test"),
        eu.FetchResult(sources[1], p2, "test"),
    ]
    monkeypatch.setattr(eu, "fetch_all", lambda sources, cache_dir, settings: results)
    monkeypatch.setattr(eu, "build_logo_map", lambda settings, cache_dir: {})

    out = tmp_path / "out"
    stats = eu.merge(base_config(), tmp_path / "cache", out, now=dt.datetime(2026, 1, 1, 10, tzinfo=UTC))

    assert stats["outputs"]["global"]["channels"] == 4
    assert stats["outputs"]["global"]["programmes"] == 3
    root = etree.parse(str(out / "epg.xml")).getroot()
    ids = [node.get("id") for node in root.findall("channel")]
    assert ids == ["BBCOne.uk", "Same.ID", "bbc1.uk", "BBC.ONE.HD"]
    same = root.find("channel[@id='Same.ID']")
    assert same is not None
    assert same.findtext("display-name") == "Priority One"
    assert same.find("icon").get("src") == "https://logo.test/same.png"
    titles = [node.findtext("title") for node in root.findall("programme")]
    assert "P1 wins" in titles
    assert "Must be discarded" not in titles
    assert "Variant kept" in titles
    assert "Outside seven days" not in titles

    with gzip.open(out / "epg.xml.gz", "rb") as handle:
        assert handle.read(5) == b"<?xml"
    assert (out / "status.json").exists()


def test_parse_xmltv_time_offsets():
    parsed = eu.parse_xmltv_time("20260101120000 +0200")
    assert parsed == dt.datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert eu.parse_xmltv_time("bad") is None
