"""Each side's vocabulary, learned from its own official statements, and each outlet's
place between them.

  count  read statements not yet counted (text in memory only), add their word counts
         to lexicon_counts by speaker group: 정부, or the party's name
  build  pick the words that set each group apart (log-odds z-scores, each group against
         all the others) into the lexicon table
  place  over the last 30 days: within each story, how much each outlet's own sentences
         (quotes removed) use one side's words rather than the other's, compared with
         the other outlets covering that story (z-score, so topic choice cancels out).
         An outlet's index is its average over stories:
           ideology: + party_axis[1] wording ... - party_axis[0] wording
           power:    + government wording ... - opposition wording
         Split-half stability is reported over even and odd stories.

Usage: python -m pipeline.lexicon count [--limit 400] | build | place
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

from pipeline import http
from pipeline.analyse import anchors as A
from pipeline.analyse import quotes as Q
from pipeline.feeds import decode_html
from pipeline.statements import sources

MIN_DOCS = 3  # a word must appear in this many statements of its side
Z_KEEP = 2.58
PER_GROUP = 300
WINDOW_DAYS = 30
MIN_ARTICLES = 20  # an outlet needs this many scored articles to be placed
HOST_PAUSE = 1.0
FLUSH_EVERY = 100


def group_of(src: dict) -> str:
    """정부 = 청와대 briefings and the government's rebuttals of news reports. Ministry
    briefings are mostly Q&A transcripts on technical policy; they are counted apart
    (부처) and left out of the comparison."""
    if src["side"] == "government":
        return "부처" if src["id"] == "ministry" else "정부"
    return src["party"]


# ---------- count ----------


def _flush(sums: dict[str, Counter], docs: dict[str, Counter], done: list[int]) -> None:
    """Add counts in one round trip: COPY into a temp table, then upsert."""
    from pipeline.db import connect

    if not done:
        return
    with connect() as conn:
        conn.execute("create temp table lc (grp text, term text, n int, docs int) on commit drop")
        with conn.cursor() as cur:
            with cur.copy("copy lc (grp, term, n, docs) from stdin") as cp:
                for g, c in sums.items():
                    for t, n in c.items():
                        cp.write_row((g, t, n, docs[g][t]))
            cur.execute(
                "insert into lexicon_counts (grp, term, n, docs) select grp, term, n, docs "
                "from lc on conflict (grp, term) do update set "
                "n = lexicon_counts.n + excluded.n, docs = lexicon_counts.docs + excluded.docs"
            )
            cur.execute("update statements set counted_at = now() where id = any(%s)", (done,))
        conn.commit()


def count(limit: int, minutes: float = 60) -> int:
    """Count up to `limit` statements, party statements first (they are fewer), saving
    every FLUSH_EVERY statements so a long backfill keeps what it has done."""
    from pipeline.db import connect

    srcs = {s["id"]: s for s in sources()}
    with connect() as conn:
        rows = conn.execute(
            "select id, source, url, title from statements where counted_at is null "
            "order by (side = 'party') desc, published_on desc nulls last limit %s",
            (limit,),
        ).fetchall()
    by_host: dict[str, list] = defaultdict(list)
    for r in rows:
        by_host[urlsplit(r[2]).netloc].append(r)

    deadline = time.time() + minutes * 60
    state = {"sums": defaultdict(Counter), "docs": defaultdict(Counter), "done": []}
    total = Counter()
    misses: Counter = Counter()  # (host, reason) -> statements skipped
    guard = threading.Lock()

    def save() -> None:
        _flush(state["sums"], state["docs"], state["done"])
        total["n"] += len(state["done"])
        state.update(sums=defaultdict(Counter), docs=defaultdict(Counter), done=[])

    def run(host: str) -> None:
        with http.client() as c:
            for n, (sid, source, url, title) in enumerate(by_host[host]):
                if time.time() > deadline:
                    return
                if n:
                    time.sleep(HOST_PAUSE)
                why = http.refusal(url)
                if why:
                    misses[(host, why)] += 1
                    continue
                try:
                    r = http.get(c, url)
                except httpx.HTTPError as e:
                    misses[(host, type(e).__name__)] += 1
                    time.sleep(5)
                    continue
                if r.status_code != 200:
                    misses[(host, f"http {r.status_code}")] += 1
                    time.sleep(5)
                    continue
                src = srcs[source]
                text = A.statement_text(decode_html(r.content), src["body"])
                if len(text) < 50:
                    misses[(host, "no text found")] += 1
                words = A.words(f"{title}\n{text}")
                with guard:
                    g = group_of(src)
                    state["sums"][g].update(words)
                    state["docs"][g].update(words.keys())
                    state["done"].append(sid)
                    if len(state["done"]) >= FLUSH_EVERY:
                        save()

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(run, list(by_host)))
    with guard:
        save()
    print(f"counted {total['n']} of {len(rows)} statements waiting")
    for (host, why), n in misses.most_common():
        print(f"  {host}: {n} {why}")
    return total["n"]


# ---------- build ----------


def axes_groups(counts: dict[str, Counter], docs: dict[str, Counter]) -> dict:
    """The two comparisons as (A name, A counts, A docs, B name, B counts, B docs)."""
    cfg = Q.politics()
    first, second = cfg["party_axis"]
    opp = [p for p, s in cfg["parties"].items() if s["bloc"] == "opposition"]
    opp_n, opp_d = Counter(), Counter()
    for p in opp:
        opp_n.update(counts.get(p, Counter()))
        opp_d.update(docs.get(p, Counter()))
    return {
        "ideology": (second, counts.get(second, Counter()), docs.get(second, Counter()),
                     first, counts.get(first, Counter()), docs.get(first, Counter())),
        "power": ("정부", counts.get("정부", Counter()), docs.get("정부", Counter()),
                  "야당", opp_n, opp_d),
    }  # fmt: skip


def build() -> dict[str, list[tuple[str, float]]]:
    """Each axis compares its two sides directly (not each side against everything
    else, which let the large government corpus drown out a party's words) and keeps
    the same number of words for each side."""
    from pipeline.db import connect

    with connect() as conn:
        rows = conn.execute("select grp, term, n, docs from lexicon_counts").fetchall()
        counts: dict[str, Counter] = defaultdict(Counter)
        ndocs: dict[str, Counter] = defaultdict(Counter)
        for g, t, n, d in rows:
            counts[g][t] = n
            ndocs[g][t] = d
        out: dict[str, list[tuple[str, float]]] = {}
        rows_out: list[tuple[str, str, float, int]] = []
        for an, ac, ad, bn, bc, bd in axes_groups(counts, ndocs).values():
            if not ac or not bc:
                continue
            z = A.log_odds(ac, bc, ac + bc)
            print(
                f"{an} vs {bn}: words {sum(ac.values()):,} / {sum(bc.values()):,}, "
                f"distinct {len(ac):,} / {len(bc):,}, "
                f"z>=+{Z_KEEP} {sum(1 for v in z.values() if v >= Z_KEEP)}, "
                f"z<=-{Z_KEEP} {sum(1 for v in z.values() if v <= -Z_KEEP)}"
            )
            a_terms = [(t, v) for t, v in z.items() if v >= Z_KEEP and ad[t] >= MIN_DOCS]
            b_terms = [(t, -v) for t, v in z.items() if v <= -Z_KEEP and bd[t] >= MIN_DOCS]
            k = min(PER_GROUP, len(a_terms), len(b_terms))
            out[an] = sorted(a_terms, key=lambda x: -x[1])[:k]
            out[bn] = sorted(b_terms, key=lambda x: -x[1])[:k]
            rows_out += [(an, t, round(v, 2), ad[t]) for t, v in out[an]]
            rows_out += [(bn, t, round(v, 2), bd[t]) for t, v in out[bn]]
        with conn.transaction():
            conn.execute("delete from lexicon")
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into lexicon (grp, term, z, docs) values (%s, %s, %s, %s)",
                    rows_out,
                )
    for g, ts in out.items():
        print(f"{g}: {len(ts)} words, e.g. {', '.join(t for t, _ in ts[:25])}")
    return out


def reset() -> None:
    """Forget all counts so every statement is counted again under the current grouping."""
    from pipeline.db import connect

    with connect() as conn:
        conn.execute("delete from lexicon_counts")
        conn.execute("update statements set counted_at = null")
        conn.commit()
    print("counts cleared; statements will be counted again")


def load(conn) -> dict[str, set[str]]:
    terms: dict[str, set[str]] = defaultdict(set)
    for g, t in conn.execute("select grp, term from lexicon"):
        terms[g].add(t)
    return dict(terms)


def article_hits(text: str, lexicon: dict[str, set[str]]) -> dict:
    """Group-word counts in the reporter's own sentences, and how many words those have."""
    words = A.words(A.narrative(text))
    total = sum(v for w, v in words.items() if " " not in w)
    hits = {g: sum(words[t] for t in ts if t in words) for g, ts in lexicon.items()}
    return {"words": total, "hits": hits}


# ---------- place ----------


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return round(sxy / (sx * sy), 3) if sx and sy else None


def story_scores(articles: list[tuple[str, dict]], pos: list[str], neg: list[str]) -> dict:
    """Within one story: each article's (pos-group rate - neg-group rate), as a z-score
    across the story's articles. Same story, so topic cancels and wording remains."""
    vals = []
    for outlet, lex in articles:
        w = max(1, lex["words"])
        h = lex["hits"]
        vals.append((outlet, (sum(h.get(g, 0) for g in pos) - sum(h.get(g, 0) for g in neg)) / w))
    if len(vals) < 5:
        return {}
    mean = sum(v for _, v in vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for _, v in vals) / (len(vals) - 1))
    if sd == 0:
        return {}
    return {o: (v - mean) / sd for o, v in vals}


def place() -> str:
    from pipeline.db import connect

    cfg = Q.politics()
    first, second = cfg["party_axis"]
    axes = {"ideology": ([second], [first]), "power": (["정부"], ["야당"])}
    with connect() as conn:
        rows = conn.execute(
            "select sa.story_id, a.outlet_id, a.metrics->'lex' from story_articles sa "
            "join articles a on a.id = sa.article_id "
            "where a.analysed_at > now() - make_interval(days => %s) and a.metrics ? 'lex'",
            (WINDOW_DAYS,),
        ).fetchall()
        by_story: dict[int, list] = defaultdict(list)
        for sid, outlet, lex in rows:
            by_story[sid].append((outlet, lex if isinstance(lex, dict) else json.loads(lex)))
        # outlet -> axis -> [all scores, even-story scores, odd-story scores]
        agg: dict[str, dict[str, list[list[float]]]] = defaultdict(
            lambda: {k: [[], [], []] for k in axes}
        )
        for sid, arts in by_story.items():
            for name, (pos, neg) in axes.items():
                for outlet, z in story_scores(arts, pos, neg).items():
                    agg[outlet][name][0].append(z)
                    agg[outlet][name][1 + sid % 2].append(z)

        def mean(xs: list[float]) -> float | None:
            return round(sum(xs) / len(xs), 3) if xs else None

        lines, halves = [], {k: [] for k in axes}
        for outlet, axs in sorted(agg.items()):
            n = len(axs["ideology"][0])
            ideo, power = mean(axs["ideology"][0]), mean(axs["power"][0])
            placed = n >= MIN_ARTICLES
            if placed:
                conn.execute(
                    "update outlets set ideology = %s, power = %s, confidence = %s, "
                    "updated_at = now() where id = %s",
                    (ideo, power, min(1.0, n / 200), outlet),
                )
                for k in axes:
                    a, b = mean(axs[k][1]), mean(axs[k][2])
                    if a is not None and b is not None and min(len(axs[k][1]), len(axs[k][2])) >= 5:
                        halves[k].append((a, b))
            shown = [("-" if v is None else v) for v in (ideo, power)]
            lines.append(
                f"{outlet:<14} stories {n:>4}  ideology {shown[0]:>7}  power {shown[1]:>7}"
                + ("" if placed else "  (too few)")
            )
        conn.commit()
    stab = {k: (_pearson([x for x, _ in v], [y for _, y in v]), len(v)) for k, v in halves.items()}
    lines.append(
        f"\nideology: + more {second} wording, - more {first}; "
        "power: + government wording, - opposition"
    )
    lines.append(
        "split-half stability (even vs odd stories, target r >= 0.8): "
        + ", ".join(f"{k} r={r} over {n} outlets" for k, (r, n) in stab.items())
    )
    text = "\n".join(lines)
    print(text)
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["count", "build", "place", "reset"])
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--minutes", type=float, default=60)
    args = ap.parse_args(argv)
    if args.action == "count":
        count(args.limit, args.minutes)
    elif args.action == "build":
        build()
    elif args.action == "reset":
        reset()
    else:
        text = place()
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(f"### Outlet placement\n\n```\n{text}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
