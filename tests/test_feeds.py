from pipeline import feeds

KBS_LIKE = """<?xml version='1.0' encoding='UTF-8'?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url>
    <loc>https://news.kbs.co.kr/news/view.do?ncd=1</loc>
    <lastmod>2026-10-17T21:30:47+09:00</lastmod>
    <news:news>
      <news:publication_date>2026-10-17T21:30:47+09:00</news:publication_date>
      <news:title>인천 공장 건물서 불</news:title>
    </news:news>
  </url>
  <url><loc>https://news.kbs.co.kr/no-title</loc></url>
</urlset>""".encode()

# JTBC writes an un-prefixed <title> whose text is an escaped CDATA block.
JTBC_LIKE = (
    '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
    'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url>'
    "<loc>https://news.jtbc.co.kr/article/NB1</loc><news:news>"
    "<title>&lt;![CDATA[[날씨] 가을비]]&gt;</title>"
    "<publication_date>2026-10-17T21:27:19+09:00</publication_date>"
    "</news:news></url></urlset>"
).encode()

INDEX = b"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc><![CDATA[https://www.ytn.co.kr/sitemap/sitemap0.xml]]></loc></sitemap>
</sitemapindex>"""


def test_registered_domain_handles_korean_second_level():
    assert feeds.registered_domain("https://www.hani.co.kr/arti/1") == "hani.co.kr"
    assert feeds.registered_domain("https://rss.donga.com/total.xml") == "donga.com"
    assert feeds.registered_domain("news.kbs.co.kr") == "kbs.co.kr"
    assert feeds.registered_domain("https://www.newscham.net/x") == "newscham.net"


def test_news_sitemap_keeps_only_titled_entries():
    kind, items = feeds.parse_sitemap(KBS_LIKE)
    assert kind == "urlset"
    assert len(items) == 1
    assert items[0].title == "인천 공장 건물서 불"
    assert items[0].published is not None


def test_sitemap_title_cdata_is_unwrapped():
    _, items = feeds.parse_sitemap(JTBC_LIKE)
    assert items[0].title == "[날씨] 가을비"


def test_sitemap_index_returns_children():
    kind, children = feeds.parse_sitemap(INDEX)
    assert kind == "index"
    assert children == ["https://www.ytn.co.kr/sitemap/sitemap0.xml"]


def test_non_sitemap_xml_is_not_mistaken_for_one():
    assert feeds.parse_sitemap(b"<rss><channel></channel></rss>") == ("", [])


def test_kst_mislabel_shifted_back_only_when_in_future_window():
    now = 1_800_000_000.0
    assert feeds.fix_kst_mislabel(now + 9 * 3600, now) == now
    assert feeds.fix_kst_mislabel(now - 3600, now) == now - 3600  # normal past item untouched
    assert feeds.fix_kst_mislabel(now + 30 * 3600, now) == now + 30 * 3600  # not a 9h slip
    assert feeds.fix_kst_mislabel(None, now) is None
