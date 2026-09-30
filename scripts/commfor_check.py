"""Prove the fetched corpus is what its provenance says it is, and measure the confounds on arrival.

T59's acceptance clause is that every figure in the write-up can be re-fetched from the provenance
document. Two things make that clause hard to honour by eye. The first is arithmetic: 2,000-odd files
each carry a claimed byte length and SHA-256 in `provenance.jsonl`, and a document that quotes a count
nobody re-hashed is a claim, not a measurement. The second is the corpus's own properties: the fake
cells are PNGs at one resolution each while the real pool is JPEGs at many, so a per-generator AUC is
partly a container-and-size tell. That difference has to be *read off the decoded pixels* rather than
copied from the source's `resolution` column, because the column is metadata the fetcher already
trusted once and a reviewer is entitled to ask what the file actually is.

So this script does exactly two jobs and prints both as tables it can be quoted from:

* **integrity** -- every record's file exists with the recorded size and hash, every image on disk has
  a record naming it, and the manifest (when given) has one row per record with the same generator
  label and truth. Any failure exits non-zero, because a corpus with an unexplained file is not a
  corpus a number can be reported from.
* **measurement** -- per directory, the decoded format, mode and pixel dimensions, the claimed
  dimensions from the source row, and the byte distribution. The claim and the measurement are
  compared rather than assumed equal; `source.resolution` turned out to be `[width, height]` for every
  row read here, which is the kind of thing worth checking against the file instead of remembering.

Stdlib plus Pillow. Operator-side: it is wired into no make target, no CI job and no test, and the
product never imports it, because FC-4 means `synthverify` must be able to score an already-present
corpus without knowing this dataset exists.

Usage::

    ./.venv/bin/python scripts/commfor_check.py --root data/corpora/commfor \
        [--manifest data/corpora/commfor/manifest.csv] [--json /tmp/report.json]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def load_records(provenance: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(provenance.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as fault:
            raise SystemExit(f"{provenance}:{lineno}: not JSON ({fault})") from fault
    return records


def check_integrity(root: Path, records: list[dict[str, Any]]) -> list[str]:
    """Re-hash every recorded file and name any file on disk no record claims.

    The reverse direction matters as much as the forward one: a stray image decodes, scores and lands
    in the denominator while belonging to no declared cell, which inflates a count without changing a
    URL anyone could follow to re-fetch it.
    """
    faults: list[str] = []
    claimed: set[str] = set()
    for record in records:
        relative = str(record["file"])
        if relative in claimed:
            faults.append(f"duplicate record for {relative}")
        claimed.add(relative)
        target = root / relative
        if not target.is_file():
            faults.append(f"{relative}: recorded but missing from disk")
            continue
        blob = target.read_bytes()
        if len(blob) != int(record["bytes"]):
            faults.append(f"{relative}: {len(blob)} bytes on disk, {record['bytes']} recorded")
        digest = hashlib.sha256(blob).hexdigest()
        if digest != record["sha256"]:
            faults.append(f"{relative}: sha256 {digest} != recorded {record['sha256']}")
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES):
        relative = str(path.relative_to(root))
        if relative not in claimed:
            faults.append(f"{relative}: on disk but named by no record")
    return faults


def check_manifest(root: Path, records: list[dict[str, Any]], manifest: Path) -> list[str]:
    """Compare the written manifest row-for-row against the provenance of the same files.

    These are two independent descriptions of one corpus: the manifest is what the harness reads, the
    provenance is what the write-up cites. `scan_by_generator` derives `sample_id` from the directory
    and filename, so a row here that disagrees with a record there is a scan that did not see the file
    the fetch wrote, or a generator label typed differently than the plan declared.
    """
    faults: list[str] = []
    with manifest.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    by_id = {str(record["file"]): record for record in records}
    seen: set[str] = set()
    for row in rows:
        sample_id = row["sample_id"]
        seen.add(sample_id)
        record = by_id.get(sample_id)
        if record is None:
            faults.append(f"manifest row {sample_id}: named by no provenance record")
            continue
        if row["generator"] != record["generator"]:
            faults.append(
                f"{sample_id}: manifest generator {row['generator']!r} != provenance {record['generator']!r}"
            )
        if int(row["truth"]) != int(record["truth"]):
            faults.append(f"{sample_id}: manifest truth {row['truth']} != provenance {record['truth']}")
        if str(Path(row["path"])) != sample_id:
            faults.append(f"{sample_id}: manifest path {row['path']!r} is not the scan-dir-relative id")
    for sample_id in sorted(set(by_id) - seen):
        faults.append(f"provenance record {sample_id}: absent from the manifest")
    return faults


def measure(root: Path, records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Decode every file and aggregate what it *is*, keyed by generator then directory.

    Two cells can share a directory -- every real pool here lands in `real/` because
    `scan_by_generator` takes one `--real-dir` -- so the grouping that the per-generator table is
    measured on is the generator label, with the directory reported beside it.
    """
    from PIL import Image

    per_cell: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        per_cell[(record["generator"], record["cell"])].append(record)

    out: dict[str, dict[str, Any]] = {}
    for (generator, cell), group in sorted(per_cell.items()):
        formats: Counter[str] = Counter()
        modes: Counter[str] = Counter()
        sizes: Counter[str] = Counter()
        mismatched_claims: list[str] = []
        byte_lengths: list[int] = []
        for record in group:
            target = root / str(record["file"])
            if not target.is_file():
                continue
            with Image.open(target) as image:
                image.load()  # a header that parses is not a file that decodes; `score` needs the pixels
                measured_format = (image.format or "").upper()
                measured_mode = image.mode
                width, height = image.size
            formats[measured_format] += 1
            modes[measured_mode] += 1
            sizes[f"{width}x{height}"] += 1
            byte_lengths.append(target.stat().st_size)
            claimed = record["source"].get("resolution") or []
            if measured_format != str(record["source"].get("format", "")).upper():
                mismatched_claims.append(f"{record['file']}: format")
            elif claimed and [int(claimed[0]), int(claimed[1])] != [width, height]:
                mismatched_claims.append(
                    f"{record['file']}: claims {claimed}, file is {width}x{height}"
                )
        out[f"{generator}|{cell}"] = {
            "generator": generator,
            "dir": cell,
            "kind": group[0]["kind"],
            "n": len(group),
            "decoded": len(byte_lengths),
            "formats": dict(formats),
            "modes": dict(modes),
            "sizes": dict(sizes.most_common()),
            "claim_mismatches": mismatched_claims,
            "bytes_min": min(byte_lengths, default=0),
            "bytes_median": int(statistics.median(byte_lengths)) if byte_lengths else 0,
            "bytes_max": max(byte_lengths, default=0),
            "bytes_total": sum(byte_lengths),
            "sources": dict(Counter(str(r["source"].get("real_source", "")) for r in group)),
            "architectures": dict(Counter(str(r["source"].get("architecture", "")) for r in group)),
        }
    return out


def print_report(out: dict[str, dict[str, Any]], *, json_path: Path | None) -> None:
    header = (
        f"{'generator':22s} {'dir':17s} {'kind':5s} {'n':>4s} {'format(s)':16s} "
        f"{'decoded size(s)':24s} {'MB':>6s}"
    )
    print(header)
    print("-" * len(header))
    for cell in out.values():
        formats = ", ".join(f"{name}×{count}" for name, count in sorted(cell["formats"].items()))
        sizes = ", ".join(f"{name}×{count}" for name, count in list(cell["sizes"].items())[:3])
        if len(cell["sizes"]) > 3:
            sizes += f", …{len(cell['sizes']) - 3} more"
        print(
            f"{cell['generator']:22s} {cell['dir']:17s} {cell['kind']:5s} {cell['n']:>4d} "
            f"{formats:16s} {sizes:24s} {cell['bytes_total'] / 1e6:>6.1f}"
        )
    print()
    for key, cell in out.items():
        if cell["claim_mismatches"]:
            print(f"{key}: {len(cell['claim_mismatches'])} claim mismatch(es)")
            for line in cell["claim_mismatches"][:5]:
                print(f"  {line}")
        if cell["n"] != cell["decoded"]:
            print(f"{key}: decoded {cell['decoded']} of {cell['n']} recorded files")
    total = sum(cell["n"] for cell in out.values())
    print(
        f"\ntotal {total} files, "
        f"{sum(cell['bytes_total'] for cell in out.values()) / 1e6:.1f} MB on disk"
    )
    if json_path is not None:
        json_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {json_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path, help="corpus root holding <dir>/ and provenance.jsonl")
    parser.add_argument("--provenance", type=Path, default=None, help="JSONL to read (default: <root>/provenance.jsonl)")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest CSV to cross-check, if one is written")
    parser.add_argument("--json", type=Path, default=None, help="write the per-cell measurements here")
    args = parser.parse_args(argv)

    provenance = args.provenance or args.root / "provenance.jsonl"
    if not provenance.is_file():
        print(f"{provenance}: no provenance file", file=sys.stderr)
        return 2
    records = load_records(provenance)
    faults = check_integrity(args.root, records)
    if args.manifest is not None:
        if not args.manifest.is_file():
            print(f"{args.manifest}: no manifest file", file=sys.stderr)
            return 2
        faults += check_manifest(args.root, records, args.manifest)

    out = measure(args.root, records)
    print_report(out, json_path=args.json)
    print(f"\nintegrity: {len(records)} records, {len(faults)} fault(s)")
    for line in faults[:20]:
        print(f"  {line}")
    if len(faults) > 20:
        print(f"  …{len(faults) - 20} more")
    if faults:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
