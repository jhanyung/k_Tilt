"""The front page renders from edition data, escapes text once, and stays script-free."""
# ruff: noqa: E501

from pipeline import render

STORY = {
    "rank": 1, "headline": "김민석, 부승찬 발언 사과", "outlets": 30, "angles": 3,
    "keywords": ["사과", "재정"], "sides": {"여당": 12, "야당": 2, "그 외": 5}, "quoting": 19,
    "coverage": [
        {"o": "a", "n": "가신문", "g": "national", "t": "김민석, 부승찬 발언 사과", "u": "https://a/1", "p": "2026-10-20T22:00+00:00", "s": 0},
        {"o": "b", "n": "나방송", "g": "broadcast", "t": "부승찬 &amp;quot;사과&amp;quot; 김민석 경고", "u": "https://b/1", "p": "2026-10-20T22:05+00:00", "s": 0},
        {"o": "c", "n": "다일보", "g": "regional", "t": "생후 2개월 딸 숨지게 한 친부 송치", "u": "https://c/1", "p": "2026-10-20T22:10+00:00", "s": 1},
    ],
    "speakers": [{"s": "더불어민주당 김민석 대표", "r": "정당", "p": "더불어민주당", "side": "여당", "c": 12,
                  "q": "말과 행동을 무겁게 해야", "o": "가신문", "u": "https://a/1"}],
    "statements": [{"src": "ppp", "n": "국민의힘 대변인 논평·브리핑", "t": "<b>논평</b>", "u": "https://p/1", "d": "2026-10-20"}],
}  # fmt: skip
DATA = {
    "edition": {"id": 1, "date": "2026-10-21", "slot": "am", "cutoff": "2026-10-20T22:00+00:00",
                "headlines": 20456, "outlets_seen": 107, "outlets_listed": 117, "refused": ["MBC"],
                "blocked": [], "generated": "2026-10-20T22:30+00:00"},
    "stories": [STORY, dict(STORY, rank=2)],
    "more": [{"rank": 11, "headline": "열한 번째", "outlets": 20, "u": "https://x/11"}],
}  # fmt: skip


def test_front_page_renders_dense_and_safe():
    page = render.render(DATA, ["2026-10-21-am"])
    assert "<script" not in page
    assert "&lt;b&gt;논평&lt;/b&gt;" in page  # statement title escaped
    assert "부승찬 &quot;사과&quot; 김민석 경고" in page  # double-encoded title decoded once
    assert "여당 <b>12</b>" in page and "11~20위" in page and "수집거부" in page


def test_headline_comparison_skips_other_events():
    picked = render.contrast(STORY, 3)
    assert all(c["o"] != "c" for c in picked)
