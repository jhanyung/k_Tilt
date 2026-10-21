"""Render an edition as a static, newspaper-style front page.

Dense by design (as Korean news portals are): every story shows how many outlets
covered it, how each side was quoted, how the headlines differ, and what the government
and parties said, with the full list one tap away. Plain HTML and CSS, no scripts.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

SIDES = ["정부", "여당", "야당", "그 외"]
GROUP_LABEL = {
    "national": "종합지", "broadcast": "방송", "wire": "통신", "economic": "경제지",
    "internet": "인터넷", "regional": "지역지", "weekly": "주간지", "specialist": "전문지",
}  # fmt: skip
GROUP_ORDER = list(GROUP_LABEL)
SOURCE_SHORT = {
    "cheongwadae": "청와대", "ministry": "부처", "factcheck": "정부 해명",
    "minjoo": "더불어민주당", "ppp": "국민의힘",
}  # fmt: skip
WEEKDAYS = "월화수목금토일"


def e(text) -> str:
    # Some feeds double-encode entities ("&amp;quot;"); decode fully, then escape once.
    return html.escape(html.unescape(html.unescape(str(text))), quote=True)


def kst(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC) + timedelta(hours=9)


def _grams(t: str) -> set[str]:
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", t)
    return {t[i : i + 2] for i in range(len(t) - 1)}


def contrast(story: dict, n: int = 3) -> list[dict]:
    """Headlines that differ most from the lead headline and from each other, from
    different kinds of outlets where possible."""
    cov = story["coverage"]
    lead = _grams(story["headline"])
    chosen: list[dict] = []

    # Only headlines clearly about the same event: the most different headline in a
    # cluster is otherwise often a stray from another event.
    def near(c: dict) -> float:
        g = _grams(c["t"])
        return len(g & lead) / max(1, len(g | lead))

    pool = [c for c in cov if c["t"] != story["headline"] and near(c) >= 0.12]
    if len(pool) < n:
        pool = sorted((c for c in cov if c["t"] != story["headline"]), key=near, reverse=True)[
            : n * 2
        ]
    while pool and len(chosen) < n:
        used_groups = {c["g"] for c in chosen}
        refs = [lead] + [_grams(c["t"]) for c in chosen]

        def score(c: dict, refs=refs, used_groups=used_groups) -> float:
            g = _grams(c["t"])
            sim = max(len(g & r) / max(1, len(g | r)) for r in refs)
            return sim + (0.25 if c["g"] in used_groups else 0)

        best = min(pool, key=score)
        chosen.append(best)
        pool = [c for c in pool if c["o"] != best["o"]]
    return chosen


def lead_item(story: dict) -> dict:
    return next((c for c in story["coverage"] if c["t"] == story["headline"]), {})


# ---------- pieces ----------


def bar(sides: dict, big: bool = False) -> str:
    total = sum(sides.get(s, 0) for s in SIDES)
    if not total:
        return '<p class="bal none">직접 인용 없음</p>'
    segs = "".join(
        f'<i class="s{k}" style="flex:{sides[s]}"></i>' for k, s in enumerate(SIDES) if sides.get(s)
    )
    legend = "".join(
        f'<span class="l s{k}">{s} <b>{sides[s]}</b></span>'
        for k, s in enumerate(SIDES)
        if sides.get(s)
    )
    label = "인용한 언론사 수" if big else "인용"
    return (
        f'<div class="bal{" big" if big else ""}" role="img" '
        f'aria-label="{label}: '
        + ", ".join(f"{s} {sides[s]}곳" for s in SIDES if sides.get(s))
        + f'"><div class="bar">{segs}</div><div class="leg"><span class="k">{label}</span>'
        f"{legend}</div></div>"
    )


def compare(story: dict, n: int) -> str:
    rows = "".join(
        f'<li><span class="o">{e(c["n"])}</span><a href="{e(c["u"])}" rel="noopener" '
        f'target="_blank">{e(c["t"])}</a></li>'
        for c in contrast(story, n)
    )
    return f'<ul class="cmp">{rows}</ul>' if rows else ""


def statements(story: dict, n: int) -> str:
    rows = "".join(
        f'<li><span class="src">{e(SOURCE_SHORT.get(s["src"], s["n"]))}</span>'
        f'<a href="{e(s["u"])}" rel="noopener" target="_blank">{e(s["t"])}</a></li>'
        for s in story["statements"][:n]
    )
    return f'<ul class="stm">{rows}</ul>' if rows else ""


def voices(story: dict, n: int) -> str:
    rows = "".join(
        f'<li><span class="who s{SIDES.index(v["side"])}">{e(v["s"])}</span>'
        f'<span class="cnt">{v["c"]}곳</span>'
        f'<q><a href="{e(v["u"])}" rel="noopener" target="_blank" title="{e(v["o"])}">'
        f"{e(v['q'])}</a></q></li>"
        for v in story["speakers"][:n]
    )
    return f'<ul class="vox">{rows}</ul>' if rows else ""


def full_list(story: dict) -> str:
    by_group: dict[str, list[dict]] = {}
    for c in story["coverage"]:
        by_group.setdefault(c["g"], []).append(c)
    parts = []
    for g in sorted(by_group, key=lambda x: GROUP_ORDER.index(x) if x in GROUP_ORDER else 99):
        rows = "".join(
            f'<li><span class="o">{e(c["n"])}</span><a href="{e(c["u"])}" rel="noopener" '
            f'target="_blank">{e(c["t"])}</a><time>{kst(c["p"]):%H:%M}</time></li>'
            for c in by_group[g]
        )
        parts.append(
            f"<h4>{e(GROUP_LABEL.get(g, g))} <span>{len(by_group[g])}</span></h4>"
            f'<ul class="all">{rows}</ul>'
        )
    more_voices = voices({"speakers": story["speakers"]}, 8)
    more_stm = statements(story, 6)
    return (
        f"<details><summary>전체 보도 {len(story['coverage'])}곳"
        f"{' · 인용 ' + str(len(story['speakers'])) + '명' if story['speakers'] else ''}"
        f"{' · 공식 입장 ' + str(len(story['statements'])) + '건' if story['statements'] else ''}"
        f"</summary>"
        + (f"<h4>공식 입장</h4>{more_stm}" if more_stm else "")
        + (
            f"<h4>많이 인용된 사람 <span>인용한 언론사 수</span></h4>{more_voices}"
            if more_voices
            else ""
        )
        + "".join(parts)
        + "</details>"
    )


def meta(story: dict) -> str:
    bits = [f"<b>{story['outlets']}</b>개 언론사"]
    if story["angles"] > 1:
        bits.append(f"{story['angles']}갈래")
    if story["keywords"]:
        bits.append(" ".join(f"#{e(k)}" for k in story["keywords"][:4]))
    return f'<p class="meta">{" · ".join(bits)}</p>'


def story_block(story: dict, lead: bool = False) -> str:
    src = lead_item(story)
    head = (
        f'<h2><a href="{e(src["u"])}" rel="noopener" target="_blank">{e(story["headline"])}</a>'
        f'</h2><p class="by">대표 제목 {e(src["n"])}</p>'
        if src
        else f"<h2>{e(story['headline'])}</h2>"
    )
    return (
        f'<article id="s{story["rank"]}" class="st{" lead" if lead else ""}">'
        f'<div class="rk">{story["rank"]}</div><div class="bd">'
        + head
        + meta(story)
        + bar(story["sides"], big=lead)
        + (f"<h3>제목 비교</h3>{compare(story, 3)}" if lead else compare(story, 2))
        + (f"<h3>공식 입장</h3>{statements(story, 3)}" if lead and story["statements"] else "")
        + (statements(story, 1) if not lead else "")
        + (f"<h3>많이 인용된 사람</h3>{voices(story, 3)}" if lead and story["speakers"] else "")
        + (voices(story, 1) if not lead else "")
        + full_list(story)
        + "</div></article>"
    )


def more_list(more: list[dict]) -> str:
    if not more:
        return ""
    rows = "".join(
        f'<li><span class="n">{m["rank"]}</span>'
        + (
            f'<a href="{e(m["u"])}" rel="noopener" target="_blank">{e(m["headline"])}</a>'
            if m.get("u")
            else e(m["headline"])
        )
        + f'<span class="c">{m["outlets"]}곳</span></li>'
        for m in more
    )
    return f'<h3>11~20위</h3><ol class="more">{rows}</ol>'


def sidebar(data: dict) -> str:
    ed = data["edition"]
    totals: Counter = Counter()
    for s in data["stories"]:
        totals.update(s["sides"])
    covered = sum(s["outlets"] for s in data["stories"])
    return (
        '<aside class="side">'
        '<h3>오늘의 숫자</h3><dl class="nums">'
        f"<div><dt>살펴본 기사 제목</dt><dd>{ed['headlines']:,}</dd></div>"
        f"<div><dt>기사를 낸 언론사</dt><dd>{ed['outlets_seen']}</dd></div>"
        f"<div><dt>10대 뉴스 보도 건수</dt><dd>{covered:,}</dd></div>"
        "</dl>"
        "<h3>10대 뉴스 전체 인용</h3>"
        + bar(dict(totals), big=True)
        + '<p class="note">각 쪽 사람의 말을 한 번이라도 직접 인용한 언론사 수를 사건별로 더한 값.</p>'
        + more_list(data.get("more", []))
        + '<a class="promo" href="/test/"><b>나의 뉴스 습관 테스트</b>'
        "<span>실제 제목 12쌍 중 고르면 어떤 제목에 손이 가는지 알려 드립니다.</span></a>"
        "</aside>"
    )


CSS = """
:root{--paper:#fff;--ink:#16181b;--sub:#5b6168;--faint:#8b9198;--rule:#e3e5e8;--tint:#f5f6f7;
--s0:#36414c;--s1:#2d6e68;--s2:#b0662a;--s3:#c5cad0;--vial:#c9d84a;
--serif:"Noto Serif KR",serif;--sans:"Pretendard Variable",Pretendard,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--paper:#121416;--ink:#e9ebee;--sub:#a6adb5;
--faint:#7d848c;--rule:#2a2e33;--tint:#1a1d20;--s0:#9aa6b2;--s1:#5fb3aa;--s2:#d99357;--s3:#4b5158}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 var(--sans);word-break:keep-all;overflow-wrap:anywhere}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}a:visited{color:var(--sub)}
a:focus-visible,summary:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding:0 12px}
header.mast{border-bottom:3px double var(--ink);padding:10px 0 6px}
.mrow{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.logo{font:900 26px/1 var(--serif);letter-spacing:-.04em;display:flex;align-items:center;gap:7px}
.logo svg{width:30px;height:12px;transform:rotate(-4deg)}
.tag{font-size:11px;border:1px solid var(--ink);padding:1px 5px;line-height:1.4}
.date{margin-left:auto;font-size:12.5px;color:var(--sub)}
.strap{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--sub);padding:6px 0;border-bottom:1px solid var(--rule)}
.strap b{color:var(--ink)}
main{display:grid;grid-template-columns:1fr;gap:0 24px;padding-top:4px}
.st{display:grid;grid-template-columns:22px 1fr;gap:0 8px;padding:12px 0;border-bottom:1px solid var(--rule)}
.rk{font:800 18px/1.2 var(--serif);color:var(--faint);text-align:right}
.st h2{font:700 16.5px/1.38 var(--serif);margin:0;letter-spacing:-.02em}
.st.lead .rk{font-size:30px;color:var(--ink)}
.st.lead h2{font-size:22px;line-height:1.32;font-weight:900}
.by{margin:2px 0 0;font-size:11.5px;color:var(--faint)}
.meta{margin:3px 0 6px;font-size:12px;color:var(--sub)}.meta b{color:var(--ink)}
h3{font:700 12px/1.4 var(--sans);margin:10px 0 3px;color:var(--sub);border-top:1px solid var(--ink);padding-top:4px}
.bal{margin:4px 0}.bal .bar{display:flex;height:6px;gap:1px;background:var(--paper)}
.bal.big .bar{height:10px}.bal .bar i{display:block;min-width:3px}
.s0{--c:var(--s0)}.s1{--c:var(--s1)}.s2{--c:var(--s2)}.s3{--c:var(--s3)}.bal .bar i{background:var(--c)}
.leg{display:flex;flex-wrap:wrap;gap:2px 10px;font-size:11.5px;color:var(--sub);margin-top:2px}
.leg .k{color:var(--faint)}.leg .l::before{content:"";display:inline-block;width:8px;height:8px;background:var(--c);margin-right:3px;vertical-align:-.5px}
.leg b{color:var(--ink);font-weight:700}.bal.none{font-size:11.5px;color:var(--faint);margin:4px 0}
ul{list-style:none;margin:0;padding:0}
.cmp li,.stm li,.vox li,.all li{display:grid;grid-template-columns:auto 1fr;gap:0 7px;padding:3px 0;font-size:13.5px;line-height:1.42}
.cmp li+li,.stm li+li,.vox li+li{border-top:1px dotted var(--rule)}
.o,.src{font-size:11px;color:var(--sub);background:var(--tint);padding:1px 4px;align-self:start;margin-top:2px;white-space:nowrap}
.src{color:var(--paper);background:var(--sub)}
.vox li{grid-template-columns:auto auto 1fr}.who{font-size:12px;font-weight:700;align-self:start;margin-top:1px}
.who::before{content:"";display:inline-block;width:3px;height:11px;background:var(--c);margin-right:4px;vertical-align:-1px}
.cnt{font-size:11px;color:var(--faint);align-self:start;margin-top:2px}.vox li q{grid-column:1/-1;quotes:"“" "”";color:var(--ink);font-family:var(--serif);font-size:13.5px}
details{margin-top:6px}summary{cursor:pointer;font-size:12px;color:var(--sub);list-style:none;padding:4px 0}
summary::-webkit-details-marker{display:none}summary::before{content:"+ ";font-weight:700;color:var(--ink)}
details[open] summary::before{content:"− "}
details h4{font-size:11.5px;margin:8px 0 2px;color:var(--ink)}details h4 span{color:var(--faint);font-weight:400}
.all li{grid-template-columns:auto 1fr auto;font-size:13px}.all time{font-size:11px;color:var(--faint);align-self:start;margin-top:2px}
.side{padding:12px 0;border-bottom:1px solid var(--rule)}
.side h3:first-child{margin-top:0}
.nums{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:4px 0 8px}.nums div{border-left:2px solid var(--ink);padding-left:6px}
.nums dt{font-size:11px;color:var(--sub)}.nums dd{margin:0;font:800 19px/1.2 var(--serif)}
.note{font-size:11.5px;color:var(--faint);margin:4px 0 10px}
.more li{display:grid;grid-template-columns:18px 1fr auto;gap:0 6px;padding:4px 0;font-size:13.5px;line-height:1.4;border-top:1px dotted var(--rule)}
.more li:first-child{border-top:0}.more .n{font:800 13px/1.4 var(--serif);color:var(--faint);text-align:right}.more .c{font-size:11px;color:var(--faint);margin-top:2px}
ol{list-style:none;margin:0;padding:0}
.promo{display:block;border:1px solid var(--ink);padding:10px;margin-top:8px;background:var(--tint)}
.promo b{display:block;font:800 15px/1.3 var(--serif)}.promo span{font-size:12.5px;color:var(--sub)}
footer{border-top:3px double var(--ink);margin-top:16px;padding:10px 0 28px;font-size:12px;color:var(--sub)}
footer p{margin:4px 0}footer a{text-decoration:underline}.arch a{margin-right:8px;white-space:nowrap}
@media (max-width:759px){.st.lead h2{font-size:20px}.st.lead .rk{font-size:26px}
.stm a,.vox q,.cmp a{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.strap .x{display:none}}
@media (min-width:760px){body{font-size:14.5px}.st.lead h2{font-size:27px}
main{grid-template-columns:minmax(0,2fr) minmax(0,1fr)}.lead{grid-column:1}.side{grid-column:2;grid-row:1;border-left:1px solid var(--rule);padding-left:16px;border-bottom:0}
.rest{grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 24px}}
@media (min-width:1080px){.rest{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
"""

LOGO = (
    '<svg viewBox="0 0 60 24" aria-hidden="true"><rect x="1" y="4" width="58" height="16" rx="3" '
    'fill="none" stroke="currentColor" stroke-width="2"/><rect x="17" y="8" width="26" height="8" '
    'rx="4" fill="var(--vial)"/><ellipse cx="35" cy="11" rx="5" ry="2" fill="#fff"/></svg>'
)


def render(data: dict, editions: list[str], archived: bool = False) -> str:
    ed = data["edition"]
    day = datetime.fromisoformat(ed["date"])
    slot = "아침판" if ed["slot"] == "am" else "저녁판"
    cut = kst(ed["cutoff"])
    title = f"기울기 {day.month}월 {day.day}일 {slot} — 오늘 가장 많이 보도된 뉴스 10"
    stories = data["stories"]
    lead, rest = (stories[0], stories[1:]) if stories else (None, [])
    refused = ", ".join(ed["refused"])
    blocked = ", ".join(ed["blocked"])
    arch = "".join(
        f'<a href="/e/{k}.html">{k[5:7].lstrip("0")}.{k[8:10].lstrip("0")} '
        f"{'아침' if k.endswith('am') else '저녁'}</a>"
        for k in editions[:14]
    )
    desc = (
        f"{day.month}월 {day.day}일 {slot}: 언론사 {ed['outlets_seen']}곳의 기사 제목 "
        f"{ed['headlines']:,}건 중 가장 많이 보도된 10건. 언론사별 제목과 누구의 말을 옮겼는지 비교합니다."
    )
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(desc)}">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:type" content="website"><meta name="theme-color" content="#ffffff">
{'<meta name="robots" content="noindex">' if archived else ""}
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+KR:wght@700;900&display=swap">
<style>{CSS}</style></head>
<body><div class="wrap">
<header class="mast"><div class="mrow"><a class="logo" href="/">{LOGO}기울기</a>
<span class="tag">뉴스 커뮤니티</span>
<span class="date">{day.year}년 {day.month}월 {day.day}일 {WEEKDAYS[day.weekday()]}요일 {slot}</span></div></header>
<div class="strap"><span class="x">{cut.month}월 {cut.day}일 {cut.hour}시까지 24시간</span>
<span>기사 제목 <b>{ed["headlines"]:,}</b>건</span><span>언론사 <b>{ed["outlets_seen"]}</b>곳</span>
<span>가장 많이 보도된 <b>{len(stories)}</b>건</span></div>
<main>
{story_block(lead, lead=True) if lead else "<p>아직 발행된 판이 없습니다.</p>"}
{sidebar(data)}
<div class="rest">{"".join(story_block(s) for s in rest)}</div>
</main>
<footer>
<p class="arch">지난 판 {arch}</p>
<p>순위는 같은 사건을 보도한 언론사 수로 정합니다(경제·전문지는 0.5곳). 인용은 기사 본문에서 자동으로 찾은 직접 인용이며, 무작위 100건 점검에서 96%가 정확했습니다. 공식 입장은 정책브리핑(korea.kr)과 각 정당 홈페이지에서 모읍니다.</p>
<p>수집거부(robots.txt): {e(refused) or "없음"}. 접속차단: {e(blocked) or "없음"}. 이 언론사들의 보도는 집계에 없습니다.</p>
<p>기사 본문은 저장하지 않으며 모든 제목은 각 언론사 원문으로 연결됩니다. 이 면은 자동 집계이며, AI 요약은 준비 중입니다.
<a href="https://github.com/jhanyung/k_Tilt/blob/main/METHOD.md">방법 공개</a></p>
<p>기울기는 언론사가 아닌 뉴스 커뮤니티입니다.</p>
</footer></div></body></html>
"""
