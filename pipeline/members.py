"""Refresh the National Assembly member list (name, party, district) from the Assembly's
open-data API. Used to place quoted lawmakers whose party the article does not name.

Needs ASSEMBLY_API_KEY (free key from open.assembly.go.kr). Writes
pipeline/data/assembly_members.json; the weekly workflow commits it when it changes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from pipeline.http import USER_AGENT

OUT = Path(__file__).resolve().parent / "data" / "assembly_members.json"
API = "https://open.assembly.go.kr/portal/openapi/nwvrqwxyaytdsfvhu"  # 국회의원 인적사항
PAGE = 100


def fetch(key: str) -> list[dict]:
    rows: list[dict] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as c:
        for page in range(1, 20):
            r = c.get(API, params={"KEY": key, "Type": "json", "pIndex": page, "pSize": PAGE})
            r.raise_for_status()
            data = r.json()
            block = data.get("nwvrqwxyaytdsfvhu")
            if not block:
                result = data.get("RESULT", {})
                if result.get("CODE") == "INFO-200":  # no more data
                    break
                raise SystemExit(f"Assembly API: {result.get('CODE')} {result.get('MESSAGE')}")
            batch = block[1].get("row", []) if len(block) > 1 else []
            rows += batch
            if len(batch) < PAGE:
                break
    return rows


def main() -> int:
    key = os.environ.get("ASSEMBLY_API_KEY")
    if not key:
        print("ASSEMBLY_API_KEY is not set; keeping the current member list.")
        return 0
    rows = fetch(key)
    members = sorted(
        (
            {"name": r["HG_NM"].strip(), "party": r["POLY_NM"].strip(), "district": r["ORIG_NM"]}
            for r in rows
            if r.get("HG_NM")
        ),
        key=lambda m: (m["name"], m["district"]),
    )
    if len(members) < 250:
        raise SystemExit(f"only {len(members)} members returned; not overwriting the list")
    old = json.loads(OUT.read_text(encoding="utf-8"))["members"] if OUT.exists() else []
    if old == members:
        print(f"{len(members)} members, unchanged")
        return 0
    OUT.write_text(
        json.dumps(
            {
                "source": "열린국회정보 국회의원 인적사항 (open.assembly.go.kr)",
                "fetched": datetime.now(UTC).date().isoformat(),
                "members": members,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"{len(members)} members written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
