"""Direct quotes in an article: what was said, who said it, and where they stand.

Rules first, model later:
  1. A direct quote is text in double quotes followed by a speech ending
     ("...라고", "...며", "...고 말했다"). Quoted words used as labels ("...라는") are not.
  2. The speaker is the topic of the sentence before the quote
     ("정점식 국민의힘 원내대표는 ... “...”라고"), or failing that its subject, unless the
     subject belongs to an embedded clause ("민주당이 발의할 경우 “...”").
     Sentences with no subject ("이어 “...”") and pronouns ("그는") continue the
     previous speaker.
  3. Short forms ("정 원내대표", "李 대통령") resolve to the full name in the article.
  4. Role and party come from the words around the name (party, ministry, title, country),
     then from other mentions of the same name, then from the Assembly member list.
     What is still unknown is left as 미상 for the model fallback (cards step).
Only the speaker, their role and the first sentence of each quote leave this module.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parent.parent / "data"
MAX_QUOTE = 150
MIN_QUOTE = 6
CARRY_SENTENCES = 3  # how far a speaker carries into later sentences without a subject
MAX_NP_WORDS = 8

# ---------- vocabulary ----------

SPEECH_ENDINGS = (
    "이라면서", "이라고도", "라면서", "라고도", "이라고", "이라며", "면서도", "라고", "라며",
    "면서", "고도", "고", "며",
)  # fmt: skip
COUNTRIES = {
    "미국", "중국", "일본", "러시아", "우크라이나", "북한", "영국", "프랑스", "독일", "유럽",
    "EU", "유엔", "UN", "나토", "NATO", "이스라엘", "이란", "대만", "인도", "캐나다", "호주",
    "베트남", "필리핀", "튀르키예", "사우디", "백악관", "크렘린", "미", "美", "中", "日", "露",
    "北", "英", "유럽연합", "팔레스타인", "하마스", "헤즈볼라", "멕시코", "브라질", "몽골",
}  # fmt: skip
FOREIGN_TITLES = ("상원의원", "하원의원", "주석", "국왕", "주지사", "상원", "하원")
FOREIGN_PARTIES = {"공화당", "자민당", "노동당", "보수당", "입헌민주당", "공산당", "민주당원"}
COUNTRY_PREFIXES = (
    "미국", "중국", "일본", "러시아", "우크라이나", "북한", "영국", "프랑스", "독일", "유럽",
    "이스라엘", "이란", "대만", "백악관", "크렘린", "펜타곤", "미 ", "주한미군",
)  # fmt: skip
OUTLET_SUFFIX = ("일보", "신문", "방송", "통신", "뉴스", "타임스", "저널", "포스트", "TV")
FOREIGN_OUTLETS = {"NYT", "CNN", "로이터", "AP", "블룸버그", "AFP", "BBC", "WSJ", "폴리티코"}


def _is_outlet(w: str) -> bool:
    return w in FOREIGN_OUTLETS or (len(w) >= 3 and w.endswith(OUTLET_SUFFIX))


FOREIGN_LEADERS = {
    "트럼프", "푸틴", "시진핑", "젤렌스키", "다카이치", "이시바", "김정은", "김여정", "마크롱",
    "네타냐후", "모디", "밴스", "루비오", "헤그세스", "베선트", "라브로프", "왕이", "리창",
}  # fmt: skip
GOV_ORGS = {
    "정부", "대통령실", "청와대", "총리실", "국무조정실", "당국", "외교부", "국방부", "통일부",
    "법무부", "기획재정부", "기재부", "재정경제부", "행정안전부", "행안부", "교육부",
    "보건복지부", "복지부", "고용노동부", "노동부", "국토교통부", "국토부", "산업통상자원부",
    "산업부", "환경부", "기후에너지환경부", "과학기술정보통신부", "과기정통부", "여성가족부",
    "성평등가족부", "문화체육관광부", "문체부", "농림축산식품부", "농식품부", "해양수산부",
    "해수부", "중소벤처기업부", "중기부", "국가보훈부", "보훈부", "인사혁신처", "법제처",
    "소방청", "소방당국", "소방본부", "질병관리청", "기상청", "관세청", "국세청", "조달청",
    "통계청", "산림청", "해양경찰청", "해경", "금융위원회", "금융위", "금융감독원", "금감원",
    "공정거래위원회", "공정위", "방송통신위원회", "방통위", "국가정보원", "국정원", "감사원",
    "합동참모본부", "합참", "국군", "한국은행", "한은", "국가안보실", "기획예산처",
}  # fmt: skip
GOV_TITLES = {
    "대통령", "국무총리", "총리", "부총리", "장관", "차관", "청장", "처장", "비서실장",
    "정책실장", "안보실장", "국가안보실장", "수석비서관", "총재",
}  # fmt: skip
PARTY_TITLES = {
    "대표", "당대표", "원내대표", "최고위원", "수석대변인", "대변인", "원내대변인", "사무총장",
    "정책위의장", "비대위원장", "비상대책위원장", "의원", "국회의원", "위원장", "상임고문",
    "후보", "예비후보", "당협위원장", "원내수석부대표", "원내수석대변인", "대표대행",
    "국회의장", "간사", "중진", "지도부",
}  # fmt: skip
LOCAL_TITLES = {
    "지사", "도지사", "시장", "군수", "구청장", "교육감", "시의원", "도의원", "구의원",
    "부지사", "부시장",
}  # fmt: skip
EXPERT_WORDS = {
    "교수", "연구위원", "연구원", "박사", "변호사", "평론가", "애널리스트", "이코노미스트",
    "전문가", "소장", "센터장", "연구소", "석좌교수", "명예교수", "회계사", "의사", "학자",
}  # fmt: skip
CITIZEN_WORDS = {
    "씨", "군", "양", "시민", "주민", "유족", "유가족", "딸", "아들", "아내", "남편", "어머니",
    "아버지", "엄마", "아빠", "부모", "학부모", "상인", "환자", "피해자", "직장인", "소비자",
    "누리꾼", "네티즌", "방문객", "관광객", "학생", "동료", "이웃", "목격자", "가족", "부인",
    "할머니", "할아버지", "조합원", "근로자", "노동자", "농민", "어민", "자영업자", "참석자",
    "관람객", "승객", "운전자", "입주민", "세입자", "청년", "참가자", "봉사자", "생존자",
    "장인", "장모", "동생", "형", "누나", "언니", "오빠", "삼촌", "친구", "지인", "손녀",
    "손자", "부친", "모친", "대원", "소방관", "소방대원", "교사", "간호사", "주부", "처형",
    "사위", "매형", "시어머니", "시아버지", "조카", "유치원생", "초등학생", "중학생", "고교생",
}  # fmt: skip
GROUP_WORDS = {
    "노조", "노동조합", "협회", "연합회", "단체", "시민단체", "참여연대", "경실련", "민주노총",
    "한국노총", "상임대표", "공동대표", "연대", "총연합회", "협의회", "연맹", "비대위",
    "대책위원회", "대책위", "재단", "운동본부", "유족회",
}  # fmt: skip
BUSINESS_TITLES = {
    "회장", "사장", "대표이사", "CEO", "부사장", "전무", "상무", "임원", "부회장", "은행장",
    "경영자", "최고경영자",
}  # fmt: skip
BUSINESS_WORDS = BUSINESS_TITLES | {"업계", "회사", "그룹", "기업", "사측", "증권", "전자", "재계"}
JUSTICE_WORDS = {
    "검찰", "검사", "경찰", "법원", "재판부", "판사", "헌법재판소", "헌재", "대법원", "대법원장",
    "특검", "특별검사", "수사팀", "공수처", "경찰청", "검찰청", "수사본부", "검찰총장",
    "경찰청장", "변호인", "변호인단", "대법관", "경찰서", "지검", "지법",
}  # fmt: skip
MEDIA_WORDS = {"기자", "앵커", "특파원", "논설위원", "언론", "외신"}
ANON_WORDS = {"관계자", "관계자들", "측", "핵심", "인사", "고위급", "당국자", "소식통", "인사들"}
PRONOUNS = {"그", "그녀", "그들", "이들", "본인", "자신", "당사자"}
NON_SPEAKER = {
    "오늘", "이날", "올해", "지난해", "이번", "당시", "현재", "최근", "앞서", "이후", "결과",
    "문제", "사건", "사고", "발언", "내용", "입장", "의견", "이유", "점", "것", "데", "수",
    "때", "글", "편지", "논평", "성명", "회견", "기자회견", "보고서", "자료", "설명", "말",
    "주장", "비판", "지적", "취지", "분위기", "상황", "결정", "조치", "방침", "계획", "제목",
    "질문", "답변", "메시지", "이야기", "목소리", "여론", "반응", "평가", "전망", "분석",
    "사과", "논란", "가운데", "한편", "특히", "다만", "또", "이어", "그러면서", "아울러",
}  # fmt: skip
LEADING_JUNK = re.compile(
    r"^(\d+[일시분]?|\d+일[께쯤]?|오늘|이날|어제|지난|앞서|최근|또|이어|특히|한편|다만|이후"
    r"|당시|현재|역시|또한|반면|그러나|하지만|결국|이에|이와|관련|그러자|전날|이튿날|당일|때)$"
)
STOP_WORDS = {"소속", "출신", "현직", "당시", "현", "측", "같은", "또", "다른", "한", "또다른"}
ORG_SUFFIX = (
    "당", "부", "청", "처", "위원회", "의회", "대학교", "대학", "연구소", "협회", "노조", "그룹",
    "은행", "펀드", "재단", "본부", "센터", "공사", "공단", "시청", "도청", "구청", "법원",
    "검찰청", "경찰청", "경찰서", "학교", "병원",
)  # fmt: skip
CLAUSE_NOUNS = {
    "데", "경우", "때", "것", "뒤", "후", "만큼", "직후", "이후", "도중", "과정", "가운데",
    "상황", "바", "다음", "이상",
}  # fmt: skip
CLAUSE_EC = {"면", "으면", "자", "지만", "는데", "은데", "ᆫ데", "어도", "아도", "더니", "다가"}
RANKS = {"소방위", "소방장", "소방교", "소방사", "소방경", "소방령", "경위", "경감", "경사", "순경"}
HANJA_SURNAME = {"李": "이", "尹": "윤", "文": "문", "朴": "박", "金": "김", "韓": "한", "安": "안"}
SURNAMES = set(
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민진"
    "지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉경"
)
KOREAN_NAME = re.compile(r"^[가-힣]{2,4}$")
# Words that only match whole: "위원장" must not count as "원장", "가족회" not as "회".
WHOLE_ONLY = {"원장", "장", "군", "양", "씨", "측", "딸", "형", "인사", "핵심", "대원"}
NOMINAL = {
    "NNG", "NNP", "NNB", "NR", "NP", "SN", "SL", "SH", "XSN", "XPN", "SO", "SW", "SSO", "SSC",
}  # fmt: skip


@dataclass
class Speaker:
    text: str  # as written, after resolving short forms
    name: str = ""
    title: str = ""
    org: str = ""
    role: str = "미상"  # 정부 정당 지자체 전문가 시민 단체 기업 사법·수사 언론 해외 기타 미상
    party: str = ""
    bloc: str = ""  # ruling | opposition | ""
    initial: str = ""  # surname of an unresolved short form ("정" in "정 원내대표")


@dataclass
class Quote:
    text: str
    speaker: Speaker
    method: str = "rule"
    flags: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        row = asdict(self.speaker)
        row.update(quote=self.text, method=self.method, flags=self.flags)
        return row


# ---------- config ----------


@lru_cache(maxsize=1)
def politics() -> dict:
    cfg = yaml.safe_load((DATA / "politics.yaml").read_text(encoding="utf-8"))
    names: dict[str, str] = {}
    for party, spec in cfg["parties"].items():
        names[party] = party
        for a in spec.get("aliases", []):
            names[a] = party
    cfg["party_names"] = names
    return cfg


@lru_cache(maxsize=1)
def members() -> dict[str, str]:
    """Assembly member name -> party. Names shared by two members are left out."""
    path = DATA / "assembly_members.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8")).get("members", [])
    seen: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        seen[r["name"]].add(r.get("party", ""))
    return {n: next(iter(p)) for n, p in seen.items() if len(p) == 1}


@lru_cache(maxsize=1)
def _kiwi():
    from kiwipiepy import Kiwi

    kiwi = Kiwi()
    # Keep party, ministry and member names whole ("국민의힘" must not become 국민/의/힘).
    words = [*politics()["party_names"], *politics()["camp_words"], *GOV_ORGS, *members()]
    for word in words:
        if len(word) >= 2 and re.fullmatch(r"[가-힣]+", word):
            kiwi.add_user_word(word, "NNP", 0.0)
    return kiwi


# ---------- words ----------


@dataclass
class Word:
    """One space-separated word (eojeol) with its morphemes."""

    text: str  # the word without trailing particles
    start: int
    end: int
    forms: list[str]
    tags: list[str]
    particle: str  # trailing particle(s), e.g. "은", "의", ""
    ptag: str  # tag of the last particle
    nominal: bool  # stem is all nominal morphemes
    joined: bool = True  # separated from the previous word by exactly one space


def words_of(text: str) -> list[Word]:
    toks = _kiwi().tokenize(text)
    out = []
    ti = 0
    prev_end = 0
    for m in re.finditer(r"\S+", text):
        a, b = m.start(), m.end()
        while ti < len(toks) and toks[ti].start < a:
            ti += 1
        mine = []
        k = ti
        while k < len(toks) and toks[k].start < b:
            mine.append(toks[k])
            k += 1
        ti = k
        if not mine:
            continue
        # Split off trailing particles and closing punctuation.
        cut = len(mine)
        while cut > 0 and (mine[cut - 1].tag.startswith("J") or mine[cut - 1].tag in ("SF", "SP")):
            cut -= 1
        stem_toks = mine[:cut]
        stem_end = stem_toks[-1].start + stem_toks[-1].len if stem_toks else a
        parts = [t for t in mine[cut:] if t.tag.startswith("J")]
        out.append(
            Word(
                text=re.sub(r"\(.*?\)|（.*?）", "", text[a:stem_end]).strip("“”\"‘’'[]()·,"),
                start=a,
                end=b,
                forms=[t.form for t in mine],
                tags=[t.tag for t in mine],
                particle="".join(t.form for t in parts),
                ptag=parts[-1].tag if parts else "",
                nominal=bool(stem_toks) and all(t.tag in NOMINAL for t in stem_toks),
                joined=text[prev_end:a] == " ",
            )
        )
        prev_end = b
    return out


def _orgish(w: str) -> bool:
    cfg = politics()
    return (
        w in cfg["party_names"]
        or w in cfg["camp_words"]
        or w in COUNTRIES
        or w in GOV_ORGS
        or w in RANKS
        or w in JUSTICE_WORDS
        or w in ("국회", "정치권", "당", "업계", "재계", "정계", "법조계", "학계")
        or (len(w) >= 3 and w.endswith(ORG_SUFFIX))
    )


def _vocab_hit(word: str, vocab: set[str]) -> bool:
    if word in vocab:
        return True
    return any(word.endswith(v) for v in vocab if v not in WHOLE_ONLY and len(v) >= 2)


TITLE_VOCAB = (
    GOV_TITLES | PARTY_TITLES | LOCAL_TITLES | EXPERT_WORDS | CITIZEN_WORDS | BUSINESS_TITLES
    | ANON_WORDS | {"기자", "앵커", "특파원", "논설위원", "상원의원", "하원의원", "대사", "주석",
    "총장", "국왕", "왕세자", "원장", "상임대표", "공동대표", "특사", "대행"}
)  # fmt: skip


def _is_title(w: str) -> bool:
    return _vocab_hit(w, TITLE_VOCAB) and w not in COUNTRIES


def _korean_name(w: str) -> bool:
    return bool(KOREAN_NAME.match(w)) and w[0] in SURNAMES and not _orgish(w) and not _is_title(w)


def parse_phrase(ws: list[Word]) -> tuple[str, str, list[str]]:
    """(name, title, organisation words) of a speaker phrase given as words."""
    texts = [HANJA_SURNAME.get(w.text, w.text) for w in ws]
    texts = [re.sub(r"\(.*?\)|（.*?）", "", t).strip("()") for t in texts]
    keep = [i for i, t in enumerate(texts) if t and not LEADING_JUNK.match(t)]
    ws = [ws[i] for i in keep]
    texts = [texts[i] for i in keep]
    if not texts:
        return "", "", []
    # Trailing titles, at most two words: "원내대표", "국부펀드 대표".
    t_start = len(texts)
    while t_start > 0 and _is_title(texts[t_start - 1]) and len(texts) - t_start < 2:
        t_start -= 1
    title = "".join(texts[t_start:])
    head, head_ws = texts[:t_start], ws[:t_start]
    if not title:
        return "", "", [t for t in head if t not in STOP_WORDS]
    # Name: a run of two or more proper nouns is a foreign name ("도널드 트럼프");
    # otherwise the last Korean name ("국민의힘 정점식"), otherwise a single proper noun.
    run: list[int] = []
    for i in range(len(head) - 1, -1, -1):
        t, w = head[i], head_ws[i]
        stem_tags = [x for x in w.tags if not x.startswith("J")]
        proper = bool(stem_tags) and set(stem_tags) <= {"NNP", "SL"}
        # Foreign given names can be one syllable ("존 코닌", "척 그래슬리").
        if not _orgish(t) and t not in STOP_WORDS and proper and (len(t) >= 2 or run):
            run.insert(0, i)
        elif run:
            break
    korean = [i for i, t in enumerate(head) if _korean_name(t)]
    if len(run) >= 2 and not all(_korean_name(head[i]) for i in run):
        pick = run
    elif korean:
        pick = [korean[-1]]
    else:
        pick = run
    if not pick:
        return "", title, [t for t in head if t not in STOP_WORDS]
    name = " ".join(head[i] for i in pick)
    return name, title, [t for k, t in enumerate(head) if k not in pick and t not in STOP_WORDS]


# ---------- sentence and quote scanning ----------

_QUOTE_PAIRS = {"“": "”", '"': '"'}


def quote_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of double-quoted spans, end exclusive and including the marks."""
    spans = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in _QUOTE_PAIRS:
            close = _QUOTE_PAIRS[ch]
            j = text.find(close, i + 1)
            nl = text.find("\n", i + 1)
            if j == -1 or (nl != -1 and nl < j) or j - i > 800:
                i += 1
                continue
            spans.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return spans


def speech_ending(text: str, end: int) -> str:
    """The speech ending right after a closing quote, or "" if the quote is not speech."""
    tail = text[end : end + 6]
    for e in SPEECH_ENDINGS:
        if tail.startswith(e):
            nxt = tail[len(e) : len(e) + 1]
            if nxt in ("", " ", "\n", ",", ".", "“", '"'):
                return e
    return ""


def sentences(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sentence boundaries that never fall inside a quote."""
    inside = bytearray(len(text) + 1)
    for s, e in spans:
        inside[s:e] = b"\x01" * (e - s)
    out, start = [], 0
    for m in re.finditer(r"(?<=[다요죠까])[.?!](?=\s|$|[가-힣])|\n", text):
        if inside[m.start()]:
            continue
        out.append((start, m.end()))
        start = m.end()
    if start < len(text):
        out.append((start, len(text)))
    return out


def first_sentence(quote: str) -> str:
    q = quote.strip()
    m = re.search(r"(?<=[다요죠까니])[.?!](?=\s)", q)
    if m:
        q = q[: m.end()]
    if len(q) > MAX_QUOTE:
        q = q[: MAX_QUOTE - 1].rstrip() + "…"
    return q


# ---------- finding the speaker phrase ----------

TOPIC = {"은", "는", "도", "은요", "는요"}
SUBJECT = {"이", "가", "께서"}


@dataclass
class Phrase:
    words: list[Word]
    kind: str  # topic | subject
    index: int  # index of the head word in the sentence's word list

    @property
    def start(self) -> int:
        return self.words[0].start


def _phrase_at(ws: list[Word], k: int) -> list[Word]:
    """Head word k plus the bare nouns (and X의 possessors) right before it."""
    j = k
    while j - 1 >= 0 and k - j < MAX_NP_WORDS:
        p = ws[j - 1]
        if p.nominal and p.particle in ("", "의") and ws[j].joined and p.text not in CLAUSE_NOUNS:
            j -= 1
        elif p.text in CLAUSE_NOUNS:  # "빈소를 찾았을 때 고인의 장인은"
            break
        elif p.text in ("같은", "한", "또", "다른", "또다른"):
            j -= 1
            break
        else:
            break
    return ws[j : k + 1]


def _speakerish(ph: list[Word]) -> bool:
    head = ph[-1].text
    if not head or head in NON_SPEAKER or LEADING_JUNK.match(head):
        return False
    if head in PRONOUNS or _is_title(head) or _orgish(head) or _is_outlet(head):
        return True
    if re.fullmatch(r"[가-힣]{1,4}[씨군양]", head) and ph[-1].tags[:1] == ["NNP"]:
        return True  # "최씨는", "김모군은"
    # A proper noun at the head ("정점식은", "외교부는"), not just somewhere in the phrase
    # ("김용범 정책실장 등 증인 채택이" is about a decision, not a speaker).
    stem = [t for t in ph[-1].tags if not t.startswith("J")]
    return bool(stem) and stem[-1] in ("NNP", "SH", "SL")


def _embedded(ws: list[Word], k: int) -> bool:
    """True if the subject at k belongs to a clause that ends before the quote:
    "민주당이 발의할 경우", "추 지사가 사과하자"."""
    for n in range(k + 1, len(ws)):
        w = ws[n]
        if w.ptag == "JKS" or w.particle in TOPIC:
            return False  # another subject starts; whatever follows is its clause
        for f, t in zip(w.forms, w.tags, strict=True):
            if t == "EC" and f in CLAUSE_EC:
                return True
            if t == "ETM" and n + 1 < len(ws) and ws[n + 1].text in CLAUSE_NOUNS:
                return True
    return False


def pick_speaker(ws: list[Word]) -> Phrase | None:
    cands: list[Phrase] = []
    for k, w in enumerate(ws):
        topic = w.nominal and w.particle in TOPIC
        subj = w.nominal and w.ptag == "JKS" and w.particle in SUBJECT
        # "마이클 매콜 하원의원 역시 “...”": 역시/또한 mark the topic.
        nxt = ws[k + 1] if k + 1 < len(ws) else None
        if w.nominal and not w.particle and nxt is not None and nxt.text in ("역시", "또한"):
            topic = True
        if not (topic or subj):
            continue
        ph = _phrase_at(ws, k)
        if _speakerish(ph):
            cands.append(Phrase(ph, "topic" if topic else "subject", k))
    topics = [c for c in cands if c.kind == "topic"]
    if topics:
        return topics[-1]
    # The last subject whose clause runs on to the quote: in "권 의원이 지적하자 추 지사가
    # 동조하며 “...”" the speaker is 추 지사.
    subjects = [c for c in cands if not _embedded(ws, c.index)]
    return subjects[-1] if subjects else None


def pick_topic(ws: list[Word]) -> Phrase | None:
    """The first speaker-like topic of a sentence without quotes, to follow who the
    article is talking about ("송 의원은 ... 전했다. 그는 “...”")."""
    for k, w in enumerate(ws):
        if w.nominal and w.particle in ("은", "는", "도"):
            ph = _phrase_at(ws, k)
            if _speakerish(ph):
                return Phrase(ph, "topic", k)
    return None


# ---------- people mentioned in the article ----------


@dataclass
class Mention:
    start: int
    name: str
    title: str
    org: list[str]
    party: str = ""  # only when the party name stands right next to the person's name


def mentions(ws: list[Word]) -> list[Mention]:
    """Full names with a title anywhere in the article: '국민의힘 정점식 원내대표'."""
    out = []
    run: list[Word] = []

    def flush() -> None:
        for cut in range(len(run)):  # try suffixes: "발언 논란 속 조문 이재명 대통령"
            name, title, org = parse_phrase(run[cut:])
            if name and title:
                texts = [w.text for w in run[cut:]]
                near = []
                if name in texts:
                    i = texts.index(name)
                    near = texts[max(0, i - 1) : i] + texts[i + 1 : i + 2]
                party = _party_of(near)[0]
                out.append(Mention(run[cut].start, name, title, org, party))
                return

    for w in ws:
        if w.nominal and (not run or w.joined):
            run.append(w)
            if w.particle:
                flush()
                run = []
        else:
            if run:
                flush()
            run = [w] if w.nominal and not w.particle else []
    if run:
        flush()
    return out


# ---------- roles ----------


def _bloc(party: str) -> str:
    spec = politics()["parties"].get(party)
    return spec["bloc"] if spec else ""


def _party_of(words: list[str]) -> tuple[str, str]:
    cfg = politics()
    for w in words:
        for alias, party in cfg["party_names"].items():
            if w == alias or (len(alias) >= 3 and w.startswith(alias)):
                return party, _bloc(party)
    for w in words:
        if w in cfg["camp_words"]:
            return "", cfg["camp_words"][w]
    return "", ""


def classify(org: list[str], name: str, title: str) -> tuple[str, str, str]:
    """(role, party, bloc)."""
    cfg = politics()
    words = [*org, title]
    party, bloc = _party_of(words)
    if not party and name in members():
        party = members()[name]
        bloc = _bloc(party)

    def hit(vocab: set[str]) -> bool:
        return any(_vocab_hit(w, vocab) for w in words if w)

    foreign_title = title.endswith(FOREIGN_TITLES) or any(
        w in FOREIGN_PARTIES or w in ("상원", "하원") for w in org
    )
    foreign = (
        foreign_title
        or any(w in COUNTRIES or w.startswith(COUNTRY_PREFIXES) for w in words)
        or name.split(" ")[-1] in FOREIGN_LEADERS
        or " " in name
    )
    if foreign_title or (foreign and not party):
        return "해외", "", ""
    if "전" in org and not party:  # former officials
        return "기타", "", ""
    if (hit(MEDIA_WORDS) or any(_is_outlet(w) for w in words)) and not party:
        return "언론", "", ""
    if hit(JUSTICE_WORDS) and not party:
        return "사법·수사", "", ""
    gov = any(w in GOV_ORGS for w in org) or hit(GOV_TITLES) or title in GOV_ORGS
    if gov and not (party and not _vocab_hit(title, GOV_TITLES)):
        if title.endswith("대통령") and name and name != cfg["president"]["name"]:
            return "기타", "", ""
        return "정부", "", "ruling"
    if hit(LOCAL_TITLES) or any(w.endswith(("도의회", "시의회", "구의회", "군의회")) for w in org):
        return "지자체", party, bloc
    if party or bloc or hit(PARTY_TITLES):
        return "정당", party, bloc
    if hit(EXPERT_WORDS):
        return "전문가", "", ""
    if hit(GROUP_WORDS):
        return "단체", "", ""
    if hit(BUSINESS_WORDS):
        return "기업", "", ""
    if hit(CITIZEN_WORDS) or re.fullmatch(r"[가-힣]{1,4}[씨군양]|[가-힣]{2,4}모[씨군양]?", title):
        return "시민", "", ""
    return "미상", "", ""


# ---------- resolving a phrase to a speaker ----------


def _resolve(ph: Phrase, ms: list[Mention], last: Speaker | None) -> Speaker | None:
    ws = [w for w in ph.words if not LEADING_JUNK.match(w.text)]
    texts = [HANJA_SURNAME.get(w.text, w.text) for w in ws]
    inherited = ""
    if texts and texts[0] == "같은":  # "같은 당 존 코닌 상원의원"
        inherited = last.party if last else ""
        ws, texts = ws[1:], texts[1:]
        if texts and texts[0] == "당":
            ws, texts = ws[1:], texts[1:]
    if texts and texts[0] in ("한", "또", "다른", "또다른"):
        ws, texts = ws[1:], texts[1:]
    if not ws:
        return None
    if texts[0] in PRONOUNS:
        return last
    name, title, org = parse_phrase(ws)
    initial = ""
    if not name:
        head = texts[0]
        rest = "".join(texts[1:])
        if len(head) == 1 and head in SURNAMES and rest:
            # Short form: "정 원내대표", "이 대통령". Prefer the latest earlier mention.
            cands = [m for m in ms if m.name[0] == head and m.title.endswith(rest)]
            before = [m for m in cands if m.start < ph.start]
            pres = politics()["president"]["name"]
            if before or cands:
                m = (before or cands)[-1]
                name, title, org = m.name, m.title, list(m.org)
            elif rest == "대통령" and head == pres[0]:
                name, title, org = pres, "대통령", []
            elif head == "이" and "MM" in ws[0].tags:
                return last  # "이 관계자는": this person, already introduced
            else:
                title, org = texts[-1], texts[1:-1]
                initial = head
        elif not title:
            title, org = texts[-1], texts[:-1]
    # Pick up a party from other mentions of the same person in the article, but only
    # where the party name stands right next to the name ("국민의힘 정점식 원내대표").
    if name and not _party_of(org)[0]:
        for m in ms:
            if m.name == name and m.party:
                org = [*org, m.party]
                break
    role, party, bloc = classify(org, name, title)
    if inherited and not party and role != "해외":
        party, bloc = inherited, _bloc(inherited)
        if role == "미상":
            role = "정당"
    if initial:
        shown = " ".join(texts)
    else:
        shown = " ".join(x for x in (" ".join(org), name, title) if x)
    return Speaker(shown, name, title, " ".join(org), role, party, bloc, initial)


# ---------- main ----------


BYLINE = re.compile(
    r"[\[(][^\])\n]{1,20}=[^\])\n]{1,20}[\])]\s*(?:[가-힣]{2,4}\s*){0,6}(?:기자|특파원)?\s*=?\s*"
    r"|(?:^|\n)[^\n=]{0,40}(?:기자|특파원)\s*=\s*"
    r"|\[[^\]\n]{0,30}(?:기자|특파원)\]\s*"
)


def clean(text: str) -> str:
    """Drop bylines ("(서울=뉴스1) 홍길동 기자 =") and put a space after sentence ends
    that run into the next sentence ("촉구했다.국민의힘")."""
    text = BYLINE.sub(lambda m: "\n" if m.group(0).startswith("\n") else "", text)
    return re.sub(r"(?<=[다요])\.(?=[가-힣“\"])", ". ", text)


def extract(text: str) -> list[Quote]:
    text = clean(text)
    spans = quote_spans(text)
    if not spans:
        return []
    all_words = words_of(text)
    ms = mentions(all_words)
    out: list[Quote] = []
    last: Speaker | None = None
    since_last = 99
    for s0, s1 in sentences(text, spans):
        since_last += 1
        speech = [(a, b) for a, b in spans if s0 <= a < s1 and speech_ending(text, b)]
        if not speech:
            topic = pick_topic([w for w in all_words if s0 <= w.start and w.end <= s1])
            if topic is not None:
                sp = _resolve(topic, ms, last)
                if sp is not None and (sp.name or sp.role != "미상"):
                    last, since_last = sp, 0
            continue
        # Each quote takes the speaker named between it and the previous quote, if any:
        # "A 의원은 “...”라고 적었고, B 의원은 “...”".
        seg_start = s0
        sp: Speaker | None = None
        flags: list[str] = []
        for n, (a, b) in enumerate(speech):
            inside = [(x, y) for x, y in spans if seg_start <= x < a]
            seg = [
                w
                for w in all_words
                if seg_start <= w.start
                and w.end <= a
                and not any(x <= w.start < y for x, y in inside)
            ]
            seg_start = b
            ph = pick_speaker(seg)
            if ph is not None:
                found = _resolve(ph, ms, sp or last)
                if found is not None:
                    sp, flags = found, []
            elif n == 0 and since_last <= CARRY_SENTENCES:
                sp, flags = last, ["carried"]
            if sp is None:
                continue
            body = text[a + 1 : b - 1]
            if len(body.strip()) >= MIN_QUOTE:
                out.append(Quote(first_sentence(body), sp, flags=list(flags)))
        if sp is not None:
            last, since_last = sp, 0
    return out


def harmonise(quotes: list[Quote]) -> list[Quote]:
    """Across a story's articles: fill in short forms ("추 지사") and missing parties from
    how other outlets name the same person, when they clearly agree."""
    full: dict[tuple[str, str], Counter] = defaultdict(Counter)
    parties: dict[str, Counter] = defaultdict(Counter)
    for q in quotes:
        sp = q.speaker
        if sp.name:
            full[(sp.name[0], sp.title)][sp.name] += 1
            if sp.party:
                parties[sp.name][sp.party] += 1
    for q in quotes:
        sp = q.speaker
        if not sp.name and sp.initial and sp.title:
            names: Counter = Counter()
            for (initial, t), c in full.items():
                if initial == sp.initial and t.endswith(sp.title):
                    names.update(c)
            if names:
                sp.name = names.most_common(1)[0][0]
                sp.text = " ".join(x for x in (sp.org, sp.name, sp.title) if x)
                q.flags.append("name-from-story")
        if sp.name and not sp.party and parties.get(sp.name):
            party, n = parties[sp.name].most_common(1)[0]
            if n >= 2 and n / sum(parties[sp.name].values()) >= 0.75:
                sp.party, sp.bloc = party, _bloc(party)
                if sp.role in ("미상", "정당"):
                    sp.role = "정당"
                q.flags.append("party-from-story")
    return quotes
