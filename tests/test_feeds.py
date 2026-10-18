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


LISTING = """<html><head><meta charset="utf-8"></head><body>
<a href="/site/data/html_dir/2026/10/17/2026101790105.html">[따져보니] 李, 집도 농지도 투기 척결</a>
<a href="/site/data/html_dir/2026/10/17/2026101790105.html">
[따져보니] 李, 집도 농지도 투기 척결 [앵커] 정부가 오늘</a>
<a href="/site/data/html_dir/2020/01/01/2020010190001.html">오래된 기사 제목은 건너뛴다 정말로</a>
<a href="https://other.com/news/123456">다른 사이트로 가는 링크는 무시합니다</a>
<a href="/member/login">로그인 하러 가기 버튼입니다 여기</a>
<a href="/site/data/html_dir/2026/10/17/2026101790200.html">
<span>국회 예산안 처리</span> 본회의 통과</a>
<a href="/about">회사 소개 페이지로 이동합니다 링크</a>
</body></html>""".encode()


def test_listing_page_keeps_recent_same_site_headlines():
    now = 1_792_300_000.0  # 2026-10-17
    items = feeds.parse_listing(LISTING, "https://news.tvchosun.com/", now)
    titles = sorted(e.title for e in items)
    assert titles == ["[따져보니] 李, 집도 농지도 투기 척결", "국회 예산안 처리 본회의 통과"]
    assert all(e.published is None for e in items)


def test_listing_page_decodes_euc_kr():
    html = '<meta charset="euc-kr"><a href="/news/123456">지역 소식 제목 열자 이상 입니다</a>'
    items = feeds.parse_listing(html.encode("cp949"), "https://www.example.co.kr/", 1e9)
    assert items[0].title == "지역 소식 제목 열자 이상 입니다"
