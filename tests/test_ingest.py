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
            assert f["kind"] in {"rss", "sitemap", "page"}, oid


def test_plan_rows_drops_known_urls_and_repeated_titles():
    a = ingest.Item("hani", "https://www.hani.co.kr/arti/1", "[속보] 예산안 통과", 1e9, "rss")
    b = ingest.Item(
        "hani", "https://www.hani.co.kr/arti/2", "예산안 통과", 1e9, "rss"
    )  # same title
    c = ingest.Item(
        "khan", "https://www.khan.co.kr/a/9", "예산안 통과", None, "rss"
    )  # other outlet
    d = ingest.Item("khan", "https://www.khan.co.kr/a/10", "새 기사", None, "page")
    res = [ingest.OutletResult("hani", [a, b]), ingest.OutletResult("khan", [c, d])]
    known = {ingest.sha1(d.url)}
    rows, dup = ingest.plan_rows(res, known, recent_keys=set())
    assert [r[1] for r in rows] == [a.url, c.url]
    assert dup == 2


def test_plan_rows_respects_titles_seen_in_last_48h():
    a = ingest.Item("hani", "https://www.hani.co.kr/arti/3", "검찰 수사 착수", None, "rss")
    rows, dup = ingest.plan_rows(
        [ingest.OutletResult("hani", [a])], set(), {("hani", ingest.title_key("검찰 수사 착수"))}
    )
    assert rows == [] and dup == 1


def test_relay_settings_derive_from_db_url():
    from pipeline import relay

    url = (
        "postgresql://postgres.abcdefref:pw@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    )
    assert relay.project_ref(url) == "abcdefref"
    assert relay.endpoint(url) == "https://abcdefref.supabase.co/functions/v1/relay"
    assert relay.relay_key(url) == relay.relay_key(url) and len(relay.relay_key(url)) == 64
    assert relay.relay_key(url) != relay.relay_key(url.replace(":pw@", ":pw2@"))
    assert relay.project_ref("postgresql://postgres:pw@db.x.supabase.co/postgres") == ""


def test_only_network_failures_never_counts_a_refusal():
    from pipeline import discover

    assert discover.only_network_failures({"a": "robots.txt unreachable", "b": "http error: X"})
    assert not discover.only_network_failures({"a": "http error: X", "b": "robots.txt disallows"})
    assert not discover.only_network_failures({"a": "status 404"})
    assert not discover.only_network_failures({})
