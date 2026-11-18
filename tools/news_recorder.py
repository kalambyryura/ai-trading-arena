"""Forward-only news recorder (D109, D114): polls key-less public feeds and appends what it sees, with honest times.

Every item keeps two times: `published_at` from the feed and `first_seen_at` from this recorder's clock. Research
uses `first_seen_at` as the moment a live bot could have known the item; items published before the recorder's
first start are flagged `before_start` and never used (D109 p. 1). Storage is append-only JSONL per source and UTC
day under data/news/<source>/, each line chained to the previous one of that source by sha256;
`record_sha256` (source, guid, title, summary, link, published_at) is the id a Jev research request records.
Only the title, a short plain-text summary and the link are kept, for internal research; nothing is published.

Polling is polite: one conditional GET (If-None-Match / If-Modified-Since) per source per round, an identified
User-Agent, a size cap, no retry in a row after an error. A heartbeat line per round shows gaps (sleep, network):
items first seen after a gap are flagged `after_gap`; items seen more than 6 h after publication are `late`.
A changed title or summary of a known item is a new `revision` line; the first published_at never changes.

v2 (review of the news text mode): every item's `first_seen_at` is the moment its own fetch returned; the clock check
uses the median skew of all sources' Date headers in the round (one CDN's cached Date no longer flags a source); the
heartbeat line is chained by sha256 and carries, per source, the fetch status and the head of that source's record
chain after the round, so a line inserted, edited or cut later no longer verifies against the heartbeat.

  py -3.12 -X utf8 -B tools/news_recorder.py run                 # until stopped; one instance per data root
  py -3.12 -X utf8 -B tools/news_recorder.py run --once          # one round
  py -3.12 -X utf8 -B tools/news_recorder.py status | verify
"""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_ROOT = Path("data/news")
# D114: four primary sources and two official ones. OKX announcements waits for its official documentation.
SOURCES = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss",  # the canonical form; the slash form answers 308
    "theblock": "https://www.theblock.co/rss.xml",
    "decrypt": "https://decrypt.co/feed",
    "cointelegraph": "https://cointelegraph.com/rss",
    "sec": "https://www.sec.gov/news/pressreleases.rss",
    "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
}
USER_AGENT = "JevArenaNewsRecorder/1.0 (private research; forward-only; polite polling)"
POLL_SECONDS = 120
GAP_SECONDS = 3 * POLL_SECONDS
LATE_SECONDS = 6 * 3600
MAX_BYTES = 3 * 1024 * 1024
MAX_SUMMARY = 1000
SCHEMA = "news-record/v2"
BEAT_SCHEMA = "news-heartbeat/v2"


def now_ms():
    return int(time.time() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain(text, limit):
    """Plain text from feed HTML: tags dropped, entities decoded, whitespace collapsed, trimmed."""
    text = re.sub(r"<[^>]*>", " ", unescape(text or ""))
    return re.sub(r"\s+", " ", unescape(text)).strip()[:limit]


def parse_feed(body):
    """[{guid, title, summary, link, published_at}] from RSS 2.0 (newest first as given). Any DOCTYPE or entity
    declaration is refused by the parser itself, in whatever encoding the feed comes."""
    if b"<!ENTITY" in body or b"<!entity" in body:
        raise ValueError("feed declares XML entities")
    from xml.parsers import expat

    def refuse(*_args):
        raise ValueError("feed declares a DOCTYPE or an entity")
    scan = expat.ParserCreate()  # expat reads the encoding from the XML declaration, so UTF-16 cannot hide a DOCTYPE
    scan.StartDoctypeDeclHandler = refuse
    scan.EntityDeclHandler = refuse
    try:
        scan.Parse(body, True)
    except expat.ExpatError as failure:
        raise ValueError(f"feed is not well-formed XML: {failure}") from None
    root = ET.fromstring(body)
    items = []
    for node in root.iter("item"):
        def text(tag):
            found = node.find(tag)
            return found.text if found is not None and found.text else ""
        link = text("link").strip()
        guid = text("guid").strip() or link
        stamp = text("pubDate").strip()
        published = None
        if stamp:
            try:
                parsed = parsedate_to_datetime(stamp)
                published = int(parsed.timestamp() * 1000) if parsed.tzinfo else None
            except (TypeError, ValueError):
                published = None
        if guid:
            items.append({"guid": guid[:500], "title": plain(text("title"), 300), "summary": plain(text("description"), MAX_SUMMARY),
                          "link": link[:500], "published_at": iso(published) if published is not None else None})
    return items


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Only a redirect to the same host is followed; any other host is an error for this source."""
        from urllib.parse import urljoin, urlsplit
        target = urljoin(req.full_url, newurl)
        if urlsplit(target).scheme != "https" or urlsplit(target).netloc != urlsplit(req.full_url).netloc:
            return None
        return super().redirect_request(req, fp, code, msg, headers, target)


def http_get(url, headers, timeout=20):
    """(status, body bytes, response headers); 304 gives an empty body. One attempt; a redirect is followed only to
    the same https host."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml",
                                                   **headers})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("feed larger than the size cap")
            return response.status, body, dict(response.headers)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return 304, b"", dict(error.headers or {})
        raise


class Recorder:
    def __init__(self, root=DEFAULT_ROOT, sources=None, fetch=http_get, clock=now_ms):
        self.root, self.sources, self.fetch, self.clock = Path(root), dict(sources or SOURCES), fetch, clock
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path, self.state_path = self.root / "RECORDER.json", self.root / "state.json"
        self.meta = json.loads(self.meta_path.read_text(encoding="utf-8")) if self.meta_path.exists() else {}
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"sources": {}}

    def start(self):
        """The first start is the D109 boundary; later starts are listed, never move it."""
        stamp = self.clock()
        self.meta.setdefault("schema", "news-recorder/v1")
        self.meta.setdefault("first_started_at", iso(stamp))
        self.meta.setdefault("starts", []).append(iso(stamp))
        self.meta["sources"] = self.sources
        self.write_json(self.meta_path, self.meta)

    def write_json(self, path, value):
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(path)

    def append(self, name, record):
        source = self.state["sources"].setdefault(name, {"seen": {}, "last_sha256": None})
        record = {"schema": SCHEMA, **record, "prev_sha256": source["last_sha256"]}
        record["line_sha256"] = sha256_text(canonical(record))
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        with (folder / (record["first_seen_at"][:10] + ".jsonl")).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(record) + "\n")
        source["last_sha256"] = record["line_sha256"]

    def round(self):
        """One polite pass over every source; returns the heartbeat written for it. All sources are fetched first,
        so the clock check can use the median skew of the round."""
        started = self.clock()
        last = self.state.get("last_round_at_ms")
        after_gap = last is None or started - last > GAP_SECONDS * 1000
        first_start = int(datetime.fromisoformat(self.meta["first_started_at"].replace("Z", "+00:00")).timestamp() * 1000)
        beat = {"schema": BEAT_SCHEMA, "started_at": iso(started), "after_gap": after_gap and last is not None, "sources": {}}
        fetched = {}
        for name, url in self.sources.items():
            source = self.state["sources"].setdefault(name, {"seen": {}, "last_sha256": None})
            headers = {k: v for k, v in (("If-None-Match", source.get("etag")), ("If-Modified-Since", source.get("modified"))) if v}
            try:
                status, body, reply = self.fetch(url, headers)
            except Exception as failure:  # no retry in a row: the next round tries again
                code = getattr(failure, "code", None)
                fetched[name] = (None, None, {}, self.clock(), type(failure).__name__ + (f" {code}" if code else ""))
                continue
            fetched[name] = (status, body, reply, self.clock(), None)
        skews = {}
        for name, (status, body, reply, at, error) in fetched.items():
            if reply.get("Date"):
                try:
                    skews[name] = round((at - parsedate_to_datetime(reply["Date"]).timestamp() * 1000) / 1000, 1)
                except (TypeError, ValueError):
                    pass
        ordered = sorted(skews.values())
        median = ordered[len(ordered) // 2] if ordered else None
        beat["clock_skew_median_s"], beat["clock_sources"] = median, len(ordered)  # how many Date headers the median saw
        for name in self.sources:
            source = self.state["sources"].setdefault(name, {"seen": {}, "last_sha256": None})
            status, body, reply, stamp, error = fetched[name]
            entry = {"http": status, "fetched_at": iso(stamp), "clock_skew_s": skews.get(name)}
            if error:
                entry["error"] = error
            skew = median
            if status == 200 and body:
                entry["body_sha256"] = hashlib.sha256(body).hexdigest()
                try:
                    items = parse_feed(body)
                except (ET.ParseError, ValueError) as failure:
                    entry["error"] = type(failure).__name__
                    entry["head_sha256"] = source["last_sha256"]
                    beat["sources"][name] = entry
                    continue
                # validators only after a good parse: a broken 200 must not turn later 304s into "covered" (review N2)
                source["etag"], source["modified"] = reply.get("ETag"), reply.get("Last-Modified")
                entry["items"], entry["new"], entry["revisions"], entry["unchanged"] = len(items), 0, 0, 0
                entry["flagged"] = {"before_start": 0, "late": 0, "clock_suspect": 0, "no_published_at": 0}
                published_times = [i["published_at"] for i in items if i["published_at"]]
                entry["newest_published_at"] = max(published_times) if published_times else None  # is the feed itself quiet?
                for item in reversed(items):  # oldest first, so a source's file stays in seen order
                    content = {"source": name, **item}
                    content_sha = sha256_text(canonical(content))
                    known = source["seen"].get(item["guid"])
                    if known and known["content_sha256"] == content_sha:
                        entry["unchanged"] += 1
                        continue
                    published = item["published_at"]
                    published_ms = int(datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp() * 1000) if published else None
                    record = {**content, "record_sha256": known["record_sha256"] if known else content_sha,
                              "content_sha256": content_sha, "revision": known["revision"] + 1 if known else 0,
                              "first_seen_at": iso(stamp), "after_gap": after_gap,
                              "before_start": published_ms is None or published_ms < first_start,
                              "late": published_ms is not None and stamp - published_ms > LATE_SECONDS * 1000,
                              "clock_skew_median_s": skew, "clock_suspect": skew is None or abs(skew) > 60}
                    if known:
                        record["published_at"] = known["published_at"]  # the first publication time never changes
                    self.append(name, record)
                    source["seen"][item["guid"]] = {"record_sha256": record["record_sha256"], "content_sha256": content_sha,
                                                    "revision": record["revision"], "published_at": record["published_at"]}
                    entry["revisions" if known else "new"] += 1
                    for flag in ("before_start", "late", "clock_suspect"):
                        entry["flagged"][flag] += bool(record[flag])
                    entry["flagged"]["no_published_at"] += record["published_at"] is None
            entry["head_sha256"] = source["last_sha256"]  # the anchor: this source's chain head after the round
            beat["sources"][name] = entry
        beat["at"] = iso(self.clock())  # the round is complete: every fetch of it is written
        beat["prev_sha256"] = self.state.get("last_beat_sha256")
        beat["line_sha256"] = sha256_text(canonical(beat))
        self.state["last_round_at_ms"] = started
        self.state["last_beat_sha256"] = beat["line_sha256"]
        self.write_json(self.state_path, self.state)
        with (self.root / "heartbeat.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical(beat) + "\n")
        return beat


def read_heartbeat(root):
    """Heartbeat lines in order; v2 lines are chained among themselves (v1 lines, before the upgrade, are not)."""
    path = Path(root) / "heartbeat.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def verify(root=DEFAULT_ROOT):
    """Re-hash every record and its chain per source, re-hash the v2 heartbeat chain, and check that every head a v2
    heartbeat recorded is in its source's chain, in order; returns problems (empty = intact)."""
    root, problems, counts = Path(root), [], {}
    chains, times = {}, {}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        previous = None
        for path in sorted(folder.glob("*.jsonl")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                record = json.loads(line)
                claimed = record.pop("line_sha256", None)
                if sha256_text(canonical(record)) != claimed:
                    problems.append(f"{path.name}:{number}: line hash")
                if record.get("prev_sha256") != previous:
                    problems.append(f"{path.name}:{number}: chain")
                previous = claimed
                counts[folder.name] = counts.get(folder.name, 0) + 1
                chains.setdefault(folder.name, []).append(claimed)
                times.setdefault(folder.name, []).append(record.get("first_seen_at") if record.get("schema") == SCHEMA else None)
    position = {name: {sha: k for k, sha in enumerate(chain)} for name, chain in chains.items()}
    previous_beat, reached, previous_at, first_head = None, {}, {}, {}
    for number, beat in enumerate(read_heartbeat(root), 1):
        if beat.get("schema") != BEAT_SCHEMA:
            continue
        claimed = beat.pop("line_sha256", None)
        if sha256_text(canonical(beat)) != claimed or beat.get("prev_sha256") != previous_beat:
            problems.append(f"heartbeat.jsonl:{number}: chain")
        previous_beat = claimed
        for name, entry in beat["sources"].items():
            head = entry.get("head_sha256")
            if head is None:
                continue
            at = position.get(name, {}).get(head)
            if at is None or at < reached.get(name, -1):
                problems.append(f"heartbeat.jsonl:{number}: {name} head not in its chain")
                continue
            # the records this beat added were first seen inside its round: after the previous beat, by its end (N1)
            low, high = previous_at.get(name), beat["at"]
            for k in range(reached.get(name, -1) + 1, at + 1):
                seen = times[name][k]
                if seen is None:  # v1 records belong only before the source's first anchored head (review D1)
                    if name in first_head:
                        problems.append(f"heartbeat.jsonl:{number}: {name} record {k + 1} has the v1 schema after the v2 era began")
                        break
                    continue
                if seen > high or (low is not None and seen <= low):
                    problems.append(f"heartbeat.jsonl:{number}: {name} record {k + 1} first seen outside its round")
                    break
            reached[name], previous_at[name] = at, high
            first_head.setdefault(name, at)
    for name, start in first_head.items():  # also v1 records after the newest head, not yet covered by any beat
        later = [k for k in range(reached.get(name, start) + 1, len(times[name])) if times[name][k] is None]
        if later:
            problems.append(f"{name}: record {later[0] + 1} has the v1 schema after the v2 era began")
    return {"ok": not problems, "records": counts, "problems": problems[:50]}


def heads(root=DEFAULT_ROOT):
    """The anchor coord commits outside the store: the newest v2 heartbeat line and its time (review N1)."""
    beats = [b for b in read_heartbeat(root) if b.get("schema") == BEAT_SCHEMA]
    if not beats:
        return None
    return {"at": beats[-1]["at"], "line_sha256": beats[-1]["line_sha256"],
            "heads": {name: entry.get("head_sha256") for name, entry in beats[-1]["sources"].items()}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--once", action="store_true")
    sub.add_parser("status")
    sub.add_parser("verify")
    sub.add_parser("heads")
    args = parser.parse_args(argv)
    if args.command == "heads":
        print(json.dumps(heads(args.root), ensure_ascii=False))
        return 0
    if args.command == "verify":
        print(json.dumps(verify(args.root), ensure_ascii=False))
        return 0
    if args.command == "status":
        meta = args.root / "RECORDER.json"
        beats = (args.root / "heartbeat.jsonl").read_text(encoding="utf-8").splitlines() if (args.root / "heartbeat.jsonl").exists() else []
        print(json.dumps({"recorder": json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else None,
                          "rounds": len(beats), "last": json.loads(beats[-1]) if beats else None}, ensure_ascii=False))
        return 0
    args.root.mkdir(parents=True, exist_ok=True)
    lock = open(args.root / "recorder.lock", "a+")
    try:
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except ImportError:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"refused": "another recorder already runs on this data root"}))
        return 2
    recorder = Recorder(args.root)
    recorder.start()
    while True:
        beat = recorder.round()
        print(canonical({"at": beat["at"], "new": sum(s.get("new", 0) for s in beat["sources"].values()),
                         "errors": [n for n, s in beat["sources"].items() if "error" in s]}), flush=True)
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
