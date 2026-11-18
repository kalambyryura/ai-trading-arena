"""Team market-data store: sha256 mirror, integrity catalogue and a range loader for 1m OKX history.

The store lives outside every worktree (data/okx/<instrument>/) and holds files in the
tools/public_history.py CSV format. Nothing here touches the network, keys or accounts.

  mirror   copy (never move) a folder tree, hashing source and copy; refuses to overwrite a different file
  catalog  scan every candle/funding CSV (header, confirm=1, minute grid, order, gaps, volume-unit eras),
           cross-check folder manifests, check segment continuity; writes CATALOG.json (+ SHA256SUMS.json if absent)
  verify   re-hash every file in SHA256SUMS.json and compare the catalogue with a fresh scan

  py -3.12 -X utf8 -B tools/team_data.py mirror --src <source-folder> --dst data/okx/BTC-USDT-SWAP
  py -3.12 -X utf8 -B tools/team_data.py catalog --root data/okx/BTC-USDT-SWAP --inst BTC-USDT-SWAP
  py -3.12 -X utf8 -B tools/team_data.py verify --root data/okx/BTC-USDT-SWAP

Python users: load_candles(root, start_ms, end_ms) returns {open_ms: okx_row} for the requested range only,
so a multi-year store never has to sit in memory at once.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil

COLUMNS = ["ts_ms", "utc", "open", "high", "low", "close", "vol_contracts", "vol_btc", "vol_quote_usdt", "confirm"]
PRICE_COLUMNS = ["ts_ms", "utc", "open", "high", "low", "close", "confirm"]  # index and mark-price candles: no volume
FUNDING_COLUMNS = ["funding_time_ms", "utc", "funding_rate", "realized_rate", "method", "formula_type"]
CANDLE_SERIES = ("_1m_", "_index-1m_", "_mark-1m_")
MINUTE = 60_000
SUMS, CATALOG = "SHA256SUMS.json", "CATALOG.json"
DEFAULT_STORE = Path("data/okx")


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    temporary = Path(path).with_name(Path(path).name + ".tmp")
    temporary.write_bytes((json.dumps(value, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))  # LF on every OS
    os.replace(temporary, path)


def files_under(root):
    root = Path(root)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name not in (SUMS, CATALOG)
                  and not p.name.endswith(".tmp"))


# ----------------------------------------------------------------------------------------------------------- mirror
def mirror(src, dst):
    """Copy every file of src into dst; each copy is re-hashed and must equal its source. Existing equal files stay."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if not src.is_dir():
        raise SystemExit(f"{src}: not a folder")
    if dst == src or src in dst.parents or dst in src.parents:
        raise SystemExit("source and destination must not contain each other")
    copied = kept = 0
    entries = {}
    for rel in files_under(src):
        source, target = src / rel, dst / rel
        digest, size = sha256_file(source), source.stat().st_size
        if target.exists():
            if sha256_file(target) != digest:
                raise SystemExit(f"{target}: exists with different bytes; nothing is overwritten")
            kept += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            shutil.copyfile(source, temporary)
            if sha256_file(temporary) != digest:
                temporary.unlink()
                raise SystemExit(f"{rel}: copy hash differs from source")
            os.replace(temporary, target)
            shutil.copystat(source, target)
            copied += 1
        entries[rel] = {"bytes": size, "sha256": digest}
    sums = {"schema": "team-data-sums/v1", "created_at": now_utc(), "mirrored_from": str(src).replace("\\", "/"),
            "method": "copy (source left untouched); every copy re-hashed against its source", "files": entries}
    write_json(dst / SUMS, sums)
    return {"files": len(entries), "copied": copied, "already_equal": kept,
            "bytes": sum(e["bytes"] for e in entries.values()), "sums": str(dst / SUMS)}


# ----------------------------------------------------------------------------------------------------------- scans
def scan_candles(path):
    """One pass over a public-history candle CSV: structure, minute grid, order, gaps and volume-unit eras."""
    problems, gaps, eras = [], [], []
    rows = zero_volume = 0
    first = previous = None
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header not in (COLUMNS, PRICE_COLUMNS):
            return {"problems": ["unexpected header"]}
        traded, confirm = header == COLUMNS, header.index("confirm")
        for row in reader:
            if len(row) != len(header):
                problems.append(f"row {rows + 1}: {len(row)} fields")
                break
            stamp = int(row[0])
            if stamp % MINUTE:
                problems.append(f"{row[1]}: not on the minute grid")
                break
            if row[confirm] != "1":
                problems.append(f"{row[1]}: unconfirmed candle")
                break
            if row[1] != iso(stamp):
                problems.append(f"{row[0]}: utc column disagrees with ts_ms")
                break
            o, h, l, c = (float(x) for x in row[2:6])
            if not (l <= min(o, c) and h >= max(o, c) and l > 0):
                problems.append(f"{row[1]}: OHLC out of order")
                break
            if previous is not None:
                if stamp <= previous:
                    problems.append(f"{row[1]}: time order broken")
                    break
                if stamp - previous != MINUTE:
                    gaps.append([iso(previous + MINUTE), iso(stamp - MINUTE), (stamp - previous) // MINUTE - 1])
            contracts, base = (float(row[6]), float(row[7])) if traded else (0.0, 0.0)
            zero_volume += traded and contracts == 0
            if traded and base > 0 and contracts > 0:
                ratio = contracts / base
                unit = round(ratio, 6) if ratio < 10 else round(ratio)
                if not eras or eras[-1]["contracts_per_base"] != unit:
                    eras.append({"contracts_per_base": unit, "first_utc": row[1], "last_utc": row[1], "rows": 0})
                eras[-1]["last_utc"], eras[-1]["rows"] = row[1], eras[-1]["rows"] + 1
            first = stamp if first is None else first
            previous, rows = stamp, rows + 1
    merged = []  # isolated rounding flips (tiny volumes) are folded into the surrounding era
    for era in eras:
        if merged and (era["rows"] < 30 or merged[-1]["contracts_per_base"] == era["contracts_per_base"]):
            merged[-1]["last_utc"], merged[-1]["rows"] = era["last_utc"], merged[-1]["rows"] + era["rows"]
        else:
            merged.append(dict(era))
    return {"rows": rows, "first_ms": first, "last_ms": previous, "first_open_utc": iso(first) if first is not None else None,
            "last_open_utc": iso(previous) if previous is not None else None,
            "expected_rows": (previous - first) // MINUTE + 1 if rows else 0, "gap_count": len(gaps),
            "missing_minutes": sum(g[2] for g in gaps), "gaps_first_200": gaps[:200], "zero_volume_rows": zero_volume,
            "volume_unit_eras": merged,
            "problems": problems}


def scan_funding(path):
    problems, stamps = [], []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        if next(reader, None) != FUNDING_COLUMNS:
            return {"problems": ["unexpected header"]}
        for row in reader:
            stamp = int(row[0])
            if stamps and stamp <= stamps[-1]:
                problems.append(f"{row[1]}: time order broken")
                break
            stamps.append(stamp)
    steps = sorted({b - a for a, b in zip(stamps, stamps[1:])})
    return {"rows": len(stamps), "first_utc": iso(stamps[0]) if stamps else None, "last_utc": iso(stamps[-1]) if stamps else None,
            "first_ms": stamps[0] if stamps else None, "last_ms": stamps[-1] if stamps else None,
            "interval_hours": [s / 3_600_000 for s in steps[:6]], "problems": problems}


def manifest_entry(folder, name):
    """The folder's own manifest record for a file (public-history/v1 or the older shared-okx-public-1m/v1)."""
    path = folder / "MANIFEST.json"
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for key in ("candles", "funding"):
        if (manifest.get(key) or {}).get("file") == name:
            return manifest[key]
    for key in ("files", "funding_files"):
        if name in (manifest.get(key) or {}):
            return manifest[key][name]
    return None


def is_data_file(rel, kind):
    parts = rel.split("/")
    tags = CANDLE_SERIES if kind == "1m" else (f"_{kind}_",)
    return ("cache" not in parts[:-1] and parts[-1].startswith("okx_") and parts[-1].endswith(".csv")
            and any(tag in parts[-1] for tag in tags))


def catalog(root, inst):
    root = Path(root)
    problems, segments, funding = [], [], []
    for rel in files_under(root):
        if is_data_file(rel, "1m"):
            scan = scan_candles(root / rel)
            entry = {"file": rel, "sha256": sha256_file(root / rel), **scan}
            declared = manifest_entry((root / rel).parent, Path(rel).name)
            if declared is None:
                problems.append(f"{rel}: no manifest record")
            else:
                declared = dict(declared)
                if "gaps_first_200" in declared:  # a gap before the first row (e.g. listing date) is not inside the file
                    inner = [g for g in declared["gaps_first_200"] if g[0] > (declared.get("first_open_utc") or "")]
                    entry["leading_missing_minutes"] = sum(g[2] for g in declared["gaps_first_200"]) - sum(g[2] for g in inner)
                    declared["gap_count"] = len(inner)
                for key in ("sha256", "rows", "first_open_utc", "last_open_utc", "gap_count"):
                    if key in declared and declared[key] != entry.get(key):
                        problems.append(f"{rel}: manifest {key}={declared[key]} but scan {entry.get(key)}")
            problems += [f"{rel}: {p}" for p in scan["problems"]]
            segments.append(entry)
        elif is_data_file(rel, "funding"):
            scan = scan_funding(root / rel)
            entry = {"file": rel, "sha256": sha256_file(root / rel), **scan}
            declared = manifest_entry((root / rel).parent, Path(rel).name)
            if declared and declared.get("sha256") not in (None, entry["sha256"]):
                problems.append(f"{rel}: manifest sha256 differs")
            problems += [f"{rel}: {p}" for p in scan["problems"]]
            funding.append(entry)
    segments.sort(key=lambda s: s.get("first_ms") or 0)
    joins = []
    for a, b in zip(segments, segments[1:]):
        step = b["first_ms"] - a["last_ms"]
        joins.append({"after": a["file"], "before": b["file"], "missing_minutes": step // MINUTE - 1})
        if step <= 0:
            problems.append(f"{a['file']} overlaps {b['file']}")
    funding.sort(key=lambda f: f.get("first_ms") or 0)
    result = {"schema": "team-data-catalog/v1", "instrument": inst, "created_at": now_utc(), "root": str(root).replace("\\", "/"),
              "segments": segments, "joins": joins, "funding": funding,
              "coverage": {"first_open_utc": segments[0]["first_open_utc"] if segments else None,
                           "last_open_utc": segments[-1]["last_open_utc"] if segments else None,
                           "rows": sum(s.get("rows", 0) for s in segments),
                           "missing_minutes_inside_segments": sum(s.get("missing_minutes", 0) for s in segments),
                           "missing_minutes_between_segments": sum(max(0, j["missing_minutes"]) for j in joins)},
              "problems": problems}
    write_json(root / CATALOG, result)
    if not (root / SUMS).is_file():
        write_json(root / SUMS, {"schema": "team-data-sums/v1", "created_at": now_utc(), "mirrored_from": None,
                                 "method": "hashes of files fetched in place by tools/public_history.py",
                                 "files": {rel: {"bytes": (root / rel).stat().st_size, "sha256": sha256_file(root / rel)}
                                           for rel in files_under(root)}})
    return result


def verify(root):
    root, problems = Path(root), []
    try:
        sums = json.loads((root / SUMS).read_text(encoding="utf-8"))
        stored = json.loads((root / CATALOG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"{SUMS} or {CATALOG} missing or unreadable"]
    for rel, entry in sums["files"].items():
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
        elif path.stat().st_size != entry["bytes"] or sha256_file(path) != entry["sha256"]:
            problems.append(f"{rel}: bytes differ from {SUMS}")
    extra = sorted(set(files_under(root)) - set(sums["files"]))
    problems += [f"{rel}: not in {SUMS}" for rel in extra]
    problems += stored.get("problems", [])
    for segment in stored["segments"]:
        fresh = scan_candles(root / segment["file"])
        for key in ("rows", "first_ms", "last_ms", "gap_count"):
            if fresh.get(key) != segment.get(key):
                problems.append(f"{segment['file']}: {key} now {fresh.get(key)}, catalogue {segment.get(key)}")
    return problems


# ----------------------------------------------------------------------------------------------------------- loading
def read_catalog(root):
    return json.loads((Path(root) / CATALOG).read_text(encoding="utf-8"))


def load_candles(root, start_ms, end_ms):
    """{open_ms: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]} (OKX response order, original strings) for
    start_ms <= open_ms < end_ms, read only from catalogue segments that overlap the range. Index and mark-price
    stores give [ts, o, h, l, c, confirm]."""
    rows = {}
    for segment in read_catalog(root)["segments"]:
        if segment["last_ms"] < start_ms or segment["first_ms"] >= end_ms:
            continue
        with (Path(root) / segment["file"]).open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            next(reader)
            for row in reader:
                stamp = int(row[0])
                if stamp >= end_ms:
                    break
                if stamp >= start_ms:
                    rows[stamp] = [row[0], *row[2:]]  # drop only the readable utc column
    return rows


def load_funding(root):
    """[(funding_time_ms, realized_rate or None)] from every catalogued funding file, sorted, later files win."""
    found = {}
    for entry in read_catalog(root)["funding"]:
        with (Path(root) / entry["file"]).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                found[int(row["funding_time_ms"])] = row["realized_rate"] or None
    return sorted(found.items())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mirror")
    m.add_argument("--src", required=True)
    m.add_argument("--dst", required=True)
    c = sub.add_parser("catalog")
    c.add_argument("--root", required=True)
    c.add_argument("--inst", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    if args.command == "mirror":
        print(json.dumps(mirror(args.src, args.dst), ensure_ascii=False))
        return 0
    if args.command == "catalog":
        result = catalog(args.root, args.inst)
        print(json.dumps({"segments": len(result["segments"]), "funding_files": len(result["funding"]),
                          "coverage": result["coverage"], "problems": result["problems"]}, ensure_ascii=False))
        return 1 if result["problems"] else 0
    problems = verify(args.root)
    print(json.dumps({"ok": not problems, "problems": problems[:50]}, ensure_ascii=False))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
