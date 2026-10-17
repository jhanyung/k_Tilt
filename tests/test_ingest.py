import json
from pathlib import Path

from pipeline import ingest


def test_canonical_url_drops_tracking_and_fragment_keeps_ids():
    u = "https://WWW.Example.co.kr/news/articleView.html?idxno=123&utm_source=rss&ref=rss#top"
    assert ingest.canonical_url(u) == "https://www.example.co.kr/news/articleView.html?idxno=123"


def test_canonical_url_keeps_scheme():
    assert ingest.canonical_url("http://rss.ohmynews.com/a/").startswith("http://")


def test_title_key_ignores_tags_spacing_and_punctuation():
    a = ingest.title_key("[속보] 여야, 예산안 합의…본회의 처리")
    b = ingest.title_key("여야 예산안 합의 본회의 처리")
    c = ingest.title_key("[단독][종합] 여야, 예산안 합의 … 본회의 처리!")
    assert a == b == c


def test_title_key_distinguishes_different_titles():
    assert ingest.title_key("여야 예산안 합의") != ingest.title_key("여야 예산안 결렬")


def test_lock_file_is_well_formed():
    lock = json.loads(
        (Path(__file__).resolve().parent.parent / "pipeline/data/feeds.lock.json").read_text()
    )
    assert len(lock["outlets"]) >= 80
    for oid, o in lock["outlets"].items():
        assert o["feeds"], oid
        assert o["home"].startswith("https://"), oid
        for f in o["feeds"]:
            assert f["kind"] in {"rss", "sitemap"}, oid
