"""Quote extraction on hand-written sentences in Korean news style."""

import pytest

from pipeline.analyse import quotes as Q

pytest.importorskip("kiwipiepy")


def run(text: str) -> list[tuple[str, str, str, str]]:
    return [(q.speaker.text, q.speaker.role, q.speaker.party, q.text) for q in Q.extract(text)]


def test_topic_speaker_party_and_title():
    out = run("박하늘 국민의힘 수석대변인은 18일 논평에서 “예산 심사가 너무 늦다”고 말했다.")
    assert out == [("국민의힘 박하늘 수석대변인", "정당", "국민의힘", "예산 심사가 너무 늦다")]


def test_short_form_resolves_to_earlier_full_name():
    out = run(
        "더불어민주당 김바다 원내대표가 기자간담회를 열었다.\n"
        "김 원내대표는 “법안을 이번 주에 처리하겠다”고 밝혔다."
    )
    assert out[0][:3] == ("더불어민주당 김바다 원내대표", "정당", "더불어민주당")


def test_pronoun_and_carry_follow_the_last_speaker():
    out = run(
        "최들판 국민의힘 의원은 “정부 해명이 부족하다”며 비판했다.\n"
        "그는 “자료를 다시 내야 한다”고 말했다.\n"
        "이어 “국정감사에서 따지겠다”고 덧붙였다."
    )
    assert [o[0] for o in out] == ["국민의힘 최들판 의원"] * 3


def test_topic_without_quote_sets_who_is_talked_about():
    out = run("편지를 공개한 송구름 의원은 빈소를 찾았다.\n그는 “마음이 무겁다”고 말했다.")
    assert out[0][0].endswith("송구름 의원")


def test_president_short_form_and_government_role():
    out = run("이 대통령은 이날 “안전 점검을 철저히 하라”고 지시했다.")
    assert out[0][:2] == ("이재명 대통령", "정부")


def test_ministry_as_speaker():
    out = run("외교부는 대변인 논평을 통해 “깊은 유감을 표한다”고 밝혔다.")
    assert out[0][:2] == ("외교부", "정부")


def test_foreign_names_and_titles():
    out = run("도널드 트럼프 미국 대통령은 “좋은 합의를 했다”고 말했다.")
    assert out[0][1] == "해외"
    assert "도널드 트럼프" in out[0][0]
    out = run("공화당 존 코닌 상원의원도 “이번 결정은 잘못됐다”고 비판했다.")
    assert out[0][:2] == ("공화당 존 코닌 상원의원", "해외")


def test_two_speakers_in_one_sentence():
    out = run(
        "가나다 상원의원은 “실수가 분명하다”라고 적었고, "
        "마바사 하원의원은 “전쟁을 연장하고 있다”고 말했다."
    )
    assert [o[0] for o in out] == ["가나다 상원의원", "마바사 하원의원"]


def test_embedded_subject_is_not_the_speaker():
    out = run(
        "정바람 의원이 재정 상황을 지적하자, 한별 지사가 이에 동조하며 "
        "“재정이 가장 큰 문제다”라고 답했다."
    )
    assert out[0][0].startswith("한별") or out[0][1] == "지자체"


def test_labels_in_quotes_are_not_speech():
    assert run("이른바 “재정 빨대” 발언이 논란을 빚었다.") == []


def test_citizens_and_anonymous_sources():
    assert run("초등학생 문가람 군은 “상어가 정말 컸다”고 말했다.")[0][1] == "시민"
    assert run("업계 관계자는 “수요가 줄고 있다”고 말했다.")[0][1] == "기업"
    out = run("한 여권 관계자는 “당분간 지켜볼 것”이라고 말했다.")
    assert out[0][1] == "정당"


def test_byline_is_removed():
    out = run("(서울=뉴스1) 홍길동 기자 = 이 대통령은 “고생 많았다”고 말했다.")
    assert out[0][0] == "이재명 대통령"


def test_quote_is_cut_to_one_sentence():
    out = run("김하나 교수는 “첫 문장입니다. 두 번째 문장입니다”라고 말했다.")
    assert out[0][3] == "첫 문장입니다."
    assert out[0][1] == "전문가"


def test_harmonise_fills_party_and_short_forms_across_articles():
    a = Q.extract("국민의힘 박하늘 수석대변인은 “사과하라”며 “진정성이 없다”고 말했다.")
    b = Q.extract("박 수석대변인은 “진정성이 없다”고 말했다.")
    qs = Q.harmonise(a + b)
    assert qs[-1].speaker.name == "박하늘"
