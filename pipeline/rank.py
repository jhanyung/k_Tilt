"""Pick the day's most-covered stories.

1. Window: headlines first seen or published in the 24 hours before the edition.
2. Drop photo/briefing items and normalise titles (bracket tags, common hanja shorthand).
3. Similarity = 0.5 x multilingual sentence embedding + 0.5 x character n-gram TF-IDF.
4. Level 1: average-linkage clustering into sub-events (one development of a story).
5. Level 2: merge sub-events into stories by headline similarity and shared key nouns.
6. Core merge: if two of the smaller story's core nouns (in >=40% of its headlines,
   generic news words excluded) run through the larger story (>=25% of its headlines),
   they are the same event and become one story. One shared word is never enough.
7. Score = distinct outlets, counting economic and specialist outlets as 0.5 because
   they often run the same corporate press release. The top 20 are stored as candidates;
   the top 10 are published. The rules favour never mixing two events, so one event can
   occasionally appear twice; the AI review pass (cards step) merges such duplicates.

Usage:
  python -m pipeline.rank --slot am|pm [--dry-run]          # reads/writes Supabase
  python -m pipeline.rank --from-dump items.json --dry-run  # local test on an ingest dump
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

LOCK = Path(__file__).resolve().parent / "data" / "feeds.lock.json"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
WINDOW_H = 24
TH_SUB = 0.45  # level-1 distance threshold
TH_STORY = 0.60  # level-2 distance threshold
W_EMB = 0.5  # weight of embeddings vs character n-grams
MIN_OUTLETS_SUB = 3  # sub-events below this are noise for story building
MIN_OUTLETS_STORY = 5
TOP_N = 10  # published per edition
TOP_CANDIDATES = 20  # stored for the AI review pass, which merges duplicates
GROUP_WEIGHT = {"economic": 0.5, "specialist": 0.5}

SKIP = re.compile(
    r"^\s*[\[【<]\s*(포토|사진|영상|부고|인사|게시판|오늘의 ?운세|날씨|운세|화보|모멘트|카드뉴스"
    r"|그래픽|TV|라이브|LIVE|만평|신간|알림|사고|정정|바로잡습니다|오늘의 날씨|헤드라인"
    r"|뉴스\s?종합)"
)
ROUNDUP = re.compile(
    r"오늘의?\s?뉴스|뉴스\s?종합|주요\s?뉴스|뉴스\s?브리핑|헤드라인|조간\s?브리핑|이 시각 주요"
    r"|\s外\s*$|\s외\s*$"
)
LEADING_TAGS = re.compile(r"^\s*(\[[^\]]{1,15}\]\s*|【[^】]{1,15}】\s*|<[^>]{1,15}>\s*)+")
PUNCT = re.compile(r"[\"'“”‘’「」『』<>…·,.!?()\[\]~]|&[a-z]+;")
HANJA = {
    "與": "여당 ",
    "野": "야당 ",
    "美": "미국 ",
    "中": "중국 ",
    "日": "일본 ",
    "北": "북한 ",
    "檢": "검찰 ",
    "警": "경찰 ",
    "靑": "청와대 ",
    "英": "영국 ",
    "獨": "독일 ",
    "佛": "프랑스 ",
    "露": "러시아 ",
    "前": "전 ",
    "現": "현 ",
}
# Generic news vocabulary: never enough on its own to say two stories are one event.
GENERIC = {
    "대통령",
    "정부",
    "사과",
    "발언",
    "대표",
    "의원",
    "경찰",
    "검찰",
    "여당",
    "야당",
    "국민",
    "논란",
    "추진",
    "결론",
    "오늘",
    "속보",
    "단독",
    "종합",
    "기자",
    "사건",
    "사고",
    "관련",
    "입장",
    "비판",
    "지적",
    "강조",
    "요구",
    "촉구",
    "논의",
    "회의",
    "공식",
    "직접",
    "최대",
    "최초",
    "이번",
    "지난",
    "올해",
    "내년",
    "정치",
    "사회",
    "경제",
    "국회",
    "민주",
    "국민의힘",
    "한국",
    "서울",
    "전국",
    "미국",
    "중국",
    "일본",
    "북한",
    "결과",
    "가능성",
    "전망",
    "우려",
    "대출",
    "금리",
    "인하",
    "인상",
    "투자",
    "시장",
    "기업",
    "은행",
    "가격",
    "상승",
    "하락",
    "개최",
    "진행",
    "선정",
    "출시",
    "체결",
    "협약",
    "지원",
    "확대",
    "강화",
    "추모",
    "기념",
    "징역",
    "선고",
    "구속",
    "체포",
    "수사",
    "혐의",
    "재판",
    "수색",
    "실종",
    "사망",
    "부상",
    "점검",
    "단속",
    "현장",
    "피해",
    "화재",
    "AI",
    "기술",
    "학술",
    "포럼",
    "행사",
    "대회",
    "지역",
    "시민",
    "주민",
}
CORE_SHARE = 0.4  # of the smaller story's headlines
PRESENT_SHARE = 0.25

# Stories about these carry little partisan framing to compare.
OFF_TOPIC = {
    "홈런", "타점", "투수", "타자", "득점", "프로야구", "KBO", "PO", "KS", "K리그", "EPL",
    "챔피언스리그", "골프", "LPGA", "PGA", "농구", "배구", "축구대표팀", "선발", "세이브",
    "아이돌", "컴백", "드라마", "예능", "음원", "뮤직비디오", "팬미팅", "열애", "시청률",
    "로또", "운세", "우승", "선두", "라운드", "KLPGA", "KPGA", "LIV", "준우승", "결승",
}  # fmt: skip


@dataclass
class Art:
    id: int
    outlet_id: str
    title: str
    url: str
    published: float
    group: str


@dataclass
class Story:
    sub_events: list[list[int]]  # indexes into the article list
    outlets: set[str] = field(default_factory=set)
    score: float = 0.0
    keywords: list[str] = field(default_factory=list)
    headline_idx: int = -1


def normalise(title: str) -> str:
    t = LEADING_TAGS.sub("", title)
    for k, v in HANJA.items():
        t = t.replace(k, v)
    t = PUNCT.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def keep(title: str) -> bool:
    return not SKIP.match(title) and not ROUNDUP.search(title) and len(normalise(title)) >= 8


def embed(titles: list[str]) -> np.ndarray:
    from fastembed import TextEmbedding

    model = TextEmbedding(MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH"))
    return np.array(list(model.embed(titles, batch_size=128)), dtype=np.float32)


def similarity(titles: list[str], emb: np.ndarray) -> np.ndarray:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import normalize

    e = normalize(emb)
    tf = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=2, sublinear_tf=True)
    x = normalize(tf.fit_transform(titles))
    return (W_EMB * (e @ e.T) + (1 - W_EMB) * (x @ x.T).toarray()).astype(np.float32)


def agglomerate(dist: np.ndarray, threshold: float) -> list[list[int]]:
    from sklearn.cluster import AgglomerativeClustering

    if len(dist) == 1:
        return [[0]]
    labels = (
        AgglomerativeClustering(
            n_clusters=None, metric="precomputed", linkage="average", distance_threshold=threshold
        )
        .fit(np.clip(dist, 0, 2))
        .labels_
    )
    groups: dict[int, list[int]] = defaultdict(list)
    for i, lab in enumerate(labels):
        groups[lab].append(i)
    return list(groups.values())


def nouns_of(titles: list[str]) -> list[set[str]]:
    from kiwipiepy import Kiwi

    kiwi = Kiwi()
    return [
        {
            x.form
            for x in kiwi.tokenize(t)
            if (x.tag in ("NNP", "NNG") and len(x.form) >= 2) or x.tag in ("SH", "SL")
        }
        for t in titles
    ]


def salient(members: list[int], nouns: list[set[str]], idf: dict[str, float]) -> dict[str, float]:
    """Nouns used by a good share of the members' headlines, weighted by rarity."""
    cnt: Counter[str] = Counter()
    for i in members:
        cnt.update(nouns[i])
    need = max(2, 0.3 * len(members)) if len(members) > 1 else 1
    return {n: idf[n] * c / len(members) for n, c in cnt.items() if c >= need}


def overlap(a: dict[str, float], b: dict[str, float]) -> float:
    inter = sum(min(a[n], b[n]) for n in a.keys() & b.keys())
    small = min(sum(a.values()), sum(b.values()))
    return inter / small if small else 0.0


def build_stories(arts: list[Art], sim: np.ndarray, nouns: list[set[str]]) -> list[Story]:
    n = len(arts)
    df: Counter[str] = Counter()
    for ns in nouns:
        df.update(ns)
    idf = {w: math.log(n / c) for w, c in df.items()}

    subs = agglomerate(1 - sim, TH_SUB)
    subs = [g for g in subs if len({arts[i].outlet_id for i in g}) >= MIN_OUTLETS_SUB]
    if not subs:
        return []
    kws = [salient(g, nouns, idf) for g in subs]

    m = len(subs)
    d2 = np.ones((m, m), dtype=np.float32)
    np.fill_diagonal(d2, 0)
    for a in range(m):
        for b in range(a + 1, m):
            # Two sub-events can only join if they share at least two specific nouns;
            # one shared word (수색, 대출, 점검) is how unrelated events get mixed.
            if len((kws[a].keys() & kws[b].keys()) - GENERIC) < 2:
                continue
            s = float(sim[np.ix_(subs[a], subs[b])].mean())
            d2[a, b] = d2[b, a] = 1 - (0.5 * s + 0.5 * overlap(kws[a], kws[b]))
    stories = [Story([subs[k] for k in grp]) for grp in agglomerate(d2, TH_STORY)]

    # Core merge: the smaller story's core nouns (in >=40% of its headlines, generic
    # words excluded) also run through the larger story (>=25% of its headlines).
    def shares(st: Story) -> tuple[int, Counter[str]]:
        members = [i for g in st.sub_events for i in g]
        cnt: Counter[str] = Counter()
        for i in members:
            cnt.update(nouns[i])
        return len(members), cnt

    def same_event(a: Story, b: Story) -> bool:
        (na, ca), (nb, cb) = shares(a), shares(b)
        if na > nb:
            (na, ca), (nb, cb) = (nb, cb), (na, ca)
        core_small = {w for w, c in ca.items() if c >= CORE_SHARE * na} - GENERIC
        present = {w for w in core_small if cb.get(w, 0) >= PRESENT_SHARE * nb}
        return len(present) >= 2

    merged = True
    while merged:
        merged = False
        for a in range(len(stories)):
            for b in range(a + 1, len(stories)):
                if same_event(stories[a], stories[b]):
                    stories[a].sub_events += stories[b].sub_events
                    del stories[b]
                    merged = True
                    break
            if merged:
                break

    for st in stories:
        members = [i for g in st.sub_events for i in g]
        st.outlets = {arts[i].outlet_id for i in members}
        st.score = sum(GROUP_WEIGHT.get(arts[i].group, 1.0) for i in _one_per_outlet(members, arts))
        sal = salient(members, nouns, idf)
        st.keywords = sorted(sal, key=lambda w: -sal[w])[:8]
        biggest = max(st.sub_events, key=len)
        st.headline_idx = max(biggest, key=lambda i: float(sim[i, biggest].mean()))
        st.sub_events.sort(key=len, reverse=True)
    return stories


def _one_per_outlet(members: list[int], arts: list[Art]) -> list[int]:
    seen: dict[str, int] = {}
    for i in members:
        seen.setdefault(arts[i].outlet_id, i)
    return list(seen.values())


def select(stories: list[Story], top_n: int = TOP_N) -> list[Story]:
    eligible = [
        s
        for s in stories
        if len(s.outlets) >= MIN_OUTLETS_STORY and not (set(s.keywords[:5]) & OFF_TOPIC)
    ]
    return sorted(eligible, key=lambda s: (-s.score, -len(s.outlets)))[:top_n]


def rank(arts: list[Art], top_n: int = TOP_N) -> tuple[list[Art], list[Story]]:
    arts = [a for a in arts if keep(a.title)]
    if len(arts) < 10:
        return arts, []
    titles = [normalise(a.title) for a in arts]
    sim = similarity(titles, embed(titles))
    stories = build_stories(arts, sim, nouns_of(titles))
    return arts, select(stories, top_n)


# ---------- I/O ----------


def edition_window(slot: str, now: datetime) -> tuple[datetime, str]:
    """The edition's cut-off time (07:00 or 18:00 KST) and its date label."""
    kst = now.astimezone(UTC) + timedelta(hours=9)
    hour = 7 if slot == "am" else 18
    cut = kst.replace(hour=hour, minute=0, second=0, microsecond=0)
    if cut > kst:
        cut -= timedelta(days=1)
    return (cut - timedelta(hours=9)).replace(tzinfo=UTC), cut.date().isoformat()


def load_from_db(conn, end: datetime) -> list[Art]:
    rows = conn.execute(
        "select a.id, a.outlet_id, a.title, a.url, "
        "extract(epoch from coalesce(a.published_at, a.first_seen_at)), o.grp "
        "from articles a join outlets o on o.id = a.outlet_id "
        "where coalesce(a.published_at, a.first_seen_at) > %s - interval '24 hours' "
        "and coalesce(a.published_at, a.first_seen_at) <= %s",
        (end, end),
    ).fetchall()
    return [Art(r[0], r[1], r[2], r[3], float(r[4]), r[5]) for r in rows]


def load_from_dump(path: str, end_ts: float) -> list[Art]:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))["outlets"]
    out = []
    for n, it in enumerate(json.loads(Path(path).read_text(encoding="utf-8"))):
        ts = it["published"] or end_ts
        if end_ts - WINDOW_H * 3600 < ts <= end_ts:
            grp = lock.get(it["outlet_id"], {}).get("group", "")
            out.append(Art(n, it["outlet_id"], it["title"], it["url"], ts, grp))
    return out


def save(conn, edition_date: str, slot: str, end: datetime, arts: list[Art], top: list[Story]):
    with conn.transaction():
        cur = conn.execute(
            "insert into editions (edition_date, slot, window_end, params) "
            "values (%s, %s, %s, %s) returning id",
            (
                edition_date,
                slot,
                end,
                json.dumps(
                    {"th_sub": TH_SUB, "th_story": TH_STORY, "w_emb": W_EMB, "model": MODEL}
                ),
            ),
        )
        edition_id = cur.fetchone()[0]
        for rank_no, st in enumerate(top, start=1):
            sid = conn.execute(
                "insert into stories (edition_id, rank, score, outlet_count, headline, keywords, "
                "status) values (%s, %s, %s, %s, %s, %s, %s) returning id",
                (
                    edition_id,
                    rank_no,
                    st.score,
                    len(st.outlets),
                    arts[st.headline_idx].title,
                    st.keywords,
                    "published" if rank_no <= TOP_N else "candidate",
                ),
            ).fetchone()[0]
            with conn.cursor() as cur2:
                cur2.executemany(
                    "insert into story_articles (story_id, article_id, sub_event) "
                    "values (%s, %s, %s) on conflict do nothing",
                    [(sid, arts[i].id, k) for k, g in enumerate(st.sub_events) for i in g],
                )
    return edition_id


def report(arts: list[Art], top: list[Story]) -> str:
    lines = []
    for n, st in enumerate(top, start=1):
        if n == TOP_N + 1:
            lines.append("    --- candidates below are stored but not published ---")
        lines.append(
            f"{n:>2}. [{st.score:.1f} / {len(st.outlets)} outlets / {len(st.sub_events)} angles] "
            f"{arts[st.headline_idx].title}"
        )
        lines.append(f"     keywords: {' '.join(st.keywords[:6])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["am", "pm"], default="am")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-dump", default="")
    ap.add_argument("--force", action="store_true", help="rebuild even if the edition exists")
    args = ap.parse_args(argv)

    t0 = time.time()
    if args.from_dump:
        arts = load_from_dump(args.from_dump, time.time())
        arts, top = rank(arts)
        print(report(arts, top))
        print(f"\n{len(arts)} headlines, {time.time() - t0:.0f}s")
        return 0

    from pipeline.db import connect

    end, edition_date = edition_window(args.slot, datetime.now(UTC))
    with connect() as conn:
        exists = conn.execute(
            "select id from editions where edition_date = %s and slot = %s",
            (edition_date, args.slot),
        ).fetchone()
        if exists and not args.force and not args.dry_run:
            print(f"edition {edition_date} {args.slot} already exists (id {exists[0]}); skipping")
            return 0
        arts, top = rank(load_from_db(conn, end), TOP_CANDIDATES)
        text = report(arts, top)
        print(text)
        if not args.dry_run and top:
            if exists:
                conn.execute("delete from editions where id = %s", (exists[0],))
            eid = save(conn, edition_date, args.slot, end, arts, top)
            print(f"\nsaved edition {eid}: {edition_date} {args.slot}")
        conn.commit()
    summary = f"### Edition {edition_date} {args.slot}\n\n```\n{text}\n```\n"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary)
    print(f"\n{len(arts)} headlines, {time.time() - t0:.0f}s")
    return 0 if top else 1


if __name__ == "__main__":
    sys.exit(main())
