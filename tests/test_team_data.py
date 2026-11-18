"""Team data store: sha256 mirror, catalogue checks and range loading on tiny temp folders (no network)."""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import team_data

T0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000


def write_segment(folder, start_ms, minutes, skip=(), units=100, name=None):
    folder.mkdir(parents=True, exist_ok=True)
    stamps = [start_ms + 60_000 * i for i in range(minutes) if i not in skip]
    name = name or f"okx_BTC-USDT-SWAP_1m_{team_data.iso(stamps[0])[:10]}_{team_data.iso(stamps[-1])[:10]}.csv"
    with (folder / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(team_data.COLUMNS)
        for i, ts in enumerate(stamps):
            price = 50000 + i
            writer.writerow([ts, team_data.iso(ts), price, price + 5, price - 5, price + 1, 12 * units if i % 7 else 0,
                             "12" if i % 7 else "0", "600000", "1"])
    scan = team_data.scan_candles(folder / name)
    manifest = {"schema": "public-history/v1", "candles": {"file": name, "sha256": team_data.sha256_file(folder / name),
                "rows": scan["rows"], "first_open_utc": scan["first_open_utc"], "last_open_utc": scan["last_open_utc"],
                "gap_count": scan["gap_count"]}}
    (folder / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder / name


class TeamDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mirror_hashes_every_copy_is_idempotent_and_never_overwrites(self):
        src = self.root / "src"
        write_segment(src / "a", T0, 30)
        (src / "a" / "cache").mkdir()
        (src / "a" / "cache" / "x.json").write_text("{}", encoding="utf-8")
        dst = self.root / "dst"
        first = team_data.mirror(src, dst)
        self.assertEqual((first["files"], first["copied"]), (3, 3))
        sums = json.loads((dst / team_data.SUMS).read_text(encoding="utf-8"))
        for rel, entry in sums["files"].items():
            self.assertEqual(team_data.sha256_file(src / rel), entry["sha256"])
            self.assertEqual(team_data.sha256_file(dst / rel), entry["sha256"])
        self.assertTrue((src / "a" / "cache" / "x.json").is_file(), "source is left untouched")
        again = team_data.mirror(src, dst)
        self.assertEqual((again["copied"], again["already_equal"]), (0, 3))
        (dst / "a" / "cache" / "x.json").write_text('{"changed": 1}', encoding="utf-8")
        with self.assertRaises(SystemExit):
            team_data.mirror(src, dst)
        with self.assertRaises(SystemExit):
            team_data.mirror(src, src / "inside")

    def test_catalog_checks_continuity_gaps_units_and_verify_detects_tampering(self):
        store = self.root / "store"
        write_segment(store / "y1", T0, 120, skip={50, 51}, units=10000)
        write_segment(store / "y2", T0 + 120 * 60_000, 60)
        result = team_data.catalog(store, "BTC-USDT-SWAP")
        self.assertEqual(result["problems"], [])
        first, second = result["segments"]
        self.assertEqual((first["rows"], first["gap_count"], first["missing_minutes"]), (118, 1, 2))
        self.assertEqual(first["volume_unit_eras"][0]["contracts_per_base"], 10000)
        self.assertGreater(first["zero_volume_rows"], 0)
        self.assertEqual(result["joins"][0]["missing_minutes"], 0)
        self.assertEqual(result["coverage"]["rows"], 178)
        self.assertEqual(team_data.verify(store), [])
        loaded = team_data.load_candles(store, T0 + 100 * 60_000, T0 + 130 * 60_000)
        self.assertEqual(sorted(loaded)[0], T0 + 100 * 60_000)
        self.assertEqual(len(loaded), 30)
        self.assertEqual(loaded[T0 + 125 * 60_000][8], "1")
        path = store / second["file"]
        path.write_bytes(path.read_bytes().replace(b"50001", b"50002", 1))
        self.assertTrue(any("bytes differ" in p for p in team_data.verify(store)))

    def test_catalog_reports_overlap_bad_rows_and_manifest_disagreement(self):
        store = self.root / "store"
        write_segment(store / "a", T0, 60)
        write_segment(store / "b", T0 + 30 * 60_000, 60)
        self.assertTrue(any("overlaps" in p for p in team_data.catalog(store, "X")["problems"]))
        bad = self.root / "bad"
        path = write_segment(bad / "a", T0, 10)
        text = path.read_text(encoding="utf-8").splitlines()
        text[3] = text[3][:-1] + "0"  # unconfirmed candle
        path.write_text("\n".join(text) + "\n", encoding="utf-8")
        problems = team_data.catalog(bad, "X")["problems"]
        self.assertTrue(any("unconfirmed" in p for p in problems))
        self.assertTrue(any("manifest sha256" in p for p in problems))


class PriceSeriesTests(unittest.TestCase):
    def test_index_candles_are_catalogued_without_volume_and_loaded_as_six_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "BTC-USDT_index" / "1m" / "2025"
            store.mkdir(parents=True)
            name = "okx_BTC-USDT_index-1m_2025-01-01_2025-01-01.csv"
            with (store / name).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(team_data.PRICE_COLUMNS)
                for i in range(20):
                    ts = T0 + 60_000 * i
                    writer.writerow([ts, team_data.iso(ts), 100 + i, 101 + i, 99 + i, 100.5 + i, "1"])
            manifest = {"candles": {"file": name, "sha256": team_data.sha256_file(store / name), "rows": 20, "gap_count": 0}}
            (store / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            root = Path(folder) / "BTC-USDT_index"
            result = team_data.catalog(root, "BTC-USDT index")
            self.assertEqual(result["problems"], [])
            self.assertEqual((result["coverage"]["rows"], result["segments"][0]["volume_unit_eras"]), (20, []))
            self.assertEqual(team_data.verify(root), [])
            rows = team_data.load_candles(root, T0, T0 + 5 * 60_000)
            self.assertEqual(rows[T0], [str(T0), "100", "101", "99", "100.5", "1"])


if __name__ == "__main__":
    unittest.main()
