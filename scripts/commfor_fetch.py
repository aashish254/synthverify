"""Operator-side fetcher for the Community Forensics tranche this repository measures on.

FC-4 says the product never fetches: `synthverify score` reads a corpus that is already on disk and
refuses to reach the network. This file is the *other* side of that rule -- the step a human runs from
a terminal before the measurement, with the same disclosure duty. It is deliberately not wired into any
`make` target, any CI job or any test, because a build that downloads a third-party research corpus is
not reproducible by the person who clones the repository.

What it does that a `curl` loop does not:

* **It checks the label it is about to write.** Every cell declares the `model_name` and the `label`
  column it expects; a row that disagrees is skipped and counted, so an offset guessed from a coarse
  survey cannot silently mislabel a sample. The generator name in the thesis comes from the source's own
  metadata, not from the directory the file landed in.
* **It records every request.** The exact URL, offset and row index plus the source metadata columns go
  into `provenance.jsonl` beside the images, so each byte on disk names the call that produced it.
* **It refuses a truncated cell.** The Datasets Server answers a binary column with base64, and it can
  return a cell it truncated for size; such a row would be a corrupt image with a valid-looking header
  record. `truncated_cells` non-empty is a skip with a reason.
* **It keeps only containers it can check.** The claimed format's magic bytes are compared with the
  decoded blob, and a format outside PNG and JPEG is skipped and counted instead of written. The LAION
  real pools carry WEBP rows, so the corpus on disk is PNG-and-JPEG *by construction of this tool*,
  which is a selection effect the metrics write-up has to state rather than a property of the source.
* **It resumes.** A file that already exists on disk and matches the recorded byte length is left alone,
  so an interrupted 600 MB pass does not restart at zero.

Usage::

    ./.venv/bin/python scripts/commfor_fetch.py --plan /tmp/sv-t59/plan.json \
        --out data/corpora/commfor --provenance data/corpora/commfor/provenance.jsonl

Stdlib only. No dependency enters the project or the 48-pin lock by way of this file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DATASET = "OwensLab/CommunityForensics-Eval"
CONFIG = "default"
SPLIT = "CompEval"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
USER_AGENT = "synthverify-research-provenance-fetch/1.0 (academic; non-commercial; CC BY-NC-SA 4.0 corpus)"

#: The columns copied verbatim into the provenance record. Deliberately everything but the bytes.
METADATA_COLUMNS = (
    "image_name",
    "format",
    "resolution",
    "mode",
    "model_name",
    "subset",
    "real_source",
    "split",
    "label",
    "architecture",
    "nsfw_flag",
    "prompt",
)

MAGIC = {"PNG": b"\x89PNG\r\n\x1a\n", "JPEG": b"\xff\xd8\xff"}
MAX_PAGE = 50


class FetchError(RuntimeError):
    """A plan that cannot be executed, rather than a row the server refused to give."""


def rows_url(offset: int, length: int) -> str:
    query = urllib.parse.urlencode(
        {"dataset": DATASET, "config": CONFIG, "split": SPLIT, "offset": offset, "length": length, "partial": "false"}
    )
    return f"{ROWS_URL}?{query}"


def fetch_page(offset: int, length: int, *, attempts: int = 4, timeout: int = 120) -> list[dict]:
    """One page of rows, retried with backoff. The server 500s on some oversized cells; that is a gap,
    not an error, and the caller records it."""
    url = rows_url(offset, length)
    last = ""
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            return payload.get("rows") or []
        except Exception as exc:  # noqa: BLE001 - a network retry classifies by message, not by type
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            time.sleep(1.5 * (attempt + 1))
    print(f"  gap at offset {offset}: {last}", file=sys.stderr, flush=True)
    return []


def cell_directories(plan: list[dict]) -> list[str]:
    return [cell["dir"] for cell in plan]


def matches(cell: dict, row: dict, truth: int) -> bool:
    """Whether this source row belongs in this cell.

    `model_name` and `label` are the identity of a cell. `require` is an optional extra condition
    matched as a case-insensitive substring, because `real_source` is a comma list on the generated
    rows ("coco,laion,...") and a single name on the real ones, and because the column that says which
    real dataset a sample was paired with is the one a confound disclosure rests on.
    """
    if row.get("model_name") != cell["model_name"] or int(row.get("label", -1)) != truth:
        return False
    for column, expected in (cell.get("require") or {}).items():
        if str(expected).lower() not in str(row.get(column, "")).lower():
            return False
    return True


def run(plan_path: Path, out: Path, provenance: Path, *, limit_bytes: int, dry: bool) -> int:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan, list) or not plan:
        raise FetchError(f"{plan_path}: a plan is a non-empty JSON list of cells")
    seen: set[str] = set()
    for cell in plan:
        for key in ("dir", "kind", "model_name", "ranges"):
            if key not in cell:
                raise FetchError(f"{plan_path}: cell {cell.get('dir')!r} is missing {key!r}")
        if cell["dir"] in seen and cell["kind"] == "fake":
            raise FetchError(f"{plan_path}: fake directory {cell['dir']!r} is declared twice")
        seen.add(cell["dir"])
        if cell["kind"] == "fake" and cell.get("generator") in (None, "", "real"):
            raise FetchError(f"{plan_path}: a fake cell needs a generator label other than 'real'")
        for column in (cell.get("require") or {}):
            if column not in METADATA_COLUMNS:
                raise FetchError(
                    f"{plan_path}: cell {cell['dir']!r} requires {column!r}, which is not a column of "
                    f"{DATASET} -- a typo here would silently match nothing"
                )

    expected_label = {"fake": 1, "real": 0}
    records: list[dict] = []
    counts = {
        "written": 0,
        "resumed": 0,
        "skipped_label": 0,
        "skipped_truncated": 0,
        "skipped_container": 0,
        "skipped_magic": 0,
        "gaps": 0,
    }
    bytes_fetched = 0
    handle = None
    recorded: set[tuple[str, int]] = set()
    if not dry:
        out.mkdir(parents=True, exist_ok=True)
        provenance.parent.mkdir(parents=True, exist_ok=True)
        if provenance.exists():
            # One line per image, so a resumed pass re-reads what it already recorded and stays quiet
            # about it rather than appending a second copy of the same fact.
            for line in provenance.read_text(encoding="utf-8").splitlines():
                try:
                    earlier = json.loads(line)
                except json.JSONDecodeError:
                    continue
                recorded.add((earlier.get("cell", ""), int(earlier.get("source_row_idx", -1))))
        handle = provenance.open("a", encoding="utf-8")
    for cell in plan:
        directory = out / cell["dir"]
        truth = expected_label[cell["kind"]]
        want = int(cell.get("take", 0))
        got = 0
        if not dry:
            directory.mkdir(parents=True, exist_ok=True)
        print(
            f"cell {cell['dir']}: model_name={cell['model_name']!r} label={truth} "
            f"take={want} ranges={cell['ranges']}",
            flush=True,
        )
        for start, span in cell["ranges"]:
            if got >= want:
                break
            offset = int(start)
            misses = 0
            ended = False
            while offset < int(start) + int(span) and got < want and bytes_fetched < limit_bytes and not ended:
                length = min(MAX_PAGE, int(start) + int(span) - offset, want - got + 10)
                page = fetch_page(offset, length)
                if not page:
                    counts["gaps"] += 1
                    offset += length
                    continue
                for entry in page:
                    if got >= want or bytes_fetched >= limit_bytes:
                        break
                    row = entry["row"]
                    index = int(entry["row_idx"])
                    if not matches(cell, row, truth):
                        counts["skipped_label"] += 1
                        # A run of rows that are not this cell is the block ending. Stopping caps the
                        # bytes spent on the neighbour's images instead of scanning to the range end.
                        # Before the first match the tolerance is wider, because a start row verified
                        # on a two-row probe can still sit inside the neighbouring block.
                        misses += 1
                        budget = (
                            int(cell.get("stop_after_misses", 25))
                            if got
                            else int(cell.get("start_abort_misses", 150))
                        )
                        if misses >= budget:
                            print(
                                f"  range {start}+{span}: {misses} consecutive rows outside "
                                f"{cell['model_name']!r}/label {truth} -- stopped at {index}",
                                flush=True,
                            )
                            ended = True
                            break
                        continue
                    misses = 0
                    if entry.get("truncated_cells"):
                        counts["skipped_truncated"] += 1
                        continue
                    blob = base64.b64decode(row["image_data"])
                    fmt = row.get("format") or ""
                    magic = MAGIC.get(fmt)
                    if magic is None:
                        # Not the source being wrong -- the corpus has WEBP reals and this fetch keeps
                        # PNG and JPEG only, which is a selection effect of the tool and has to be
                        # counted as one rather than quietly shrinking a pool.
                        counts["skipped_container"] += 1
                        print(
                            f"  row {index}: container {fmt!r} is not one this fetch accepts "
                            f"({', '.join(sorted(MAGIC))})",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    if not blob.startswith(magic):
                        counts["skipped_magic"] += 1
                        print(
                            f"  row {index}: claims {fmt!r} but its bytes are not that container",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    name = f"{index:07d}_{Path(row['image_name']).name}"
                    target = directory / name
                    already = target.exists() and target.stat().st_size == len(blob)
                    record = {
                        "source_dataset": DATASET,
                        "source_config": CONFIG,
                        "source_split": SPLIT,
                        "source_row_idx": index,
                        "request_url": rows_url(offset, length),
                        "cell": cell["dir"],
                        "kind": cell["kind"],
                        "generator": "real" if cell["kind"] == "real" else cell.get("generator", cell["model_name"]),
                        "truth": truth,
                        "file": str(Path(cell["dir"]) / name),
                        "bytes": len(blob),
                        "sha256": hashlib.sha256(target.read_bytes() if already else blob).hexdigest(),
                        "source": {column: row.get(column) for column in METADATA_COLUMNS},
                    }
                    if not dry:
                        if not already:
                            temporary = target.with_suffix(target.suffix + ".part")
                            temporary.write_bytes(blob)
                            temporary.replace(target)
                        # A file on disk with no line naming it is the crash window this closes: the
                        # bytes landed, the record did not.
                        if (cell["dir"], index) not in recorded:
                            handle.write(json.dumps(record, sort_keys=True) + "\n")
                            handle.flush()
                            recorded.add((cell["dir"], index))
                    records.append(record)
                    counts["resumed" if already else "written"] += 1
                    got += 1
                    bytes_fetched += len(blob)
                offset += length
        print(f"  -> {got} of {want} written, {bytes_fetched / 1e6:.1f} MB cumulative", flush=True)
    if handle is not None:
        handle.close()
    print(json.dumps(counts, sort_keys=True, indent=2))
    print(f"total in this pass: {bytes_fetched / 1e6:.1f} MB across {len(records)} files")
    if not dry and out.is_dir():
        for stray in sorted(p for p in out.rglob("*.part")):
            stray.unlink()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", required=True, type=Path, help="JSON list of cells to fetch")
    parser.add_argument("--out", required=True, type=Path, help="corpus root to write <root>/<cell dir>/")
    parser.add_argument(
        "--provenance",
        type=Path,
        default=None,
        help="JSONL appended to with one record per fetched image (default: <out>/provenance.jsonl)",
    )
    parser.add_argument(
        "--limit-mb",
        type=int,
        default=900,
        help="stop after this many megabytes, so one pass cannot spend the whole download budget",
    )
    parser.add_argument("--dry-run", action="store_true", help="read the live API and validate the plan, but write no file")
    args = parser.parse_args(argv)
    provenance = args.provenance or (args.out / "provenance.jsonl")
    try:
        return run(args.plan, args.out, provenance, limit_bytes=args.limit_mb * 1024 * 1024, dry=args.dry_run)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
