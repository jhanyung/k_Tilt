"""Publish an edition as static files for the site.

  site/data/<date>-<slot>.json   the edition's data (headlines, links, quote counts,
                                 one-sentence quotes, linked official statements)
  site/index.html                the front page, rendered from the latest edition
  site/e/<date>-<slot>.html      the same page kept for the archive

Usage:
  python -m pipeline.publish                    # latest edition from the database
  python -m pipeline.publish --edition 12
  python -m pipeline.publish --from-json site/data/2026-10-21-am.json   # render only
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from pipeline.analyse import quotes as Q

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
REGISTRY = Path(__file__).resolve().parent / "outlets.yaml"
LOCK = Path(__file__).resolve().parent / "data" / "feeds.lock.json"
STATEMENT_SOURCES = Path(__file__).resolve().parent / "data" / "statements.yaml"
TOP_SPEAKERS = 8


def _outlets() -> dict[str, dict]:
    return {o["id"]: o for o in yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["outlets"]}


def _speaker_rows(rows: list[tuple]) -> list[Q.Quote]:
    out = []
    for speaker, name, title, org, role, party, bloc, quote, flags, outlet, url in rows:
        first = speaker.split(" ")[0] if speaker else ""
        initial = first if not name and len(first) == 1 and first in Q.SURNAMES else ""
        sp = Q.Speaker(speaker, name, title, org, role, party, bloc, initial)
        q = Q.Quote(quote, sp, flags=list(flags or []))
        q.outlet, q.url = outlet, url  # type: ignore[attr-defined]
        out.append(q)
    return out


def side_of(sp: Q.Speaker) -> str:
    """정부 | 여당 | 야당 | 그 외, for the quote balance bar."""
    if sp.role == "정부":
        return "정부"
    if sp.bloc == "ruling":
        return "여당"
    if sp.bloc == "opposition":
        return "야당"
    return "그 외"


def export(conn, edition_id: int | None = None) -> dict:
    outlets = _outlets()
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    src_names = {
        s["id"]: s["name"]
        for s in yaml.safe_load(STATEMENT_SOURCES.read_text(encoding="utf-8"))["sources"]
    }
    eid = (
        edition_id
        or conn.execute(
            "select e.id from editions e where exists (select 1 from stories s "
            "where s.edition_id = e.id and s.status = 'published') order by e.id desc limit 1"
        ).fetchone()[0]
    )
    day, slot, window_end = conn.execute(
        "select edition_date, slot, window_end from editions where id = %s", (eid,)
    ).fetchone()
    total = conn.execute(
        "select count(*), count(distinct outlet_id) from articles where "
        "coalesce(published_at, first_seen_at) > %s - interval '24 hours' "
        "and coalesce(published_at, first_seen_at) <= %s",
        (window_end, window_end),
    ).fetchone()
    stories = []
    for sid, rank, headline, outlet_count, keywords in conn.execute(
        "select id, rank, headline, outlet_count, keywords from stories "
        "where edition_id = %s and status = 'published' order by rank",
        (eid,),
    ).fetchall():
        arts = conn.execute(
            "select a.outlet_id, a.title, a.url, coalesce(a.published_at, a.first_seen_at), "
            "sa.sub_event from story_articles sa join articles a on a.id = sa.article_id "
            "where sa.story_id = %s order by coalesce(a.published_at, a.first_seen_at)",
            (sid,),
        ).fetchall()
        seen: set[str] = set()
        coverage = []
        for oid, title, url, when, _ in arts:
            if oid in seen:
                continue
            seen.add(oid)
            o = outlets.get(oid, {})
            coverage.append(
                {"o": oid, "n": o.get("name", oid), "g": o.get("group", ""), "t": title,
                 "u": url, "p": when.astimezone(UTC).isoformat(timespec="minutes")}
            )  # fmt: skip
        qs = Q.harmonise(
            _speaker_rows(
                conn.execute(
                    "select q.speaker, q.name, q.title, q.org, q.role, q.party, q.bloc, "
                    "q.quote, q.flags, a.outlet_id, a.url from quotes q "
                    "join articles a on a.id = q.article_id "
                    "join story_articles sa on sa.article_id = a.id where sa.story_id = %s "
                    "order by a.id, q.seq",
                    (sid,),
                ).fetchall()
            )
        )
        # Balance = how many outlets quoted each side at least once, not how many sentences:
        # one statement repeated three times in sixty articles is still one voice.
        side_outlets: dict[str, set[str]] = defaultdict(set)
        for q in qs:
            if q.speaker.role != "미상":
                side_outlets[side_of(q.speaker)].add(q.outlet)  # type: ignore[attr-defined]
        sides = {k: len(v) for k, v in side_outlets.items()}
        by_speaker: dict[str, list[Q.Quote]] = defaultdict(list)
        for q in qs:
            if q.speaker.role != "미상":
                key = q.speaker.name or q.speaker.text
                by_speaker[key].append(q)
        speakers = []
        ranked = sorted(by_speaker.values(), key=lambda g: -len({q.outlet for q in g}))  # type: ignore[attr-defined]
        for group in ranked[:TOP_SPEAKERS]:
            sp = max((q.speaker for q in group), key=lambda s: len(s.text))
            common = Counter(q.text for q in group).most_common(1)[0][0]
            first = next(q for q in group if q.text == common)
            speakers.append(
                {"s": sp.text, "r": sp.role, "p": sp.party, "side": side_of(sp),
                 "c": len({q.outlet for q in group}),  # type: ignore[attr-defined]
                 "q": common, "o": outlets.get(first.outlet, {}).get("name", first.outlet),
                 "u": first.url}
            )  # fmt: skip
        statements = [
            {"src": src, "n": src_names.get(src, src), "t": title, "u": url,
             "d": d.isoformat() if d else None}
            for src, title, url, d in conn.execute(
                "select st.source, st.title, st.url, st.published_on from story_statements ss "
                "join statements st on st.id = ss.statement_id where ss.story_id = %s "
                "order by ss.similarity desc limit 6",
                (sid,),
            ).fetchall()
        ]  # fmt: skip
        stories.append(
            {"rank": rank, "headline": headline, "outlets": outlet_count,
             "angles": len({a[4] for a in arts}), "keywords": list(keywords or [])[:6],
             "coverage": coverage, "sides": sides,
             "quoting": len({q.outlet for q in qs}),  # type: ignore[attr-defined]
             "speakers": speakers, "statements": statements}
        )  # fmt: skip
    names = {o: v["name"] for o, v in outlets.items()}
    return {
        "edition": {
            "id": eid, "date": day.isoformat(), "slot": slot,
            "cutoff": window_end.astimezone(UTC).isoformat(timespec="minutes"),
            "headlines": total[0], "outlets_seen": total[1],
            "outlets_listed": len(outlets),
            "refused": [names.get(o, o) for o in lock.get("refused", [])],
            "blocked": [names.get(o, o) for o in lock.get("blocked", [])],
            "generated": datetime.now(UTC).isoformat(timespec="minutes"),
        },
        "stories": stories,
    }  # fmt: skip


def kst(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC) + timedelta(hours=9)


def archive_list() -> list[str]:
    return sorted((p.stem for p in (SITE / "data").glob("*.json")), reverse=True)


def write(data: dict, json_only: bool = False) -> None:
    ed = data["edition"]
    key = f"{ed['date']}-{ed['slot']}"
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    (SITE / "e").mkdir(parents=True, exist_ok=True)
    (SITE / "data" / f"{key}.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    if json_only:
        print(f"exported {key}")
        return
    from pipeline.render import render

    editions = archive_list()
    (SITE / "e" / f"{key}.html").write_text(render(data, editions, archived=True), "utf-8")
    if editions and editions[0] == key:
        (SITE / "index.html").write_text(render(data, editions), encoding="utf-8")
    print(f"published {key}: {len(data['stories'])} stories")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", type=int, default=0)
    ap.add_argument("--from-json", default="")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args(argv)
    if args.from_json:
        data = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        from pipeline.db import connect

        with connect() as conn:
            data = export(conn, args.edition or None)
    write(data, args.json_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
