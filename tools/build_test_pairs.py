"""Build site/test/pairs.json for the news-habit test from an ingest dump.

Each pair is two real headlines about one event, taken exactly as the outlets
published them (matched by prefix so nothing is retyped), plus the pole each
headline represents on its axis:

  voice:   gov (government/ruling party voice leads)  vs  critic (opposition/critics lead)
  tone:    fact (states what happened)                 vs  interp (adds interpretation)
  outlook: hope (stresses the positive development)    vs  caution (stresses the risk)

Usage: python tools/build_test_pairs.py items.json
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "pipeline/data/feeds.lock.json"
OUT = ROOT / "site/test/pairs.json"

PAIRS = [
    ("voice", "대법원장 탄핵 논의",
     ("insight", "김민석 “조희대 탄핵, 모든"), ("daejonilbo", "국힘, 조희대 탄핵 논의에")),
    ("tone", "일본 총리의 야스쿠니 신사 공물 봉납",
     ("dt", "다카이치 총리, 야스쿠니 신사에 공물"), ("munhwa", "직접 가는 대신 공물만")),
    ("outlook", "IMF의 한국 잠재성장률 전망",
     ("fnnews", "반도체發 잠재성장률 반등"), ("newspim", "韓 잠재성장률 40년 만에 2년 연속 상승")),
    ("voice", "국무회의 생중계 논란",
     ("hani", "이 대통령 “국무회의 비공개 주장"), ("munhwa", "장동혁 “국무회의 생중계 계속하라")),
    ("tone", "국민연금 3분기 국내 주식 평가손실",
     ("sedaily", "국민연금, 3분기 국내 증시 손실"), ("etoday", "'반도체 쏠림' 부메랑")),
    ("outlook", "코스피 8거래일 만의 반등",
     ("etnews", "코스피 2%·코스닥 4% 반등"), ("ebn", "4% 급등했는데 아직 약하다")),
    ("voice", "추미애 경기지사 '소방 재정 빨대' 발언 사과",
     ("daejonilbo", "김민석, 추미애 '소방 재정 빨대' 발언 공식 사과"),
     ("dailian", '국민의힘, 추미애 사과에 "빈소는')),
    ("tone", "트럼프 대통령의 대러 제재 면제",
     ("ytn", "트럼프, 러시아 제재 면제 발표"), ("joongang", "선거 코앞인데 기름값")),
    ("outlook", "정부의 첫 주택공급 정례 브리핑",
     ("heraldcorp", "정부 “착공·분양·입주 상황판"), ("hankookilbo", "집값 불안에 공급 성과")),
    ("voice", "전시작전통제권 전환 조건을 둘러싼 한미 이견",
     ("mbn", "민주당, 미국의 '전작권 새 조건'"), ("dailian", "野, 한미 간 전작권 전환 시각차에")),
    ("tone", "트럼프 대통령의 '현대 조지아 투자' 발언",
     ("obs", '트럼프 "현대, 조지아에'), ("sisajournal", '트럼프 "현대, 조지아에')),
    ("outlook", "부천 화재 순직 소방관 추모",
     ("newsis", "여야, 부천 소방관 순직에 애도"), ("dailian", "여야 대표, 순직 소방관 애도")),
]  # fmt: skip
POLES = {"voice": ("gov", "critic"), "tone": ("fact", "interp"), "outlook": ("hope", "caution")}


def main(dump: str) -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))["outlets"]
    items: dict[tuple[str, str], dict] = {}
    for it in json.loads(Path(dump).read_text(encoding="utf-8")):
        items.setdefault((it["outlet_id"], html.unescape(it["title"]).strip()), it)

    def find(outlet: str, prefix: str) -> dict:
        hits = [(t, it) for (o, t), it in items.items() if o == outlet and t.startswith(prefix)]
        if len(hits) != 1:
            raise SystemExit(f"{outlet} '{prefix}': {len(hits)} matches {[t for t, _ in hits][:3]}")
        title, it = hits[0]
        return {"outlet": outlet, "name": lock[outlet]["name"], "title": title, "url": it["url"]}

    pairs = []
    for n, (axis, context, a, b) in enumerate(PAIRS, start=1):
        first, second = find(*a), find(*b)
        first["pole"], second["pole"] = POLES[axis]
        pairs.append({"id": n, "axis": axis, "context": context, "options": [first, second]})
    data = {
        "meta": {
            "collected": "2026년 10월 16~18일",
            "note": "제목은 언론사가 게재한 그대로입니다. 링크는 각 언론사 원문으로 연결됩니다.",
        },
        "pairs": pairs,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for p in pairs:
        for o in p["options"]:
            print(f"{p['id']:>2} {p['axis']:<7} {o['pole']:<7} {o['name']:<8} {o['title'][:68]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
