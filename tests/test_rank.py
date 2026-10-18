"""Ranking tests on small hand-built inputs, so CI needs neither the embedding model nor
the Korean analyser. The similarity matrix stands in for embeddings + n-grams."""

from datetime import UTC, datetime

import numpy as np

from pipeline import rank as R


def test_normalise_strips_tags_and_expands_hanja():
    assert R.normalise("[속보][단독] 與, 예산안 처리…野 반발") == "여당 예산안 처리 야당 반발"


def test_keep_drops_photos_and_roundups():
    assert not R.keep("[포토] 국회 본회의장 모습")
    assert not R.keep("[아주경제 오늘의 뉴스 종합] 대통령 조문 外")
    assert not R.keep("부천 화재 소방관 조문 外")
    assert R.keep("이 대통령, 순직 소방관 조문")


def test_edition_window_cutoffs_in_kst():
    # 2026-10-18 08:30 KST -> morning edition cut at 07:00 KST the same day
    end, label = R.edition_window("am", datetime(2026, 10, 17, 23, 30, tzinfo=UTC))
    assert label == "2026-10-18" and end == datetime(2026, 10, 17, 22, 0, tzinfo=UTC)
    # 2026-10-18 06:00 KST -> before 07:00, so the previous day's morning edition
    end, label = R.edition_window("am", datetime(2026, 10, 17, 21, 0, tzinfo=UTC))
    assert label == "2026-10-17"
    # evening edition at 18:00 KST
    end, label = R.edition_window("pm", datetime(2026, 10, 18, 10, 0, tzinfo=UTC))
    assert label == "2026-10-18" and end == datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def _arts(spec):
    out = []
    for n, (outlet, group) in enumerate(spec):
        out.append(R.Art(n, outlet, f"t{n}", f"https://x/{n}", 0.0, group))
    return out


def _block_sim(blocks):
    """Similarity 0.9 inside each block, 0.05 between blocks."""
    n = sum(blocks)
    s = np.full((n, n), 0.05, dtype=np.float32)
    start = 0
    for b in blocks:
        s[start : start + b, start : start + b] = 0.9
        start += b
    return s


def test_two_events_sharing_one_word_stay_apart():
    # Event A: 6 outlets, nouns {흉기, 인천, 최충락}; event B: 4 outlets, {다이버, 양양, 수색}.
    # They share only 수색 (a generic word) via A's "압수수색".
    arts = _arts([(f"o{i}", "national") for i in range(10)])
    nouns = [{"흉기", "인천", "최충락", "수색"}] * 6 + [{"다이버", "양양", "수색", "해경"}] * 4
    stories = R.build_stories(arts, _block_sim([6, 4]), nouns)
    assert sorted(len(s.outlets) for s in stories) == [4, 6]


def test_angles_of_one_event_are_merged():
    # Two angles of one event share specific nouns (순직, 소방관, 부천) though their
    # headlines differ (low cross similarity).
    arts = _arts([(f"o{i}", "national") for i in range(9)])
    nouns = [{"순직", "소방관", "부천", "조문"}] * 5 + [{"순직", "소방관", "부천", "특진"}] * 4
    stories = R.build_stories(arts, _block_sim([5, 4]), nouns)
    assert len(stories) == 1 and len(stories[0].sub_events) == 2


def test_economic_outlets_count_half():
    arts = _arts(
        [("a", "national"), ("b", "national"), ("c", "economic"), ("d", "economic")]
        + [("e", "wire"), ("f", "economic")]
    )
    nouns = [{"하이드로타워", "출시"}] * 6
    (story,) = R.build_stories(arts, _block_sim([6]), nouns)
    assert story.score == 3 * 1.0 + 3 * 0.5


def test_select_drops_sports_and_small_stories():
    big = R.Story([[0]], outlets={f"o{i}" for i in range(9)}, score=9, keywords=["탄핵"])
    sport = R.Story([[1]], outlets={f"s{i}" for i in range(12)}, score=12, keywords=["홈런"])
    small = R.Story([[2]], outlets={"x", "y"}, score=2, keywords=["사고"])
    assert R.select([big, sport, small]) == [big]
