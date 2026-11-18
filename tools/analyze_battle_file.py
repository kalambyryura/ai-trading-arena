"""Verify a received battle ZIP offline. Never launch models or trades."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trading_lab.battle_export import verify_archive

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_archive(args.archive), ensure_ascii=False))

if __name__ == "__main__":
    main()
