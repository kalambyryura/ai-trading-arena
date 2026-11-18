"""Optional shared public H1/H4 context for the PAPER G1 pilot only."""
import hashlib
import json
import os
from pathlib import Path
from itertools import islice
import stat
import time
from urllib.parse import urlencode
import urllib.request

from .arena_market import PublicFeed, BASE
from .arena_strategies import ArenaError
from .causal_htf import ClockSample, Page, Request, HTFProvider, attach_context, digest, validate_document, FEATURES


class HTFPublicFeed(PublicFeed):
    def __init__(self, context_path, *, clock=time.time, monotonic=time.monotonic):
        super().__init__()
        self.context_path = Path(context_path)
        self.clock, self.monotonic = clock, monotonic
        self.clock_sample = None
        self.last_refresh_attempt = None
        self.refresh_failure = None
        self.provider = HTFProvider(self.get_page, self.persist)

    def get(self, name):
        before = int(self.monotonic() * 1000)
        result = super().get(name)
        if name == 'clock':
            self.clock_sample = ClockSample(int(result[0]['ts']), int(self.clock() * 1000),
                                            int(self.monotonic() * 1000), before)
        return result

    def get_page(self, request):
        if type(request) is not Request:
            raise ArenaError('htf_request_type')
        params = request.params()  # strict path/bar/limit/instrument allowlist
        if self.calls >= self.max_calls:
            raise ArenaError('public_request_budget')
        self.calls += 1
        req = urllib.request.Request(BASE + request.path + '?' + urlencode(params), method='GET',
                                     headers={'Accept': 'application/json', 'Cache-Control': 'no-cache',
                                              'User-Agent': 'JevPaperHTF/1'})
        with self.opener.open(req, timeout=6) as response:
            raw = response.read(262145)
        local, mono = int(self.clock() * 1000), int(self.monotonic() * 1000)
        if len(raw) > 262144:
            raise ArenaError('htf_response_too_large')
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('code') != '0'
                or not isinstance(data.get('data'), list) or len(data['data']) > request.limit):
            raise ArenaError('htf_response_shape')
        return Page(data['data'], local, mono)

    def persist(self, document):
        validate_document(document)
        data = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        pin = digest(document)
        if self.context_path.is_symlink():
            return False
        self.context_path.mkdir(parents=True, exist_ok=True)
        path = self.context_path / (pin + '.json')
        if path.is_symlink():
            return False
        if path.exists():
            if not path.is_file() or path.stat().st_size != len(data):
                return False
            with path.open('rb') as existing:
                if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                    return False
                return existing.read(len(data) + 1) == data
        if sum(1 for _ in islice(self.context_path.glob('*.json'), 512)) >= 512:
            return False
        with path.open('xb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        return True

    def snapshot(self):
        mono = self.monotonic()
        if self.last_refresh_attempt is None or mono - self.last_refresh_attempt >= 60:
            self.last_refresh_attempt = mono
            try:
                self.get('clock')
                self.provider.refresh(self.clock_sample, int(self.clock() * 1000), int(self.monotonic() * 1000))
                self.refresh_failure = None  # Provider retains its own typed failure, if any.
            except (ValueError, OSError, KeyError, TypeError):
                # A fresh M1 clock must not mask a failed scheduled HTF batch.
                self.refresh_failure = 'FETCH_FAILED'
        snapshot = super().snapshot()
        view = self.provider.view(self.clock_sample, int(snapshot['at'] * 1000), int(self.monotonic() * 1000))
        if self.refresh_failure:
            view = {**view, 'status': 'UNKNOWN', 'reason': self.refresh_failure,
                    'h1_signal_eligible': False, 'features': dict.fromkeys(FEATURES)}
        enriched = attach_context(snapshot, view, self.provider.context)
        if len(json.dumps(enriched, separators=(',', ':'), allow_nan=False).encode()) > 48 * 1024:
            unknown = {**view, 'status': 'UNKNOWN', 'reason': 'CONTEXT_WIRE_BOUND',
                       'context_sha256': None, 'h1_signal_eligible': False, 'features': dict.fromkeys(FEATURES)}
            return attach_context(snapshot, unknown, lambda _: None)
        return enriched
