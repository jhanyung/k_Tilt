"""Analyse the articles of an edition's published stories: fetch each page, extract
the quotes, store speakers and one-sentence quotes, drop the text.

Usage:
  python -m pipeline.analyse                     # latest edition
  python -m pipeline.analyse --edition 12        # a given edition id
  python -m pipeline.analyse --dry-run           # fetch and report, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from pipeline.analyse import bodies, quotes

LOCK = Path(__file__).resolve().parent.parent / "data" / "feeds.lock.json"
PER_STORY = 30  # articles fetched per story, at most one per outlet
ROLE_ORDER = [
    "정부", "정당", "지자체", "전문가", "시민", "단체", "기업", "사법·수사", "언론", "해외",
]  # fmt: skip


def pick_articles(rows: list[tuple]) -> list[tuple]:
    """One article per outlet per story, the earliest first, up to PER_STORY."""
    seen: set[tuple[int, str]] = set()
    count: Counter = Counter()
    out = []
    for story_id, article_id, outlet_id, url in rows:
        if (story_id, outlet_id) in seen or count[story_id] >= PER_STORY:
            continue
        seen.add((story_id, outlet_id))
        count[story_id] += 1
        out.append((story_id, article_id, outlet_id, url))
    return out


def summarise(story_rows: dict[int, dict], by_story: dict[int, list[quotes.Quote]]) -> str:
    lines = []
    for sid, info in story_rows.items():
        qs = quotes.harmonise(by_story.get(sid, []))
        roles = Counter(q.speaker.role for q in qs)
        blocs = Counter(q.speaker.bloc for q in qs if q.speaker.bloc)
        top = Counter(q.speaker.text for q in qs if q.speaker.role != "미상").most_common(4)
        lines.append(f"{info['rank']:>2}. {info['headline']}")
        lines.append(
            f"    pages {info['ok']}/{info['tried']}  quotes {len(qs)}  "
            + " ".join(f"{r} {roles[r]}" for r in ROLE_ORDER if roles[r])
            + (f"  미상 {roles['미상']}" if roles["미상"] else "")
        )
        if blocs:
            lines.append(f"    여권 {blocs['ruling']}  야권 {blocs['opposition']}")
        if top:
            lines.append("    most quoted: " + ", ".join(f"{s} ({n})" for s, n in top))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-analyse articles done before")
    args = ap.parse_args(argv)

    from pipeline.db import connect

    lock = json.loads(LOCK.read_text(encoding="utf-8"))["outlets"]
    t0 = time.time()
    with connect() as conn:
        eid = args.edition or conn.execute("select max(id) from editions").fetchone()[0]
        if not eid:
            print("no edition yet")
            return 0
        stories = conn.execute(
            "select id, rank, headline from stories where edition_id = %s "
            "and status = 'published' order by rank",
            (eid,),
        ).fetchall()
        rows = conn.execute(
            "select sa.story_id, a.id, a.outlet_id, a.url from story_articles sa "
            "join articles a on a.id = sa.article_id "
            "join stories s on s.id = sa.story_id "
            "where s.edition_id = %s and s.status = 'published' "
            + ("" if args.force else "and a.analysed_at is null ")
            + "order by s.rank, coalesce(a.published_at, a.first_seen_at)",
            (eid,),
        ).fetchall()
        todo = pick_articles(rows)
        story_of: dict[int, list[int]] = defaultdict(list)
        for sid, aid, _, _ in todo:
            story_of[aid].append(sid)
        meta = {r[3]: (r[1], r[2]) for r in todo}
        items = [(url, lock.get(o, {}).get("via") == "relay") for _, _, o, url in todo]
        items = list(dict.fromkeys(items))  # an article can sit in two stories

        results: dict[int, tuple[str, list[quotes.Quote]]] = {}
        guard = threading.Lock()

        def on_page(page: bodies.Page) -> None:
            found = quotes.extract(page.text) if page.status == "ok" else []
            with guard:
                results[meta[page.url][0]] = (page.status, found)
            # page.text goes out of scope here; nothing keeps it.

        counts = bodies.fetch_many(items, on_page)

        story_info = {
            sid: {"rank": rank, "headline": headline, "ok": 0, "tried": 0}
            for sid, rank, headline in stories
        }
        by_story: dict[int, list[quotes.Quote]] = defaultdict(list)
        for aid, (status, found) in results.items():
            for sid in story_of[aid]:
                story_info[sid]["tried"] += 1
                story_info[sid]["ok"] += status == "ok"
                by_story[sid].extend(found)
        text = summarise(story_info, by_story)
        print(text)
        print(f"\npages: {dict(counts)}  ({time.time() - t0:.0f}s)")

        if not args.dry_run:
            with conn.transaction():
                for aid, (status, found) in results.items():
                    conn.execute(
                        "update articles set analysed_at = now(), analyse_status = %s "
                        "where id = %s",
                        (status, aid),
                    )
                    conn.execute("delete from quotes where article_id = %s", (aid,))
                    with conn.cursor() as cur:
                        cur.executemany(
                            "insert into quotes (article_id, seq, speaker, name, title, org, "
                            "role, party, bloc, quote, method, flags) "
                            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            [
                                (
                                    aid,
                                    n,
                                    q.speaker.text[:120],
                                    q.speaker.name,
                                    q.speaker.title,
                                    q.speaker.org[:120],
                                    q.speaker.role,
                                    q.speaker.party,
                                    q.speaker.bloc,
                                    q.text,
                                    q.method,
                                    q.flags,
                                )
                                for n, q in enumerate(found)
                            ],
                        )
            print(f"stored quotes for {len(results)} articles")

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(f"### Quotes, edition {eid}\n\n```\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
