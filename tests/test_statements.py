"""Statement lists, matching statements to stories, and within-story scoring."""

from collections import Counter
from datetime import date

from pipeline import lexicon, statements
from pipeline.analyse import anchors

KOREA_KR = """
<div class="list_type fact"><a href="/briefing/actuallyView.do?newsId=111" onclick="x">
<span class="text"><strong>(보도설명) 정부는 국채 이자를 안정적으로 관리 중입니다</strong>
<span class="source"><span>2026.10.18</span><span>기획예산처</span></span></span></a></div>
<tr><td>국무조정실</td><td class="subject">
<a href="/briefing/policyBriefingView.do?newsId=222&amp;pageIndex=1">
<strong>제1차 주택공급 정례브리핑 </strong></a>
<a href="/briefing/policyBriefingView.do?newsId=222"><span class="vid">수어</span></a></td>
<td>2026.10.16</td></tr>
"""
MINJOO = """
<div class="board-item"><a href="./view.php?sno=0&brd=11&post=1222495&search=">
<span>[이용우 대변인] 사법부 구성원들은 침묵하지 마십시오</span></a>
<time datetime="2023-02-14">2026-10-18</time></div>
"""
PPP = """<tr><td class="sbj"><a href="/news/comment_view/BBSDD0001/116424?page=1&">
정쟁으로 몰아가는 나쁜 버릇 버리십시오 [국민의힘 함인경 대변인 논평]</a></td>
<td class="date">2026-10-18</td></tr>"""


def test_korea_kr_list_dates_inside_and_after_links():
    got = statements.parse_list(
        KOREA_KR, "https://www.korea.kr/briefing/x.do", r"View\.do\?newsId=\d+", "s"
    )
    by_url = {s.url: s for s in got}
    a = by_url["https://www.korea.kr/briefing/actuallyView.do?newsId=111"]
    assert a.published_on == date(2026, 10, 18) and a.title.startswith("(보도설명)")
    b = by_url["https://www.korea.kr/briefing/policyBriefingView.do?newsId=222"]
    assert b.title == "제1차 주택공급 정례브리핑" and b.published_on == date(2026, 10, 16)
    assert len(got) == 2  # the 수어 icon link is the same statement


def test_party_lists_ignore_dates_in_attributes():
    (m,) = statements.parse_list(
        MINJOO,
        "https://theminjoo.kr/main/sub/news/list.php?brd=11",
        r"view\.php\?(?:sno=\d+&(?:amp;)?)?brd=11&(?:amp;)?post=\d+",
        "minjoo",
    )
    assert m.url == "https://theminjoo.kr/main/sub/news/view.php?brd=11&post=1222495"
    assert m.published_on == date(2026, 10, 18)
    (p,) = statements.parse_list(
        PPP, "https://www.peoplepowerparty.kr/news/comment/BBSDD0001", r"BBSDD0001/\d+", "ppp"
    )
    assert p.url == "https://www.peoplepowerparty.kr/news/comment_view/BBSDD0001/116424"


def test_match_sends_a_statement_to_its_story_only():
    stories = {
        1: ["추미애 소방 재정 빨대 발언 사과", "추미애 지사 사과에 국힘 끝까지 오만"],
        2: ["다카이치 야스쿠니 공물 봉납", "정부 야스쿠니 공물에 깊은 유감"],
    }
    got = anchors.match(
        stories, [(10, "추미애 지사의 사과는 끝까지 오만했습니다"), (11, "계란 수입")]
    )
    assert [s for s, _ in got[1]] == [10] and got[2] == []


def test_log_odds_favours_each_sides_own_words():
    a = Counter({"내란": 30, "사법": 10, "국민": 50})
    b = Counter({"동맹": 30, "안보": 10, "국민": 50})
    z = anchors.log_odds(a, b, a + b)
    assert z["내란"] > 2 and z["동맹"] < -2 and abs(z["국민"]) < 1


def test_story_scores_are_relative_within_a_story():
    arts = [
        (f"o{i}", {"words": 1000, "hits": {"국민의힘": i, "더불어민주당": 5}}) for i in range(6)
    ]
    z = lexicon.story_scores(arts, ["국민의힘"], ["더불어민주당"])
    assert z["o5"] > 0 > z["o0"]
    assert abs(sum(z.values())) < 1e-9


def test_offset_pagination():
    src = {
        "list": "https://theminjoo.kr/main/sub/news/list.php?brd=11",
        "page_param": "sno",
        "page_step": 20,
    }
    assert statements.page_url(src, 1) == src["list"]
    assert statements.page_url(src, 3).endswith("brd=11&sno=40")
    ppp = {"list": "https://x/news/comment/BBSDD0001", "page_param": "page"}
    assert statements.page_url(ppp, 2) == "https://x/news/comment/BBSDD0001?page=2"
