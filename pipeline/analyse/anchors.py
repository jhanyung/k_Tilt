"""Measure each article against what the government and the parties said about the same
story, in their own words.

For every published story:
  1. Match the day's official statements to the story (청와대·부처 briefings and the
     government's rebuttals; each party's spokesperson statements), by headline wording.
  2. Find the words each group uses distinctively about this story: weighted log-odds
     with an informative prior (Monroe, Colaresi & Quinn 2008), the story's own articles
     serving as the background, so words every report uses carry no signal.
  3. Score each article by how often it uses each group's words (per 1,000 words),
     then standardise across the story's articles.

Groups are scored separately (정부, and each party), so a story where only one side
spoke still counts. Rates are standardised within each story; an outlet's standing
comes from averaging over many stories.
Text is read in memory and dropped; only word counts and scores leave this module.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

import lxml.html

from pipeline.analyse import quotes as Q

MIN_HITS = 3
TOP_TERMS = 30
Z_MIN = 1.96
MATCH_MIN = 0.12  # statement-to-story headline similarity needed for a match
MATCH_MARGIN = 1.5  # and the best story must beat the second best by this factor
NOUN_TAGS = {"NNG", "NNP"}
# Words that say who is speaking rather than how the story is framed.
STOP = {
    "대변인", "논평", "브리핑", "서면", "수석", "수석대변인", "원내대변인", "일시", "장소",
    "소통관", "기자회견장", "국회", "오전", "오후", "오늘", "이날", "말씀", "생각", "대표",
    "의원", "정부", "대통령", "더불어민주당", "민주당", "국민의힘", "국힘", "여당", "야당",
    "입장", "관련", "문제", "사안", "부분", "경우", "때문", "우리", "국민", "보도", "기사",
    "자료", "설명", "내용", "사실", "첨부", "파일", "참고", "이재명", "청와대", "강유정",
}  # fmt: skip


@dataclass
class Anchor:
    """A matched official statement and the words of its text."""

    statement_id: int
    source: str
    side: str  # government | party
    party: str
    title: str
    similarity: float
    words: Counter = field(default_factory=Counter)


# ---------- words ----------


def words(text: str) -> Counter:
    """Nouns and adjacent-noun pairs, without speaker and boilerplate words."""
    toks = Q._kiwi().tokenize(text)
    out: Counter = Counter()
    prev = None
    for t in toks:
        if t.tag in NOUN_TAGS and len(t.form) >= 2 and t.form not in STOP:
            out[t.form] += 1
            if prev is not None and prev.start + prev.len >= t.start - 1:
                out[f"{prev.form} {t.form}"] += 1
            prev = t
        else:
            prev = None if t.tag not in NOUN_TAGS else t
    return out


_QUOTED = re.compile(r"“[^”\n]{0,800}”|\"[^\"\n]{0,800}\"|‘[^’\n]{0,300}’|'[^'\n]{0,300}'")


def narrative(text: str) -> str:
    """The reporter's own words: the article with quoted speech and quoted phrases removed,
    so an outlet is not scored for what it quotes (that is counted in quotes.py)."""
    return _QUOTED.sub(" ", Q.clean(text))


def statement_text(html: str, body_class: str) -> str:
    try:
        doc = lxml.html.fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return ""
    nodes = doc.xpath(f"//*[contains(concat(' ', normalize-space(@class), ' '), ' {body_class} ')]")
    if not nodes:
        return ""
    node = max(nodes, key=lambda n: len(n.text_content()))
    for bad in node.xpath(".//script | .//style"):
        bad.drop_tree()
    return re.sub(r"[ \t\xa0]+", " ", node.text_content()).strip()


# ---------- matching statements to stories ----------


def _grams(text: str) -> Counter:
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", text)
    return Counter(t[i : i + n] for n in (2, 3) for i in range(len(t) - n + 1))


def _cos(a: Counter, b: Counter, idf: dict[str, float]) -> float:
    dot = sum(v * b[k] * idf.get(k, 1.0) ** 2 for k, v in a.items() if k in b)
    na = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in a.items()))
    nb = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in b.items()))
    return dot / (na * nb) if na and nb else 0.0


def match(stories: dict[int, list[str]], statements: list[tuple[int, str]]) -> dict:
    """{story_id: [(statement_id, similarity)]}. Each statement goes to its best story
    only, and only when its headline is close enough to that story's headlines."""
    docs = [_grams(" ".join(ts)) for ts in stories.values()] + [_grams(t) for _, t in statements]
    df: Counter = Counter()
    for d in docs:
        df.update(d.keys())
    n = len(docs)
    idf = {k: math.log((1 + n) / (1 + v)) + 1 for k, v in df.items()}
    # A story is represented by its headlines' grams, each headline counted once.
    vecs = {}
    for sid, titles in stories.items():
        v: Counter = Counter()
        for t in titles:
            v.update(set(_grams(t)))
        vecs[sid] = v
    out: dict[int, list[tuple[int, float]]] = {sid: [] for sid in stories}
    for st_id, title in statements:
        g = _grams(title)
        scored = sorted(((_cos(g, v, idf), sid) for sid, v in vecs.items()), reverse=True)
        if not scored or scored[0][0] < MATCH_MIN:
            continue
        if len(scored) > 1 and scored[0][0] < MATCH_MARGIN * scored[1][0]:
            continue  # could be about either story: leave it out
        out[scored[0][1]].append((st_id, round(scored[0][0], 3)))
    return out


# ---------- distinctive words ----------


def log_odds(a: Counter, b: Counter, prior: Counter, alpha0: float = 500.0) -> dict[str, float]:
    """z-scores of each word for A over B (positive = A's word), with an informative
    Dirichlet prior proportional to the background counts (Monroe et al. 2008)."""
    na, nb = sum(a.values()), sum(b.values())
    total = sum(prior.values()) or 1
    z: dict[str, float] = {}
    for w in set(a) | set(b):
        aw = alpha0 * (prior.get(w, 0) + 0.01) / total
        la = math.log((a[w] + aw) / max(1e-9, na + alpha0 - a[w] - aw))
        lb = math.log((b[w] + aw) / max(1e-9, nb + alpha0 - b[w] - aw))
        z[w] = (la - lb) / math.sqrt(1 / (a[w] + aw) + 1 / (b[w] + aw))
    return z


def group_terms(group: Counter, background: Counter) -> dict[str, float]:
    """Words a group's statements use far more than the story's coverage as a whole."""
    rest = background.copy()
    rest.subtract(group)
    rest = +rest
    z = log_odds(group, rest, background)
    keep = [(w, s) for w, s in z.items() if s >= Z_MIN and group[w] >= 2]
    top = sorted(keep, key=lambda x: -x[1])
    return dict(top[:TOP_TERMS])


def groups(anchors: list[Anchor]) -> dict[str, Counter]:
    """Statement words by speaker group: 정부, or the party's name."""
    out: dict[str, Counter] = {}
    for an in anchors:
        key = "정부" if an.side == "government" else an.party
        out.setdefault(key, Counter()).update(an.words)
    return out


@dataclass
class Echo:
    group: str
    hits: int
    rate: float  # group words per 1,000 words of the article
    z: float | None = None  # rate standardised across the story's articles


def echoes(article: Counter, terms: dict[str, dict[str, float]]) -> dict[str, Echo]:
    total = max(1, sum(v for w, v in article.items() if " " not in w))
    out = {}
    for g, ts in terms.items():
        hits = sum(article[w] for w in ts)
        out[g] = Echo(g, hits, round(1000 * hits / total, 2))
    return out


def standardise(per_article: dict[int, dict[str, Echo]]) -> None:
    """z-score each group's rate across the story's articles, so a story's vocabulary
    cancels out and only differences between outlets remain."""
    by_group: dict[str, list[Echo]] = {}
    for es in per_article.values():
        for g, e in es.items():
            by_group.setdefault(g, []).append(e)
    for es in by_group.values():
        if len(es) < 4:
            continue
        mean = sum(e.rate for e in es) / len(es)
        sd = math.sqrt(sum((e.rate - mean) ** 2 for e in es) / (len(es) - 1))
        for e in es:
            e.z = round((e.rate - mean) / sd, 2) if sd > 0 else 0.0
