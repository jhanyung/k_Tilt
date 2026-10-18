from pipeline.robots import Robots

BOT = "giulgibot"


def can(text: str, url: str) -> bool:
    return Robots.parse(text).can_fetch(BOT, url)


def test_all_star_groups_are_merged():
    # 농민신문's real layout: two early '*' groups, then a final '* / Disallow: /'.
    text = """
User-agent: *
Disallow: /print/
User-agent: *
Disallow: /searchMain

User-agent: Googlebot
Allow: /

User-agent: *
Disallow: /
"""
    assert not can(text, "https://www.nongmin.com/sitemap.xml")
    assert not can(text, "https://www.nongmin.com/print/1")


def test_named_search_engines_only():
    # 아시아경제 style: named crawlers allowed, everyone else refused.
    text = "User-agent: Googlebot\nAllow: /\n\nUser-agent: *\nDisallow: /\n"
    assert not can(text, "https://www.asiae.co.kr/rss/all.xml")


def test_specific_group_overrides_star():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: GiulgiBot\nAllow: /\n"
    assert can(text, "https://example.com/news/1")


def test_longest_match_wins_and_allow_wins_ties():
    text = "User-agent: *\nDisallow: /news/\nAllow: /news/politics/\nDisallow: /x\nAllow: /x\n"
    assert can(text, "https://e.com/news/politics/1")
    assert not can(text, "https://e.com/news/sports/1")
    assert can(text, "https://e.com/x")


def test_wildcards_and_end_anchor():
    text = "User-agent: *\nDisallow: /*.php$\nDisallow: /tmp*/private\n"
    assert not can(text, "https://e.com/a/b.php")
    assert can(text, "https://e.com/a/b.php?x=1")
    assert not can(text, "https://e.com/tmp123/private")


def test_empty_disallow_allows_everything():
    assert can("User-agent: *\nDisallow:\n", "https://e.com/anything")


def test_full_url_rules_are_honoured_as_paths():
    text = "User-agent: *\nDisallow : https://www.skyedaily.com/news/news_view.html?ID=1\n"
    assert not can(text, "https://www.skyedaily.com/news/news_view.html?ID=1")
    assert can(text, "https://www.skyedaily.com/news/news_view.html?ID=2")


def test_mediatoday_rss_dir_blocked_but_sitemap_allowed():
    text = "User-agent: *\nDisallow:/rss/\nDisallow:/bbs/\nSitemap:https://www.mediatoday.co.kr/sitemap.xml\n"
    r = Robots.parse(text)
    assert not r.can_fetch(BOT, "https://www.mediatoday.co.kr/rss/allArticle.xml")
    assert r.can_fetch(BOT, "https://www.mediatoday.co.kr/sitemap.xml")
    assert r.sitemaps == ["https://www.mediatoday.co.kr/sitemap.xml"]


def test_robots_txt_itself_always_allowed_and_deny_all():
    assert Robots.parse("User-agent: *\nDisallow: /\n").can_fetch(BOT, "https://e.com/robots.txt")
    assert not Robots.deny_all().can_fetch(BOT, "https://e.com/")


def test_unreachable_is_marked_separately_from_refusal():
    r = Robots.deny_all(unreachable=True)
    assert r.unreachable and not r.can_fetch(BOT, "https://e.com/")
    assert not Robots.deny_all().unreachable


def test_blocked_is_unreachable_but_marked():
    rb = Robots.deny_all(blocked=True)
    assert rb.blocked and rb.unreachable
    assert not rb.can_fetch(BOT, "https://e.com/")
