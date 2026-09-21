"""Build and checksum a frozen USGS replay corpus.

This is the reproducible command-line entry point for the network-backed
``watcher.replay.build_corpus`` helper.  Evaluation code never calls it: study
runs consume the resulting JSON artifact offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from watcher.replay import build_corpus, corpus_stats, save_corpus


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--county", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--fips", required=True)
    parser.add_argument("--start", required=True, help="inclusive ISO date/time")
    parser.add_argument("--end", required=True, help="inclusive ISO date/time")
    parser.add_argument("--site-id", action="append", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    corpus = build_corpus(
        county=args.county,
        state=args.state,
        fips=args.fips,
        start=args.start,
        end=args.end,
        site_ids=args.site_id,
    )
    target = save_corpus(corpus, args.out)
    print(
        json.dumps(
            {
                "path": str(target),
                "sha256": _sha256(target),
                "stats": corpus_stats(corpus),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
