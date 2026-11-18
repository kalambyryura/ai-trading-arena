#!/usr/bin/env node
// Secret scan before commit/push: every object in the shared Git object database plus the
// working files a commit would add in the current worktree. Never prints a secret value.
// Usage: node team/tools/secret_scan.cjs [--work-only]
// Exit 0 = no real secret found; exit 1 = exact secret value or suspicious key found.
'use strict';
const fs = require('fs');
const path = require('path');
const cp = require('child_process');

const git = (args, opts = {}) => cp.execFileSync('git', args, { maxBuffer: 1024 * 1024 * 1024, ...opts });
const top = git(['rev-parse', '--show-toplevel']).toString().trim();
const commonDir = path.resolve(top, git(['rev-parse', '--git-common-dir'], { cwd: top }).toString().trim());
const MAIN = path.dirname(commonDir); // main checkout: the only place with local secret files
const workOnly = process.argv.includes('--work-only');
// TELEGRAM_BOT_TOKEN (D167, notifier plan check p. 2): the token lives on the other PC only; listed here should one ever appear
const SECRET_KEYS = new Set(['OKX_API_KEY', 'OKX_API_SECRET', 'OKX_API_PASSPHRASE', 'OPENROUTER_API_KEY', 'TELEGRAM_BOT_TOKEN']);

const tokens = new Map(); // value -> label
for (const f of fs.readdirSync(MAIN)) {
  if (!f.startsWith('.env') || f.endsWith('.example')) continue;
  for (const line of fs.readFileSync(path.join(MAIN, f), 'utf8').split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (!m || !SECRET_KEYS.has(m[1])) continue;
    const v = m[2].replace(/^['"]|['"]$/g, '');
    if (v.length >= 6 && !tokens.has(v)) tokens.set(v, `${f}:${m[1]}`);
  }
}
function leaves(obj, p) {
  if (obj === null || obj === undefined) return;
  if (typeof obj === 'object') { for (const k of Object.keys(obj)) leaves(obj[k], `${p}.${k}`); return; }
  const s = String(obj);
  if (s.length >= 8 && /[0-9]/.test(s) && !/^(https?:|[A-Za-z_]+$)/.test(s) && !tokens.has(s)) tokens.set(s, `LocalSettings${p}`);
}
const ls = path.join(MAIN, 'LocalSettings');
if (fs.existsSync(ls)) for (const f of fs.readdirSync(ls)) {
  try { leaves(JSON.parse(fs.readFileSync(path.join(ls, f), 'utf8')), `/${f}`); } catch (e) { /* not JSON */ }
}

const PATTERNS = [
  ['openrouter_key', /sk-or-v1-[A-Za-z0-9]{20,}/],
  ['private_key_block', /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
  ['okx_header_value', /OK-ACCESS-(KEY|SIGN|PASSPHRASE)["']?\s*[:=]\s*["'][A-Za-z0-9+\/=-]{8,}["']/],
  ['bearer_token', /Authorization["']?\s*[:=]\s*["']Bearer\s+[A-Za-z0-9._-]{20,}/],
];
// Env-style assignments: report only values that do not look like placeholders.
const ASSIGN = /(OKX_API_KEY|OKX_API_SECRET|OKX_API_PASSPHRASE|OPENROUTER_API_KEY)[ \t]*=[ \t]*["']?([A-Za-z0-9+\/=_-]{8,})/g;
const PLACEHOLDER = /^(FAKE|fake|MAIN|fictional|fixture|test|dummy|example|placeholder|your|xxx|OKX_API)/;

const hits = [];
function scan(buf, where) {
  const s = buf.toString('latin1');
  for (const [value, label] of tokens) if (s.includes(value)) hits.push({ where, kind: 'EXACT', label });
  const u = buf.toString('utf8');
  for (const [name, re] of PATTERNS) if (re.test(u)) hits.push({ where, kind: 'PATTERN', label: name });
  let m;
  ASSIGN.lastIndex = 0;
  while ((m = ASSIGN.exec(u))) {
    if (!PLACEHOLDER.test(m[2])) hits.push({ where, kind: 'ASSIGN', label: `${m[1]} value len ${m[2].length}` });
  }
}

let objects = 0;
if (!workOnly) {
  const all = cp.spawnSync('git', ['cat-file', '--batch-all-objects', '--batch'], { cwd: top, maxBuffer: 4 * 1024 * 1024 * 1024 }).stdout;
  let i = 0;
  while (i < all.length) {
    const nl = all.indexOf(10, i);
    const [sha, type, size] = all.slice(i, nl).toString().split(' ');
    const body = all.slice(nl + 1, nl + 1 + Number(size));
    if (type === 'blob' || type === 'commit' || type === 'tag') scan(body, `${type}:${sha}`);
    objects++;
    i = nl + 1 + Number(size) + 1;
  }
}
const files = git(['ls-files', '-m', '-o', '--exclude-standard', '-z'], { cwd: top }).toString().split('\0').filter(Boolean);
for (const rel of files) {
  try { scan(fs.readFileSync(path.join(top, rel)), `work:${rel}`); } catch (e) { /* deleted */ }
}
console.log(`secret-scan: ${tokens.size} local secret tokens, ${objects} git objects, ${files.length} working files`);
for (const h of hits) console.log(`${h.kind} ${h.label} @ ${h.where}`);
console.log(hits.length ? `FAIL: ${hits.length} finding(s)` : 'PASS: no secrets found');
process.exit(hits.length ? 1 : 0);
