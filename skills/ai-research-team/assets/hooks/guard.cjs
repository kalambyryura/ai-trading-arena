#!/usr/bin/env node
// team-guard: a config-driven PreToolUse hook for a research team of Claude Code sessions
// (ai-research-team skill, references/safety.md).
//
// Claude Code runs this before every tool call that the hook matcher selects and passes the
// call as JSON on stdin. Allow is a silent exit 0, so the normal permission rules still apply.
// Deny is exit 2 with the reason on stderr (default), or a JSON decision on stdout when the
// config says "output": "json". The guard is a tripwire for honest mistakes and casual prompt
// injection, not a sandbox: least-privilege keys, provider credit limits, branch protection and
// the owner's own hands are the real limits.
//
//   node guard.cjs                   hook mode: reads one PreToolUse event from stdin
//   node guard.cjs --explain         prints {"decision","rule","reason","role","profile","tool"}
//   node guard.cjs --check-config    validates the config and lists what is unset
//   node guard.cjs --self-test [-v]  runs the built-in regression cases in a temp folder
//
// Config: guard.config.json next to this file, or the file named by TEAM_GUARD_CONFIG.
// Test overrides: TEAM_WORKTREES_ROOT, TEAM_GUARD_TMP, TEAM_GUARD_HOME.
// Node 18 or newer, no dependencies. Only the lead edits this file and its config.
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const FILE_TOOLS = new Set(['Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Grep', 'Glob']);
const WRITE_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit']);
const DEFAULT_SHELL_TOOLS = { Bash: ['bash'], PowerShell: ['powershell'], mcp__terminal__run_in_terminal: ['bash', 'powershell'] };
const DEFAULT_BROWSER = '^(mcp__claude-in-chrome__|mcp__Claude_Browser__|mcp__computer-use__|WebFetch$)';
const DEFAULT_REAL_BROWSER = '^(mcp__claude-in-chrome__|mcp__computer-use__)';
const MAX_STDIN = 50 * 1024 * 1024;

class Deny extends Error {
  constructor(rule, why) { super(why); this.rule = rule; this.why = why; }
}
class ConfigError extends Error {}
function deny(rule, why) { throw new Deny(rule, why); }

// ---------------------------------------------------------------- paths

const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const isUnset = (v) => v === undefined || v === null || v === '' || (typeof v === 'string' && /^<[^>]*>$/.test(v.trim()));
const isAbsLike = (v) => /^[A-Za-z]:[\\/]/.test(v) || /^[\\/]/.test(v) || /^~([\\/]|$)/.test(v);

function toNative(p, base, home) {
  let s = String(p).trim();
  if (s.startsWith('\\\\?\\')) s = s.slice(4);
  if (process.platform === 'win32' && /^\/[A-Za-z](\/|$)/.test(s)) s = `${s[1]}:${s.slice(2) || '/'}`;
  if (home && (s === '~' || /^~[\\/]/.test(s))) s = path.join(home, s.slice(1));
  return path.resolve(base || process.cwd(), s);
}
function cmpForm(p, ci) {
  let s = String(p).replace(/\\/g, '/');
  s = /^\/+$/.test(s) ? '/' : s.replace(/\/+$/, '');
  if (/^[A-Za-z]:$/.test(s)) s += '/';
  return ci ? s.toLowerCase() : s;
}
function under(p, base) {
  if (!p || !base) return false;
  return p === base || p.startsWith(base.endsWith('/') ? base : `${base}/`);
}
// Claude Code names a session's transcript folder after its start directory, with every
// character that is not a letter or a digit replaced by '-' ([ASSUMPTION] verified on the
// reference project; see references/safety.md).
function sanitizeKey(dir, ci) {
  const s = path.resolve(dir).replace(/[^A-Za-z0-9]/g, '-');
  return ci ? s.toLowerCase() : s;
}
function expand(value, tokens) {
  let unknown = false;
  let missing = false;
  const out = String(value).replace(/\{(\w+)\}/g, (m, name) => {
    if (!Object.prototype.hasOwnProperty.call(tokens, name)) { unknown = true; return m; }
    if (tokens[name] === null || tokens[name] === undefined) { missing = true; return m; }
    return tokens[name];
  });
  if (unknown) return undefined;
  if (missing) return null;
  return out;
}

// Text forms of a path: C:\x, C:/x and the Git Bash /c/x all match, separators repeat freely.
const PATH_END = String.raw`(?=$|[\\/\s"'\x60;&|)<>,\]}])`;
const ABS_START = String.raw`(?<![\w.~-])`;
const REL_START = String.raw`(?<=^|[\\/\s"'=:(<>|;&,])`;
const SEP = String.raw`[\\/]+`;
function absCore(n) {
  const m = /^([a-z]):\/?(.*)$/i.exec(n);
  let head = '';
  let rest = n;
  if (m) { head = `(?:${m[1]}:|/${m[1]})`; rest = m[2]; } else if (n.startsWith('//')) { head = String.raw`[\\/]{2}`; rest = n.slice(2); }
  const segs = rest.split('/').filter(Boolean).map(escapeRe);
  return segs.length ? head + SEP + segs.join(SEP) : head + String.raw`[\\/]`;
}
function relCore(rel) { return rel.split('/').filter(Boolean).map(escapeRe).join(SEP); }

class PathSet {
  constructor(entries, ctx) {
    this.entries = entries;
    this.ctx = ctx;
    const srcs = [];
    for (const e of entries) {
      if (e.kind === 'abs') srcs.push(ABS_START + absCore(e.value) + PATH_END);
      else if (e.kind === 'suffix') srcs.push(REL_START + relCore(e.value) + PATH_END);
      else if (e.kind === 'code') { const s = codeTextSource(ctx); if (s) srcs.push(s); }
      else if (e.kind === 'others') { const s = othersTextSource(ctx); if (s) srcs.push(s); }
    }
    this.source = srcs.map((s) => `(?:${s})`).join('|');
    this.re = this.source ? new RegExp(this.source, ctx.cfg.ci ? 'i' : '') : null;
  }
  get empty() { return this.entries.length === 0; }
  text(s) { return !!this.re && this.re.test(String(s)); }
  // fence (optional): the main checkout in compare form. An absolute entry outside the fence never
  // matches a path inside it, so {tmp} or {home} in a writeAllow list cannot open the main checkout
  // when the repository itself sits in the temp or home folder.
  file(p, fence) {
    if (!p) return false;
    const fenced = !!fence && under(p, fence);
    for (const e of this.entries) {
      if (e.kind === 'abs' && under(p, e.value) && !(fenced && !under(e.value, fence))) return true;
      if (e.kind === 'suffix' && (p === e.value || p.endsWith(`/${e.value}`) || p.includes(`/${e.value}/`))) return true;
      if (e.kind === 'code' && isCodePath(p, this.ctx)) return true;
      if (e.kind === 'others' && isOtherWorktree(p, this.ctx)) return true;
    }
    return false;
  }
}
function codeTextSource(ctx) {
  const roots = [];
  if (ctx.mainCmp) roots.push(absCore(ctx.mainCmp));
  if (ctx.wtRootCmp) roots.push(absCore(ctx.wtRootCmp) + SEP + String.raw`[^\\/\s"'\x60]+`);
  const alts = [];
  if (ctx.cfg.codeDirs.length) alts.push(`(?:${ctx.cfg.codeDirs.map(escapeRe).join('|')})${PATH_END}`);
  if (ctx.cfg.codeExtensions.length) {
    alts.push(String.raw`[^\\/\s"'\x60]+\.(?:${ctx.cfg.codeExtensions.map(escapeRe).join('|')})(?=$|[\s"'\x60;&|)<>,\]}])`);
  }
  if (!roots.length || !alts.length) return '';
  return `${ABS_START}(?:${roots.join('|')})${SEP}(?:${alts.join('|')})`;
}
function othersTextSource(ctx) {
  if (!ctx.wtRootCmp) return '';
  const own = ctx.worktreeCmp && under(ctx.worktreeCmp, ctx.wtRootCmp) && ctx.worktreeCmp !== ctx.wtRootCmp
    ? ctx.worktreeCmp.slice(ctx.wtRootCmp.length + 1).split('/')[0] : '';
  const not = own ? `(?!${escapeRe(own)}` + String.raw`(?:[\\/\s"'\x60;&|)]|$))` : '';
  return ABS_START + absCore(ctx.wtRootCmp) + SEP + not + String.raw`[^\\/\s"'\x60;&|)]+`;
}
function isCodePath(p, ctx) {
  let rel = null;
  if (ctx.wtRootCmp && under(p, ctx.wtRootCmp) && p !== ctx.wtRootCmp) {
    const rest = p.slice(ctx.wtRootCmp.length + 1);
    const i = rest.indexOf('/');
    if (i < 0) return false;
    rel = rest.slice(i + 1);
  } else if (ctx.mainCmp && under(p, ctx.mainCmp) && p !== ctx.mainCmp) {
    rel = p.slice(ctx.mainCmp.length + (ctx.mainCmp.endsWith('/') ? 0 : 1));
  }
  if (!rel) return false;
  const first = rel.split('/')[0];
  if (ctx.codeDirSet.has(ctx.cfg.ci ? first.toLowerCase() : first)) return true;
  return !rel.includes('/') && ctx.cfg.codeExtRe.test(rel);
}
function isOtherWorktree(p, ctx) {
  if (!ctx.wtRootCmp || !under(p, ctx.wtRootCmp) || p === ctx.wtRootCmp) return false;
  return !(ctx.worktreeCmp && under(p, ctx.worktreeCmp));
}
function pathSet(ctx, list) {
  if (ctx.sets.has(list)) return ctx.sets.get(list);
  const entries = [];
  for (const raw of list || []) {
    if (typeof raw !== 'string' || !raw) continue;
    if (raw === '{code}') { entries.push({ kind: 'code' }); continue; }
    if (raw === '{otherWorktrees}') { if (ctx.wtRootCmp) entries.push({ kind: 'others' }); continue; }
    const v = expand(raw, ctx.tokens);
    if (v === null || v === undefined || v === '') continue;
    if (isAbsLike(v)) entries.push({ kind: 'abs', value: cmpForm(toNative(v, ctx.cfg.main, ctx.home), ctx.cfg.ci) });
    else entries.push({ kind: 'suffix', value: cmpForm(v.replace(/^\.[\\/]/, ''), ctx.cfg.ci) });
  }
  const set = new PathSet(entries, ctx);
  ctx.sets.set(list, set);
  return set;
}

// ---------------------------------------------------------------- config

function compileConfig(c, file, rt) {
  const fail = (msg) => { throw new ConfigError(msg); };
  if (!c || typeof c !== 'object' || Array.isArray(c)) fail('the top level must be a JSON object');
  const strArray = (v, name, dflt) => {
    if (v === undefined) return dflt;
    if (!Array.isArray(v) || v.some((x) => typeof x !== 'string')) fail(`${name} must be an array of strings`);
    return v;
  };
  const cfg = { file, warnings: [] };
  cfg.ci = c.caseInsensitivePaths === undefined || c.caseInsensitivePaths === 'auto'
    ? process.platform === 'win32' || process.platform === 'darwin' : c.caseInsensitivePaths === true;
  if (c.output !== undefined && !['exit2', 'json'].includes(c.output)) fail('output must be "exit2" or "json"');
  if (c.onInternalError !== undefined && !['allow', 'deny'].includes(c.onInternalError)) fail('onInternalError must be "allow" or "deny"');
  cfg.output = c.output || 'exit2';
  cfg.onInternalError = c.onInternalError || 'allow';
  cfg.name = typeof c.name === 'string' && c.name ? c.name : 'team-guard';
  cfg.maxCommandChars = Number.isInteger(c.maxCommandChars) && c.maxCommandChars > 0 ? c.maxCommandChars : 100000;
  cfg.maxHeredocs = Number.isInteger(c.maxHeredocs) && c.maxHeredocs >= 0 ? c.maxHeredocs : 64;
  cfg.roleMarker = typeof c.roleMarker === 'string' && c.roleMarker ? c.roleMarker : '.team-role';
  cfg.markerList = [cfg.roleMarker];
  cfg.blockedSuffix = typeof c.blockedSuffix === 'string' ? c.blockedSuffix : '';

  // Named paths: {main} first, then the others, which may refer to {main}, {tmp}, {home} or each other.
  const p = c.paths && typeof c.paths === 'object' && !Array.isArray(c.paths) ? c.paths : {};
  const named = {};
  for (const [k, v] of Object.entries(p)) if (typeof v === 'string') named[k] = v;
  if (isUnset(named.main)) {
    named.main = path.resolve(__dirname, '..', '..');
    cfg.warnings.push(`paths.main is unset; using ${named.main} (two folders above guard.cjs)`);
  }
  const tokens = { tmp: rt.tmp, home: rt.home };
  const mainValue = expand(named.main, tokens);
  if (!mainValue) fail('paths.main must be an absolute path (it may use {home})');
  tokens.main = toNative(mainValue, undefined, rt.home);
  // Relative values in "paths" are relative to {main}.
  for (let pass = 0; pass < 5; pass++) {
    for (const [k, v] of Object.entries(named)) {
      if (Object.prototype.hasOwnProperty.call(tokens, k)) continue;
      if (isUnset(v)) { tokens[k] = null; continue; }
      const e = expand(v, tokens);
      if (e === undefined) continue;
      tokens[k] = e === null ? null : toNative(e, tokens.main, rt.home);
    }
  }
  for (const k of Object.keys(named)) {
    if (!Object.prototype.hasOwnProperty.call(tokens, k)) { tokens[k] = null; cfg.warnings.push(`paths.${k} refers to an unknown token`); }
  }
  if (rt.worktreesRoot) tokens.worktreesRoot = toNative(rt.worktreesRoot, tokens.main, rt.home);
  if (!Object.prototype.hasOwnProperty.call(tokens, 'worktreesRoot')) tokens.worktreesRoot = path.join(tokens.main, '.claude', 'worktrees');
  cfg.tokens = tokens;
  cfg.main = tokens.main;
  cfg.unsetPaths = Object.keys(named).filter((k) => tokens[k] === null);
  cfg.codeDirs = strArray(p.codeDirs, 'paths.codeDirs', ['src', 'tools', 'tests', 'web', 'deployment']);
  cfg.codeExtensions = strArray(p.codeExtensions, 'paths.codeExtensions', ['py', 'js', 'cjs', 'mjs', 'ts', 'ps1', 'sh', 'cmd', 'bat', 'ipynb']);
  cfg.codeExtRe = new RegExp(`\\.(?:${cfg.codeExtensions.map(escapeRe).join('|') || '(?!)'})$`, 'i');
  cfg.denialLog = typeof c.denialLog === 'string' && c.denialLog ? expand(c.denialLog, tokens) || null : null;

  const sp = c.secretPaths && typeof c.secretPaths === 'object' ? c.secretPaths : {};
  const compile = (src, flags, where) => {
    try { return new RegExp(src, flags); } catch (e) { return fail(`${where} is not a valid regular expression`); }
  };
  cfg.secretRes = strArray(sp.patterns, 'secretPaths.patterns', []).map((s, i) => compile(s, 'gi', `secretPaths.patterns[${i}]`));
  cfg.secretExceptions = strArray(sp.exceptions, 'secretPaths.exceptions', []).map((s, i) => compile(s, 'i', `secretPaths.exceptions[${i}]`));
  if (!cfg.secretRes.length) cfg.warnings.push('secretPaths.patterns is empty: no secret file is protected');

  const g = c.git && typeof c.git === 'object' ? c.git : {};
  cfg.pushRemotes = strArray(g.pushRemotes, 'git.pushRemotes', ['origin']);
  cfg.protectedBranches = strArray(g.protectedBranches, 'git.protectedBranches', ['main', 'master']);
  cfg.roleBranchPattern = typeof g.roleBranchPattern === 'string' ? g.roleBranchPattern : '';

  const t = c.tools && typeof c.tools === 'object' ? c.tools : {};
  cfg.shellTools = Object.assign({}, DEFAULT_SHELL_TOOLS, t.shell && typeof t.shell === 'object' ? t.shell : {});
  for (const [k, v] of Object.entries(cfg.shellTools)) {
    if (!Array.isArray(v) || v.some((d) => d !== 'bash' && d !== 'powershell')) fail(`tools.shell.${k} must list "bash" and/or "powershell"`);
  }
  cfg.browserRe = compile(typeof t.browser === 'string' ? t.browser : DEFAULT_BROWSER, '', 'tools.browser');
  cfg.realBrowserRe = compile(typeof t.realBrowser === 'string' ? t.realBrowser : DEFAULT_REAL_BROWSER, '', 'tools.realBrowser');

  // profiles, with extends chains
  const rawProfiles = c.profiles && typeof c.profiles === 'object' ? c.profiles : {};
  if (!Object.keys(rawProfiles).length) fail('profiles must define at least one profile');
  cfg.profiles = {};
  const resolveProfile = (name, seen) => {
    if (cfg.profiles[name]) return cfg.profiles[name];
    const raw = rawProfiles[name];
    if (!raw || typeof raw !== 'object') fail(`profile "${name}" is missing`);
    if (seen.includes(name)) fail(`profile "${name}" extends itself`);
    let parent = null;
    if (raw.extends !== undefined) {
      if (typeof raw.extends !== 'string') fail(`profiles.${name}.extends must be a profile name`);
      parent = resolveProfile(raw.extends, seen.concat(name));
    }
    const merged = Object.assign({}, parent ? parent.fields : {}, raw);
    delete merged.extends;
    for (const key of ['readDeny', 'writeDeny', 'browserAllow']) merged[key] = strArray(merged[key], `profiles.${name}.${key}`, []);
    if (merged.writeAllow !== undefined) merged.writeAllow = strArray(merged.writeAllow, `profiles.${name}.writeAllow`, []);
    const prof = {
      name,
      chain: [name].concat(parent ? parent.chain : []),
      fields: merged,
      integrator: merged.integrator === true,
      gitRestricted: merged.gitRestricted === true,
      realBrowser: merged.realBrowser === true,
      allowAliasedPaths: merged.allowAliasedPaths !== false,
      contentRules: merged.contentRules === true,
      readDeny: merged.readDeny,
      writeDeny: merged.writeDeny,
      writeAllow: merged.writeAllow,
      browserAllowRes: merged.browserAllow.map((s, i) => compile(s, 'gi', `profiles.${name}.browserAllow[${i}]`)),
    };
    cfg.profiles[name] = prof;
    return prof;
  };
  for (const name of Object.keys(rawProfiles)) resolveProfile(name, []);
  const known = (n) => n === '*' || n === '@marked' || n === '@unmarked' || Object.prototype.hasOwnProperty.call(cfg.profiles, n);

  cfg.unmarkedProfile = c.unmarkedProfile || 'lead';
  cfg.unknownRoleProfile = c.unknownRoleProfile || 'staff';
  if (!cfg.profiles[cfg.unmarkedProfile]) fail(`unmarkedProfile "${cfg.unmarkedProfile}" is not a profile`);
  if (!cfg.profiles[cfg.unknownRoleProfile]) fail(`unknownRoleProfile "${cfg.unknownRoleProfile}" is not a profile`);
  cfg.roles = {};
  const roles = c.roles && typeof c.roles === 'object' ? c.roles : {};
  for (const [role, prof] of Object.entries(roles)) {
    if (typeof prof !== 'string' || !cfg.profiles[prof]) fail(`roles.${role} names a missing profile`);
    cfg.roles[role.toLowerCase()] = prof;
  }

  cfg.commandRules = [];
  const rules = c.commandRules === undefined ? [] : c.commandRules;
  if (!Array.isArray(rules)) fail('commandRules must be an array');
  rules.forEach((r, i) => {
    const where = `commandRules[${i}]`;
    if (!r || typeof r !== 'object' || typeof r.id !== 'string' || !r.id) fail(`${where} needs an id`);
    if (typeof r.pattern !== 'string' && r.script === undefined) fail(`${r.id} needs a pattern or a script`);
    const on = strArray(r.on, `${r.id}.on`, ['shell']);
    if (on.some((x) => !['shell', 'browser', 'code'].includes(x))) fail(`${r.id}.on may list only shell, browser and code`);
    const profiles = strArray(r.profiles, `${r.id}.profiles`, ['*']);
    const except = strArray(r.exceptProfiles, `${r.id}.exceptProfiles`, []);
    for (const n of profiles.concat(except)) if (!known(n)) cfg.warnings.push(`${r.id} names an unknown profile "${n}"`);
    const scripts = r.script === undefined ? [] : typeof r.script === 'string' ? [r.script] : strArray(r.script, `${r.id}.script`, []);
    if (r.unless !== undefined) compile(String(r.unless).replace(/\{role\}/g, 'x'), 'i', `${r.id}.unless`);
    if (r.action !== undefined && r.action !== 'deny' && r.action !== 'ask') fail(`${r.id}.action must be "deny" or "ask"`);
    cfg.commandRules.push({
      id: r.id,
      re: typeof r.pattern === 'string' ? compile(r.pattern, 'i', `${r.id}.pattern`) : null,
      scripts,
      unless: typeof r.unless === 'string' ? r.unless : '',
      why: typeof r.why === 'string' && r.why ? r.why : 'blocked by the team rules',
      on,
      profiles,
      exceptProfiles: except,
      action: r.action || 'deny',
    });
  });

  cfg.protectedPaths = [];
  const prot = c.protectedPaths === undefined ? [] : c.protectedPaths;
  if (!Array.isArray(prot)) fail('protectedPaths must be an array');
  prot.forEach((r, i) => {
    if (!r || typeof r !== 'object' || typeof r.id !== 'string' || !r.id) fail(`protectedPaths[${i}] needs an id`);
    const profiles = strArray(r.profiles, `${r.id}.profiles`, ['@marked']);
    const except = strArray(r.exceptProfiles, `${r.id}.exceptProfiles`, []);
    for (const n of profiles.concat(except)) if (!known(n)) cfg.warnings.push(`${r.id} names an unknown profile "${n}"`);
    cfg.protectedPaths.push({
      id: r.id,
      paths: strArray(r.paths, `${r.id}.paths`, []),
      why: typeof r.why === 'string' && r.why ? r.why : 'protected path',
      profiles,
      exceptProfiles: except,
    });
  });
  return cfg;
}

function loadConfig(file, rt) {
  let raw;
  try { raw = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''); } catch (e) { throw new ConfigError(`cannot read ${file}`); }
  let parsed;
  try { parsed = JSON.parse(raw); } catch (e) { throw new ConfigError(`${path.basename(file)} is not valid JSON`); }
  return compileConfig(parsed, file, rt);
}

// ---------------------------------------------------------------- roles

function readMarker(file) {
  const buf = fs.readFileSync(file);
  let text;
  if (buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) text = buf.slice(2).toString('utf16le');
  else if (buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) text = Buffer.from(buf.slice(2)).swap16().toString('utf16le');
  else text = buf.toString('utf8').replace(/^\uFEFF/, '');
  return text.split(/\r?\n/)[0].trim().toLowerCase();
}
function isFile(f) { try { return fs.statSync(f).isFile(); } catch (e) { return false; } }
function markerDir(start, marker) {
  let dir = start;
  for (let i = 0; dir && i < 8; i++) {
    if (isFile(path.join(dir, marker))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return '';
}

function makeContext(ev, cfg, rt) {
  const ci = cfg.ci;
  const cwd = toNative(String(ev.cwd || rt.cwd || process.cwd()), undefined, rt.home);
  const wtRoot = cfg.tokens.worktreesRoot || null;
  let base = '';
  let hardened = false;
  // 1. The transcript folder pins the role for the whole session, even after a cd into the main
  //    checkout (some desktop builds report the main checkout as CLAUDE_PROJECT_DIR in worktrees).
  const parts = String(ev.transcript_path || '').split(/[\\/]/).filter(Boolean).map((x) => (ci ? x.toLowerCase() : x));
  if (parts.length > 1 && wtRoot) {
    let names = [];
    try { names = fs.readdirSync(wtRoot); } catch (e) { names = []; }
    for (const name of names) {
      const dir = path.join(wtRoot, name);
      if (parts.includes(sanitizeKey(dir, ci)) && isFile(path.join(dir, cfg.roleMarker))) { base = dir; break; }
    }
    // A transcript inside the worktrees root without a marker means a renamed folder or a deleted
    // marker: the most restricted profile, never the unmarked one.
    if (!base && parts.some((x) => x.startsWith(`${sanitizeKey(wtRoot, ci)}-`))) hardened = true;
  }
  // 2. Fallbacks: walk up from CLAUDE_PROJECT_DIR, then from the hook cwd.
  if (!base && !hardened) {
    for (const d of [rt.projectDir, cwd]) {
      if (!d) continue;
      const found = markerDir(toNative(d, undefined, rt.home), cfg.roleMarker);
      if (found) { base = found; break; }
    }
  }
  let role = '';
  let profileName = cfg.unmarkedProfile;
  if (hardened) { role = '?'; profileName = cfg.unknownRoleProfile; } else if (base) {
    try { role = readMarker(path.join(base, cfg.roleMarker)); } catch (e) { role = '?'; }
    profileName = cfg.roles[role] || cfg.unknownRoleProfile;
  }
  const profile = cfg.profiles[profileName];
  const tokens = Object.assign({}, cfg.tokens, { worktree: base || null });
  const ctx = {
    cfg,
    cwd,
    home: rt.home,
    role,
    marked: !!(base || hardened),
    worktree: base || null,
    worktreeCmp: base ? cmpForm(base, ci) : '',
    mainCmp: cfg.main ? cmpForm(cfg.main, ci) : '',
    wtRootCmp: wtRoot ? cmpForm(wtRoot, ci) : '',
    profileName,
    profile,
    chain: profile.chain,
    tokens,
    sets: new Map(),
    codeDirSet: new Set(cfg.codeDirs.map((d) => (ci ? d.toLowerCase() : d))),
    ownBranch: base && role && role !== '?' && cfg.roleBranchPattern ? cfg.roleBranchPattern.replace(/\{role\}/g, role) : '',
    ask: null,
  };
  return ctx;
}

function profileMatches(list, except, ctx) {
  const m = (l) => Array.isArray(l) && l.some((n) => n === '*' || (n === '@marked' && ctx.marked) ||
    (n === '@unmarked' && !ctx.marked) || ctx.chain.includes(n));
  return m(list) && !m(except);
}

// ---------------------------------------------------------------- shell text model

// Walk text the way the shell groups it. cb(ch, active): active only for an unquoted, unescaped
// character, so a separator or a comment # cannot hide behind an escaped quote. Bash escapes with
// \ (also inside $'...'), PowerShell with ` and with '' inside single quotes.
function eachShellChar(text, ps, cb) {
  let quote = '';
  let ansiC = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quote) {
      if (ansiC && c === '\\' && i + 1 < text.length) { cb(c, false); cb(text[++i], false); continue; }
      if (ps && quote === "'" && c === "'" && text[i + 1] === "'") { cb(c, false); cb(text[++i], false); continue; }
      if (quote === '"' && (ps ? c === '`' : c === '\\') && i + 1 < text.length) { cb(c, false); cb(text[++i], false); continue; }
      cb(c, false);
      if (c === quote) { quote = ''; ansiC = false; }
      continue;
    }
    if ((ps ? c === '`' : c === '\\') && i + 1 < text.length) { cb(c, false); cb(text[++i], false); continue; }
    if (!ps && c === '$' && text[i + 1] === "'") { cb(c, true); cb(text[++i], false); quote = "'"; ansiC = true; continue; }
    if (c === '"' || c === "'") { quote = c; cb(c, false); continue; }
    cb(c, true);
  }
}
// Split at newlines, ; & and | outside quotes, so a grep alternation a|b stays one segment.
function segments(text, ps) {
  const out = [];
  let cur = '';
  eachShellChar(text, ps, (c, active) => {
    if (active && (c === '\n' || c === ';' || c === '&' || c === '|')) { out.push(cur); cur = ''; } else cur += c;
  });
  out.push(cur);
  return out;
}
function maskQuoted(seg, ps) {
  let out = '';
  eachShellChar(seg, ps, (c, active) => { out += active ? c : ' '; });
  return out;
}
// Shell words with quotes and escapes resolved; separators are their own entries. Inside double
// quotes bash keeps a backslash unless it precedes $ ` " \ or a newline, so "C:\repo\x.py" stays a path.
function shellWords(text, ps) {
  const out = [];
  let value = '';
  let raw = '';
  let quote = '';
  let open = false;
  const flush = () => { if (open) out.push({ value, raw }); value = ''; raw = ''; open = false; };
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    const esc = ps ? c === '`' : c === '\\';
    if (quote) {
      raw += c;
      if (c === quote) quote = '';
      else if (quote === '"' && esc && i + 1 < text.length && (ps || /[$`"\\\n]/.test(text[i + 1]))) { value += text[++i]; raw += text[i]; } else value += c;
      continue;
    }
    if (c === '"' || c === "'") { quote = c; raw += c; open = true; continue; }
    if (esc && i + 1 < text.length) { value += text[++i]; raw += c + text[i]; open = true; continue; }
    if (c === '\n') { flush(); out.push({ sep: c }); continue; }
    if (/\s/.test(c) || (ps && c === ',')) { flush(); continue; }
    if (/[;&|(){}[\]]/.test(c) || (!ps && c === '`')) { flush(); out.push({ sep: c }); continue; }
    value += c;
    raw += c;
    open = true;
  }
  flush();
  return out;
}
const wordsOf = (seg, ps) => shellWords(seg, ps).filter((w) => !w.sep).map((w) => w.value);

// Documentation is data, not a command: heredoc bodies written to a non-script file, PowerShell
// here-strings written with Set-Content/Out-File, comments, and git commit/merge/tag/stash/notes
// messages are masked. Bodies fed to an interpreter or written to a script file stay, and so do
// the $( ) substitutions the shell runs inside unquoted heredocs.
const INTERPRETER = /\b(bash|sh|zsh|dash|ksh|python[\d.]*|py|node|pwsh|powershell|cmd|perl|ruby|iex|Invoke-Expression)\b/i;
const SUBST = /\$\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)|`[^`\n]*`/g;
const HERE_STRING = /@(['"])\r?\n[\s\S]*?\r?\n\1@/g;
const MESSAGE = /(\bgit(?:\s+-\S+)*\s+(?:commit|merge|tag|stash|notes)\b[^;&|\n]*?\s(?:-[a-zA-Z]*m|--message)(?:=|\s+))("(?:[^"\\]|\\.)*"|'[^']*')/;
function keepSubst(body) {
  const found = body.match(SUBST);
  return found ? ` ; ${found.join(' ; ')}` : '';
}
function scanText(cmd, ps) {
  let out = cmd.replace(/<<-?[ \t]*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n([\s\S]*?)\n[ \t]*\2[ \t]*(?=\r?\n|$)/g,
    (match, quote, tag, rest, body, offset, whole) => {
      const before = whole.slice(whole.lastIndexOf('\n', offset) + 1, offset).split(/;|&&|\|\||&/).pop();
      const own = before + rest.split(/;|&&|\|\||&/)[0];
      if (INTERPRETER.test(own) || scriptTarget(own)) return match;
      return `<<${tag} [doc]${quote ? '' : keepSubst(body)}`;
    });
  if (!/\b(Invoke-Expression|iex)\b/i.test(out) && !scriptTarget(out.replace(HERE_STRING, ' '))) {
    out = out.replace(HERE_STRING, (match, quote, offset, whole) => {
      const after = whole.slice(offset + match.length, offset + match.length + 80);
      if (/^\s*\|\s*&?\s*["']?(bash|sh|zsh|dash|ksh|python[\d.]*|py|node|pwsh|powershell|cmd|perl|ruby)\b/i.test(after)) return match;
      return `@[doc]@${quote === '"' ? keepSubst(match) : ''}`;
    });
  }
  out = stripComments(out, ps);
  for (let i = 0; i < 50; i++) {
    let changed = false;
    out = out.replace(MESSAGE, (all, pre, msg) => {
      SUBST.lastIndex = 0;
      if (msg[0] === '"' && SUBST.test(msg) && !/^"\$\(cat <<\w+ \[doc\]\s*\)"$/.test(msg)) return all;
      changed = true;
      return `${pre}[msg]`;
    });
    if (!changed) break;
  }
  return out;
}
// A # that starts a word is a comment to the end of the line, in bash and PowerShell.
// Not after { or $, so ${#var} and $# stay code.
function stripComments(text, ps) {
  let out = '';
  let prev = '';
  let inComment = false;
  eachShellChar(text, ps, (c, active) => {
    if (inComment) {
      if (c === '\n') { inComment = false; out += c; }
      prev = c;
      return;
    }
    if (active && c === '#' && (prev === '' || /[\s;]/.test(prev))) { inComment = true; prev = c; return; }
    out += c;
    prev = c;
  });
  return out;
}

const LEAD = String.raw`^\s*[({]*\s*(&\s*)?["']?`;
const EXEC_SEG = new RegExp(LEAD + String.raw`((bash|sh|zsh|dash|ksh|pwsh|powershell|cmd|eval|exec|source|xargs|iex|Invoke-Expression|Invoke-Command|icm|Start-Process|start|py|python[\d.]*|pythonw|node|perl|ruby|awk|gawk|curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm)(\.exe)?|\.|[^\s"']+\.(ps1|sh|bash|py|cmd|bat))["']?(\s|$)`, 'i');
const DATA_SEG = new RegExp(LEAD + String.raw`(grep|egrep|fgrep|rg|findstr|Select-String|sls|echo|printf|Write-Output|Write-Host|where|which|Get-Command|gcm|Add-Content|ac|Set-Content|sc|Out-File)(\.exe)?(\s|$)`, 'i');
const GIT_READ_SEG = /^\s*[({]*\s*git(\.exe)?\s+(-C\s+("[^"]*"|'[^']*'|\S+)\s+|--no-pager\s+|-P\s+)*(log|show|diff|blame|grep|ls-files|cat-file|shortlog|status|rev-parse|for-each-ref)\b/i;
const LISTING_SEG = /^\s*[({]*\s*(ls|dir|Get-ChildItem|gci|Test-Path|git(\.exe)?\s+(-C\s+("[^"]*"|'[^']*'|\S+)\s+)?(status|check-ignore))(\s|$)/i;
const READ_VERB = /^(cat|type|head|tail|less|more|grep|egrep|fgrep|rg|findstr|Get-Content|gc|Select-String|sls|ls|dir|Get-ChildItem|gci|Test-Path|stat|wc|file|du|tree|realpath|readlink|basename|dirname|test|od|xxd|hexdump|strings|jq|diff|cmp|nl|git|cd|pushd|popd|Set-Location|sl|Resolve-Path|Split-Path|Get-Location|Format-Hex|Compare-Object|find(?![^\n]*\s-(exec|execdir|delete|ok|okdir|fprint\w*|fls)\b)|sha\d*sum|md5sum|cksum|Get-FileHash|Get-Item|gi|Get-ItemProperty|gp|Get-Acl)(\.exe)?(\s|$)/i;
const WRITE_VERB = /^(mv|move|Move-Item|mi|Rename-Item|rni|ren|rename|rm|rmdir|rd|del|erase|Remove-Item|ri|tee|Tee-Object|touch|mkdir|md|ni|New-Item|Set-Content|sc|Add-Content|ac|Out-File|Clear-Content|clc|truncate)$/i;
const COPY_VERB = /^(cp|copy|xcopy|robocopy|Copy-Item|cpi|rsync|install)$/i;
const MOVE_OR_REMOVE = /^(mv|move|Move-Item|mi|Rename-Item|rni|ren|rename|rm|rmdir|rd|del|erase|Remove-Item|ri)$/i;
const OUTPUT_OPTS = /^(--output|--output-document|--out|--out-dir|--outdir|--output-dir|-o|-OutFile|-OutputFile|-FilePath)$/i;

// Text runs nothing only without substitutions: $( ), bash backticks, or PowerShell ( ) outside quotes.
function inert(seg, ps) {
  if (/\$\(/.test(seg) || (!ps && /`/.test(seg))) return false;
  let paren = false;
  eachShellChar(seg, ps, (c, active) => { if (active && c === '(') paren = true; });
  return !paren;
}
function dataSegment(seg, ps) { return DATA_SEG.test(seg) && inert(seg, ps) && !scriptTarget(seg); }
// For grep-like tools the pattern is text, not a file; for echo-like tools only redirect targets count.
function searchOperands(seg) {
  if (/^\s*[({]*\s*(echo|printf|Write-Output|Write-Host)(\.exe)?\s/i.test(seg) && !/\$\(|`|<|\$\{?\w/.test(seg)) {
    return (seg.match(/>>?\s*("[^"]*"|'[^']*'|\S+)/g) || []).join(' ');
  }
  const m = seg.match(/^(\s*[({]*\s*(?:grep|egrep|fgrep|rg|findstr|Select-String|sls)(?:\.exe)?)(\s.*)$/i);
  if (!m) return seg;
  let rest = m[2].replace(/\s(-e|--regexp|-Pattern|-SimpleMatch)(=|\s+)("[^"]*"|'[^']*'|\S+)/gi, ' $1 [pat]');
  if (!/\[pat\]/.test(rest)) rest = rest.replace(/^((?:\s+-\S+)*)\s+("[^"]*"|'[^']*'|[^\s-]\S*)/, '$1 [pat]');
  return m[1] + rest;
}
const WRITE_OP = />>?|\btee\b|\b(Set-Content|Add-Content|Out-File|New-Item)\b/i;
const SCRIPT_EXT = /\.(sh|bash|zsh|ps1|psm1|py|pyw|js|cjs|mjs|cmd|bat)\b/i;
function scriptTarget(text) {
  for (const seg of String(text).split(/[\n|;&]/)) {
    const m = WRITE_OP.exec(seg);
    if (m && SCRIPT_EXT.test(seg.slice(m.index))) return true;
  }
  return false;
}
const SHORT_NAME = /[\\/][^\\/\s"']{1,6}~\d([\\/\s"']|$)/;
// Links, junctions, drive substitutions and 8.3 short names can reach a denied folder under
// another name, so restricted roles may not create or use them.
function aliasedPath(text, ps) {
  if (SHORT_NAME.test(text)) return true;
  for (const seg of segments(text, ps)) {
    if (/\b(mklink|New-PSDrive)\b/i.test(seg)) return true;
    const ws = wordsOf(seg, ps);
    const { i, verb } = leadingVerb(ws);
    if (/^(ln|subst|ndr)$/i.test(verb)) return true;
    if (/^fsutil$/i.test(verb) && /^hardlink$/i.test(ws[i + 1] || '')) return true;
    if (/^(New-Item|ni)$/i.test(verb) && /-ItemType\s+["']?(Junction|SymbolicLink|HardLink)/i.test(seg)) return true;
  }
  return false;
}
function encodedPowershell(segs, ps) {
  const PS_BIN = /(^|[\\/])(pwsh|powershell)(\.exe)?$/i;
  const ENC_FLAG = /^-(e|ec|en|enc|encodedcommand)$/i;
  const ENC_MASKED = /(^|[\s;&|(])(pwsh|powershell)(\.exe)?(\s[^\n]*?)?\s-(e|ec|en|enc|encodedcommand)\b/i;
  for (const seg of segs) {
    const words = shellWords(seg, ps);
    let i = 0;
    while (i < words.length && (words[i].sep || (!ps && /^[A-Za-z_]\w*=/.test(words[i].value)))) i++;
    if (i >= words.length || !PS_BIN.test(words[i].value)) continue;
    if (ENC_MASKED.test(maskQuoted(seg, ps))) return true;
    for (let j = i + 1; j < words.length && !words[j].sep; j++) if (ENC_FLAG.test(words[j].value)) return true;
  }
  return false;
}
function runsScript(cmd, name) {
  const n = escapeRe(name);
  const invoked = new RegExp(String.raw`(^|[\s;&|(])(py|python[\d.]*|pythonw|node|bash|sh|pwsh|powershell)(\.exe)?\s[^\n|;&]*?(${n}\.(py|js|cjs|mjs|sh|ps1)\b|-m\s+([\w.]+\.)?${n}\b)`, 'i');
  if (invoked.test(cmd)) return true;
  const reads = /[^\n|;&]*\b(grep|egrep|fgrep|rg|findstr|Select-String|sls|git|cat|head|tail|less|more|Get-Content|gc|type|wc|sha\d*sum|Get-FileHash|diff|ls|dir)\b[^\n|;&]*/gi;
  return new RegExp(String.raw`(^|[\s;&|(]|[\\/])${n}\.(py|js|cjs|mjs|sh|ps1)(\s|$)`, 'i').test(cmd.replace(reads, ''));
}

// Words that name paths, resolved against the hook cwd (options keep only their =value).
function resolveWord(w, ctx) {
  let s = String(w);
  if (s.startsWith('-')) {
    const eq = s.indexOf('=');
    if (eq < 0) return '';
    s = s.slice(eq + 1);
  }
  if (!s || s.length > 4096 || /[\n\0$]/.test(s) || /^[a-z][a-z0-9+.-]*:\/\//i.test(s)) return '';
  try { return cmpForm(toNative(s, ctx.cwd, ctx.home), ctx.cfg.ci); } catch (e) { return ''; }
}
function names(text, m, ps, ctx) {
  if (m.text(text)) return true;
  return wordsOf(text, ps).some((w) => m.file(resolveWord(w, ctx)));
}
function redirectTargets(seg, ps) {
  const masked = maskQuoted(seg, ps);
  const out = [];
  for (let i = 0; i < masked.length; i++) {
    if (masked[i] !== '>') continue;
    let j = i + 1;
    if (masked[j] === '>' || masked[j] === '|') j++;
    while (j < seg.length && (seg[j] === ' ' || seg[j] === '\t')) j++;
    if (seg[j] === '&') { i = j; continue; }
    const m = /^("[^"]*"|'[^']*'|[^\s|;&<>()]+)/.exec(seg.slice(j));
    if (!m) continue;
    const t = m[1].replace(/^["']|["']$/g, '');
    if (!/^(\/dev\/null|\$null|nul)$/i.test(t)) out.push(t);
    i = j + m[1].length - 1;
  }
  return out;
}
function optionTargets(words) {
  const out = [];
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    const eq = w.indexOf('=');
    if (eq > 0 && OUTPUT_OPTS.test(w.slice(0, eq))) out.push(w.slice(eq + 1));
    else if (OUTPUT_OPTS.test(w) && i + 1 < words.length) out.push(words[i + 1]);
  }
  return out;
}
function gitSubcommand(words) {
  let j = words.findIndex((w) => /(^|[\\/])git(\.exe)?$/i.test(w));
  if (j < 0) return '';
  for (j++; j < words.length && words[j].startsWith('-'); j++) if (GIT_VALUE_OPTS.has(words[j])) j++;
  return (words[j] || '').toLowerCase();
}
function leadingVerb(words) {
  let i = 0;
  while (i < words.length && (/^(xargs|sudo|exec|command|nohup|time|env)$/i.test(words[i]) || /^[A-Za-z_]\w*=/.test(words[i]))) i++;
  return { i, verb: (words[i] || '').replace(/^.*[\\/]/, '').replace(/\.exe$/i, '') };
}

// Strict: a redirect into the target, or any command naming it that is not a plain read.
function writesStrict(cmd, m, ps, ctx) {
  for (const seg of segments(cmd, ps)) {
    const ws = wordsOf(seg, ps);
    const hit = (w) => m.text(w) || m.file(resolveWord(w, ctx));
    if (!m.text(seg) && !ws.some(hit)) continue;
    if (redirectTargets(seg, ps).some(hit)) return true;
    const lead = seg.replace(/^[\s({&]+/, '');
    if (!READ_VERB.test(lead)) return true;
    if (/^git(\.exe)?\s/i.test(lead) && /^(apply|am|checkout|restore|mv|rm)$/.test(gitSubcommand(ws))) return true;
  }
  return false;
}
// Precise (best effort, for profile writeDeny): redirects, sed -i, write verbs naming the target,
// copies into it, git apply|am|checkout|restore|mv|rm naming it, output options, inline code writes.
// A path held in a variable escapes this check.
function writesPrecise(cmd, m, ps, ctx) {
  for (const seg of segments(cmd, ps)) {
    const ws = wordsOf(seg, ps);
    const hit = (w) => m.text(w) || m.file(resolveWord(w, ctx));
    if (!m.text(seg) && !ws.some(hit)) continue;
    if (redirectTargets(seg, ps).some(hit)) return true;
    const { i, verb } = leadingVerb(ws);
    const rest = ws.slice(i + 1);
    const operands = rest.filter((w) => !w.startsWith('-') && !(process.platform === 'win32' && /^\/[A-Za-z?]+(:\S*)?$/.test(w)));
    if (/^sed$/i.test(verb) && rest.some((w) => /^-[A-Za-z]*i/.test(w) || w.startsWith('--in-place')) && operands.some(hit)) return true;
    if (WRITE_VERB.test(verb) && operands.some(hit)) return true;
    if (COPY_VERB.test(verb) && operands.length) {
      const dest = /^robocopy$/i.test(verb) ? operands[Math.min(1, operands.length - 1)] : operands[operands.length - 1];
      if (hit(dest)) return true;
    }
    if (/^git$/i.test(verb) && /^(apply|am|checkout|restore|mv|rm)$/.test(gitSubcommand(ws.slice(i))) && operands.some(hit)) return true;
    if (optionTargets(rest).some(hit)) return true;
  }
  return codeWritesInto(cmd, m);
}
// <PATH> stands for the protected path; bounded gaps ({0,400}) keep these linear, because a hook
// timeout would let the call through.
const CODE_WRITE_TEMPLATES = [
  String.raw`\bopen\s*\(\s*[^)\n]{0,400}?<PATH>[^)\n]{0,400}?,\s*(?:mode\s*=\s*)?[rbu]?['"][^'"\n]{0,8}[wax+]`,
  String.raw`\bPath\s*\(\s*[^)\n]{0,400}?<PATH>[^)\n]{0,400}?\)\s*\.\s*(?:write_text|write_bytes|touch|mkdir|unlink|rename|replace|rmdir)`,
  String.raw`(?:shutil\s*\.\s*(?:copy\w*|move)|os\s*\.\s*(?:rename|replace))\s*\([^,\n]{0,400},\s*[^)\n]{0,400}?<PATH>`,
  String.raw`(?:os\s*\.\s*(?:remove|unlink|rmdir|makedirs|mkdir)|shutil\s*\.\s*rmtree)\s*\(\s*[^)\n]{0,400}?<PATH>`,
  String.raw`\b(?:writeFileSync|appendFileSync|writeFile|appendFile|createWriteStream|mkdirSync|rmSync|unlinkSync|rmdirSync)\s*\(\s*[^)\n]{0,400}?<PATH>`,
  String.raw`\b(?:renameSync|copyFileSync|cpSync)\s*\([^,\n]{0,400},\s*[^)\n]{0,400}?<PATH>`,
  String.raw`\.to_(?:csv|parquet|json|pickle|feather|hdf|excel)\s*\(\s*[^)\n]{0,400}?<PATH>`,
  String.raw`\[(?:System\.)?IO\.(?:File|Directory)\]::(?:Write\w*|Append\w*|Create\w*|Delete|Move|Copy|Replace)\s*\([^)\n]{0,400}?<PATH>`,
];
function codeWritesInto(text, m) {
  if (!m.source) return false;
  if (!m.codeRe) m.codeRe = new RegExp(CODE_WRITE_TEMPLATES.map((t) => t.split('<PATH>').join(`(?:${m.source})`)).join('|'), m.ctx.cfg.ci ? 'i' : '');
  return m.codeRe.test(text);
}

// ---------------------------------------------------------------- built-in rails

const GIT_FILE = /(^|[\\/\s"'=~])(\.git[\\/](config|hooks|remotes|branches|refs([\\/]|\b)|packed-refs|HEAD\b|info[\\/]grafts|objects[\\/]info[\\/]alternates|worktrees[\\/][^\\/\s"']+[\\/](config|HEAD|commondir|gitdir))|\.git(?=$|["'\s;&|)<>])|\.gitconfig\b|\.config[\\/]git[\\/]|etc[\\/]gitconfig\b|\.bashrc\b|\.bash_profile\b|\.bash_login\b|\.profile\b|\.zshrc\b|\.zprofile\b|\.zshenv\b)|[\w.]*_profile\.ps1\b/i;
const GIT_FILE_MATCHER = { text: (s) => GIT_FILE.test(s), file: (p) => GIT_FILE.test(p), source: '' };
const GIT_ENV_NAMES = String.raw`CONFIG\w*|DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|REPLACE_REF_BASE|NAMESPACE|SSH\w*|ASKPASS|EXEC_PATH|TEMPLATE_DIR|PROXY_COMMAND|ALLOW_PROTOCOL|PROTOCOL_FROM_USER|TRACE\w*|CURL_VERBOSE|SSL\w*|HTTP_\w+|REDACT\w*|CEILING_DIRECTORIES|DISCOVERY_ACROSS_FILESYSTEM`;
const GIT_ENV = new RegExp(String.raw`(\$env:|^|[\s;&|(])GIT_(${GIT_ENV_NAMES})\s*=|\b(setx\s+|(Set-Item|New-Item|si|ni)\s+(-Path\s+)?env:)GIT_(${GIT_ENV_NAMES})\b|SetEnvironmentVariable\(\s*["']GIT_`, 'i');
const CRED_STORE = /\b(cmdkey|vaultcmd)(\.exe)?\b|PasswordVault|\bCredRead|Get-StoredCredential|\bCredentialManager\b/i;
// base64 -d (or --decode) in one pipe segment and a shell later on the same line; linear on purpose.
function decodeRun(text) {
  if (/FromBase64String/i.test(text)) return true;
  for (const line of text.split('\n')) {
    let decoding = false;
    for (const part of line.split('|')) {
      if (decoding && /^\s*(?:ba|z|da|k)?sh\b/i.test(part)) return true;
      if (/\bbase64\b/i.test(part) && /\s(?:-d|--decode)\b/i.test(part)) decoding = true;
    }
  }
  return false;
}
const PROXY = /(^|[\s;&|(])(https?|all)_proxy\s*=|\$env:(https?|all)_proxy\s*=|\bsetx\s+(https?|all)_proxy\b/i;
const GIT_BIN = /(^|[\\/])git(\.exe)?$/i;
const GIT_LOWLEVEL_BIN = /(^|[\\/])git-(send-pack|http-push|receive-pack|credential[\w-]*|remote-[\w-]+)(\.exe)?$/i;
const GIT_VALUE_OPTS = new Set(['-c', '-C', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env', '--attr-source', '--list-cmds']);
const PUSH_SAFE_GLOBALS = new Set(['-C', '-P', '--no-pager', '-p', '--paginate', '--no-optional-locks']);
const PUSH_DANGER = ['force', 'force-with-lease', 'force-if-includes', 'mirror', 'delete', 'prune', 'receive-pack', 'exec', 'repo'];
const PUSH_WIDE = ['all', 'branches', 'tags', 'follow-tags'];
const UNSAFE_PUSH = 'force push, mirror, prune and remote ref deletion are forbidden';
const INLINE_DANGER = /^\s*(alias|core\.(hookspath|sshcommand|fsmonitor|askpass|gitproxy|editor|pager)|credential|include|includeif|url|remote|protocol)\b/i;
const GIT_SAFE_UNDER_XARGS = new Set(['status', 'log', 'diff', 'show', 'ls-files', 'rev-parse', 'blame', 'grep', 'for-each-ref',
  'check-ignore', 'merge-base', 'cat-file', 'shortlog', 'describe', 'rev-list', 'ls-tree', 'name-rev']);

function checkGit(cmd, ps, ctx) {
  scanShell(cmd, ps, 0, (call) => checkGitCall(call, ctx));
  if (GIT_ENV.test(cmd)) deny('git.env', 'git environment overrides (GIT_CONFIG*, GIT_DIR, GIT_SSH*, GIT_TRACE*, ...) are owner-only');
  if (CRED_STORE.test(cmd)) deny('git.credentials', 'stored credentials are owner-only');
  if (/["'`]git(\.exe)?["'`]\s*,\s*\[?\s*["'`]push["'`]/i.test(cmd)) deny('git.indirect', 'git push from code is forbidden; run git push directly');
  if (/\b(alias\s+[\w-]+=\s*["']?|(Set|New)-Alias\s+(-Name\s+)?\S+\s+(-Value\s+)?)git\b/i.test(cmd)) deny('git.indirect', 'shell aliases for git are forbidden');
}
// Words that still hold spaces or shell syntax are scanned again as commands in both shells:
// bash -c "...", pwsh -Command '...', "$(...)", subprocess strings.
function scanShell(text, ps, depth, onCall) {
  const words = shellWords(text, ps);
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (w.sep) continue;
    if (GIT_LOWLEVEL_BIN.test(w.value)) deny('git.lowlevel', 'low-level git push and credential programs are forbidden');
    const next = words[i + 1];
    if (w.raw.includes('$') && next && !next.sep && next.value.toLowerCase() === 'push') deny('git.indirect', 'git must be called by name, not through a variable');
    if (!GIT_BIN.test(w.value)) continue;
    const opts = [];
    let j = i + 1;
    while (j < words.length && !words[j].sep && words[j].value.startsWith('-')) {
      const word = words[j].value;
      const eq = word.indexOf('=');
      const name = eq > 0 ? word.slice(0, eq) : word;
      let value = eq > 0 ? word.slice(eq + 1) : '';
      if (eq < 0 && GIT_VALUE_OPTS.has(name) && j + 1 < words.length && !words[j + 1].sep) value = words[++j].value;
      opts.push({ name, value });
      j++;
    }
    if (j >= words.length || words[j].sep) continue;
    const args = [];
    let k = j + 1;
    while (k < words.length && !words[k].sep) args.push(words[k++]);
    onCall({ opts, sub: words[j].value.toLowerCase(), args, end: k < words.length ? words[k].sep : '', ps });
    i = k;
  }
  if (depth < 3) {
    for (const w of words) {
      if (!w.sep && /[\s;&|(){}[\],`$]/.test(w.value)) {
        scanShell(w.value, false, depth + 1, onCall);
        scanShell(w.value, true, depth + 1, onCall);
      }
    }
  }
}
function checkGitCall(call, ctx) {
  const { opts, sub, args } = call;
  const vals = args.map((a) => a.value);
  const first = (vals.find((v) => !v.startsWith('-')) || '').toLowerCase();
  for (const o of opts) {
    if ((o.name === '-c' || o.name === '--config-env') && INLINE_DANGER.test(o.value)) {
      deny('git.config', 'inline git config for aliases, hooks, credentials or remotes is forbidden');
    }
  }
  if (sub === 'push') { checkPush(call, ctx); return; }
  if (['send-pack', 'http-push', 'receive-pack'].includes(sub) || sub.startsWith('remote-')) deny('git.lowlevel', `git ${sub} is forbidden`);
  if (['filter-branch', 'filter-repo', 'replace', 'fast-import'].includes(sub)) deny('git.history', `git ${sub} rewrites history and is owner-only`);
  if (sub.startsWith('credential')) deny('git.credentials', 'git credential helpers print tokens; they are owner-only');
  if (sub === 'subtree' && first === 'push') deny('git.indirect', 'git subtree push is forbidden; push branches with git push');
  if (sub === 'remote' && /^(add|set-url|rename|remove|rm|set-branches|set-head)$/.test(first)) deny('git.remote', 'changing git remotes is owner-only');
  if (sub === 'config') checkConfig(vals, first);
  if ((sub === 'init' || sub === 'clone') && vals.some((v) => /^--(template|separate-git-dir|config)\b/.test(v) || v === '-c')) {
    deny('git.config', 'git init or clone with templates, a separate git dir or config is owner-only');
  }
  if (sub === 'reflog' && /^(expire|delete)$/.test(first)) deny('git.history', 'deleting reflog history is forbidden');
  if (sub === 'symbolic-ref' && (vals.filter((v) => !v.startsWith('-')).length >= 2 || vals.some((v) => /^(-d|--delete)$/.test(v)))) {
    deny('git.history', 'changing symbolic refs is owner-only');
  }
  if (sub === 'reset' && vals.includes('--hard')) deny('git.destructive', 'git reset --hard is forbidden; it discards work');
  if (sub === 'clean' && vals.some((v) => /^-[A-Za-z]*f/.test(v) || v === '--force')) deny('git.destructive', 'git clean -f is forbidden; it deletes untracked work');
  if ((sub === 'checkout' || sub === 'restore') && (vals[0] === '.' || (vals[0] === '--' && vals[1] === '.'))) {
    deny('git.destructive', 'discarding all working-tree changes is forbidden');
  }
  if (!ctx.profile.gitRestricted) return;
  const refs = 'only the integrator manages branches, tags and refs';
  if (sub === 'update-ref') deny('git.refs', refs);
  if (sub === 'branch' && vals.some((v) => /^-[A-Za-z]*[fdDmMcC]/.test(v) || /^--(force|delete|move|copy)\b/.test(v))) deny('git.refs', refs);
  if (sub === 'tag' && !tagListing(vals)) deny('git.refs', refs);
  if ((sub === 'checkout' && vals.some((v) => /^-[A-Za-z]*B/.test(v))) ||
      (sub === 'switch' && vals.some((v) => /^-[A-Za-z]*C/.test(v) || v === '--force-create'))) deny('git.refs', refs);
  if (sub === 'worktree' && first !== 'list') deny('git.refs', 'only the lead creates, moves or removes worktrees');
  if (sub === 'fetch' && vals.some((v) => !v.startsWith('-') && /[:+]/.test(v))) deny('git.refs', 'fetch without refspecs; the integrator updates shared branches');
}
function tagListing(vals) {
  if (vals.some((v) => /^(-[dfasumFe]|--(delete|force|annotate|sign|local-user|message|file|edit)\b)/.test(v))) return false;
  if (vals.some((v) => /^(-l|--list|-v|--verify|--contains|--no-contains|--points-at|--merged|--no-merged|-n\d*)\b/.test(v))) return true;
  return !vals.some((v) => !v.startsWith('-'));
}
function checkConfig(vals, first) {
  if (vals.some((v) => /^(--get(-all|-regexp|-urlmatch|-color|-colorbool)?|--list|-l|-h|--help)$/.test(v)) || /^(get|list)$/.test(first)) return;
  const writes = vals.some((v) => /^(--add|--replace-all|--unset(-all)?|--rename-section|--remove-section|-e|--edit)$/.test(v)) ||
    /^(set|unset|edit|rename-section|remove-section)$/.test(first);
  const operands = [];
  for (let i = 0; i < vals.length; i++) {
    if (/^(--file|-f|--blob|--type|--default|--comment)$/.test(vals[i])) { i++; continue; }
    if (!vals[i].startsWith('-')) operands.push(vals[i]);
  }
  if (!writes && operands.length === 1 && /^[\w-]+(\.[^\s=]+)?\.[\w-]+$/.test(operands[0])) return;
  deny('git.config', 'changing git config is owner-only; read it with git config --get or --list');
}
function checkPush(call, ctx) {
  const { opts, args, end, ps } = call;
  const restricted = ctx.profile.gitRestricted;
  for (const o of opts) if (!PUSH_SAFE_GLOBALS.has(o.name)) deny('git.push-options', `git ${o.name} with push is forbidden; push plainly with git push <remote> <branch>`);
  if (['(', '{', '[', '`'].includes(end)) deny('git.push-literal', 'git push arguments must be literal');
  const positional = [];
  let endOfOpts = false;
  for (let i = 0; i < args.length; i++) {
    const v = args[i].value;
    const raw = args[i].raw;
    if (/^[*\d]?(>>?|<)/.test(v)) { if (/^[*\d]?(>>?|<)$/.test(v)) i++; continue; }
    if (raw.includes('$') || (!ps && raw.includes('`'))) deny('git.push-literal', 'git push arguments must be literal (no variables or substitution)');
    if (!endOfOpts && v === '--') { endOfOpts = true; continue; }
    if (!endOfOpts && v.startsWith('--')) {
      // git accepts any unique prefix of a long option, so --mirr is --mirror
      const name = v.slice(2).split('=')[0].toLowerCase();
      if (!name) continue;
      if (PUSH_DANGER.some((d) => d.startsWith(name))) deny('git.push-force', UNSAFE_PUSH);
      if (restricted && PUSH_WIDE.some((d) => d.startsWith(name))) deny('git.push-scope', 'your role pushes only HEAD or its own branch, without --all, --branches, --tags or --follow-tags');
      if (!v.includes('=') && ['push-option', 'recurse-submodules'].some((d) => d.startsWith(name))) i++;
      continue;
    }
    if (!endOfOpts && /^-./.test(v)) {
      for (let c = 1; c < v.length; c++) {
        if (v[c] === 'f' || v[c] === 'd') deny('git.push-force', UNSAFE_PUSH);
        if (v[c] === 'o') { if (c === v.length - 1) i++; break; }
      }
      continue;
    }
    positional.push(v);
  }
  const [remote, ...refspecs] = positional;
  const cfg = ctx.cfg;
  if (remote !== undefined && !cfg.pushRemotes.includes(remote)) deny('git.push-remote', `the team pushes only to ${cfg.pushRemotes.join(', ') || 'no remote'}`);
  const norm = (b) => (cfg.ci ? b.toLowerCase() : b);
  const isProtected = (b) => cfg.protectedBranches.some((x) => norm(x) === norm(b.replace(/^refs\/heads\//i, '')));
  for (const r of refspecs) {
    if (r.startsWith('+') || r.startsWith(':')) deny('git.push-force', UNSAFE_PUSH);
    const dst = r.includes(':') ? r.slice(r.lastIndexOf(':') + 1) : r;
    if (restricted) {
      if (r.includes(':')) deny('git.push-scope', 'your role pushes only HEAD or its own branch (git push -u origin HEAD), never src:dst');
      if (isProtected(dst)) deny('git.push-scope', `${dst} is pushed only by the integrator`);
      if (ctx.ownBranch && r !== 'HEAD' && norm(r.replace(/^refs\/heads\//i, '')) !== norm(ctx.ownBranch)) {
        deny('git.push-scope', `your role pushes only HEAD or ${ctx.ownBranch}`);
      }
    } else if (!ctx.profile.integrator && isProtected(dst)) deny('git.push-scope', `${dst} is pushed only by the integrator`);
  }
}
// xargs appends arguments from stdin, so under it git may only read.
function checkXargs(cmd, ps) {
  for (const seg of segments(cmd, ps)) {
    const words = (seg.match(/"[^"]*"|'[^']*'|\S+/g) || []).map((s) => s.replace(/^["']|["']$/g, ''));
    let i = words.findIndex((w) => /^xargs(\.exe)?$/i.test(w));
    if (i < 0) continue;
    for (i++; i < words.length && words[i].startsWith('-');) i += /^-[InLPdasEe]$/.test(words[i]) ? 2 : 1;
    const prog = (words[i] || '').replace(/^.*[\\/]/, '').replace(/\.exe$/i, '').toLowerCase();
    if (/^(sh|bash|zsh|dash|pwsh|powershell|cmd|py|python[\d.]*|node)$/.test(prog) && words.slice(i + 1).some((w) => /\bgit\b/i.test(w))) {
      deny('git.indirect', 'git through xargs may only read');
    }
    if (prog !== 'git') continue;
    let j = i + 1;
    while (j < words.length && words[j].startsWith('-')) j += GIT_VALUE_OPTS.has(words[j]) ? 2 : 1;
    if (!GIT_SAFE_UNDER_XARGS.has((words[j] || '').toLowerCase())) deny('git.indirect', 'git through xargs may only read');
  }
}

// ---------------------------------------------------------------- checks

function secretMentioned(text, cfg) {
  const s = String(text);
  for (const re of cfg.secretRes) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(s))) {
      const found = m[0].replace(/^[\\/\s"'~=]+|[\\/\s"']+$/g, '');
      if (!cfg.secretExceptions.some((x) => x.test(found))) return found || 'a secret path';
      if (m[0].length === 0) re.lastIndex++;
    }
  }
  return '';
}
function applyCommandRules(text, where, ctx) {
  for (const rule of ctx.cfg.commandRules) {
    if (!rule.on.includes(where) || !profileMatches(rule.profiles, rule.exceptProfiles, ctx)) continue;
    let hit = !!rule.re && rule.re.test(text);
    if (!hit && where === 'shell' && rule.scripts.length) hit = rule.scripts.some((s) => runsScript(text, s));
    if (!hit) continue;
    if (rule.unless) {
      const re = new RegExp(rule.unless.replace(/\{role\}/g, ctx.role && ctx.role !== '?' ? escapeRe(ctx.role) : '(?!)'), 'i');
      if (re.test(text)) continue;
    }
    if (rule.action === 'ask') { if (!ctx.ask) ctx.ask = { rule: rule.id, why: rule.why }; continue; }
    deny(rule.id, rule.why);
  }
}
function protectedHit(abs, raw, ctx) {
  for (const pp of ctx.cfg.protectedPaths) {
    if (!profileMatches(pp.profiles, pp.exceptProfiles, ctx)) continue;
    const set = pathSet(ctx, pp.paths);
    if (set.file(abs) || set.text(raw)) return pp;
  }
  return null;
}

function checkShell(raw, dialect, ctx) {
  const ps = dialect === 'powershell';
  const { cfg, profile } = ctx;
  if (raw.length > cfg.maxCommandChars || (raw.match(/<</g) || []).length > cfg.maxHeredocs) {
    deny('size.too-long', 'the command is too long or has too many heredocs for the guard; split it');
  }
  const cmd = scanText(raw, ps);
  const segs = segments(cmd, ps);
  // Search, echo and read-only git text is data, unless something on the line runs code.
  const runsCode = segs.some((s) => EXEC_SEG.test(s));
  const active = runsCode ? cmd : segs.filter((s) => !dataSegment(s, ps) && !(GIT_READ_SEG.test(s) && inert(s, ps))).join('\n');
  applyCommandRules(active, 'shell', ctx);
  if (encodedPowershell(segs, ps)) deny('hidden.encoded-ps', 'encoded PowerShell commands hide what they run');
  if (decodeRun(active)) deny('hidden.decode-run', 'decoding and running hidden commands is forbidden');
  if (PROXY.test(active)) deny('hidden.proxy', 'proxy overrides are owner-only');
  checkGit(active, ps, ctx);
  checkXargs(cmd, ps);
  if (writesStrict(cmd, GIT_FILE_MATCHER, ps, ctx)) deny('git.files', 'git config, refs, hooks and shell profiles are owner-only');
  for (const pp of cfg.protectedPaths) {
    if (profileMatches(pp.profiles, pp.exceptProfiles, ctx) && writesStrict(cmd, pathSet(ctx, pp.paths), ps, ctx)) deny(pp.id, pp.why);
  }
  if (ctx.marked && writesStrict(cmd, pathSet(ctx, cfg.markerList), ps, ctx)) deny('protect.marker', 'role markers are written only by the lead');
  if (ctx.marked && ctx.wtRootCmp) {
    for (const seg of segs) {
      const ws = wordsOf(seg, ps);
      const { i, verb } = leadingVerb(ws);
      if (!MOVE_OR_REMOVE.test(verb)) continue;
      for (const w of ws.slice(i + 1)) {
        const abs = resolveWord(w, ctx);
        if (abs === ctx.wtRootCmp || (under(abs, ctx.wtRootCmp) && !abs.slice(ctx.wtRootCmp.length + 1).includes('/'))) {
          deny('role.worktree-rename', 'role worktree folders are created, moved and removed only by the lead');
        }
      }
    }
  }
  if (profile.writeDeny.length) {
    const set = pathSet(ctx, profile.writeDeny);
    if (!set.empty && writesPrecise(cmd, set, ps, ctx)) deny('path.write-deny', `your role (${ctx.profileName}) may not write there`);
  }
  for (const seg of segs) {
    if (LISTING_SEG.test(seg)) continue;
    const secret = secretMentioned(searchOperands(seg), cfg);
    if (secret) deny('secret.path', `${secret} is a secret file; only code reads secrets, agents never open or print them`);
  }
  if (profile.readDeny.length) {
    const set = pathSet(ctx, profile.readDeny);
    if (!set.empty) {
      for (const seg of segs) {
        const ops = /\s(-f|--file)\b/.test(seg) ? seg : searchOperands(seg);
        if (names(ops, set, ps, ctx)) deny('path.read-deny', `your role (${ctx.profileName}) may not read there`);
      }
    }
  }
  if (!profile.allowAliasedPaths && aliasedPath(cmd, ps)) deny('path.aliased', 'links, junctions, drive substitutions and 8.3 short names are not allowed for your role');
}

function checkFile(tool, input, ctx) {
  const { cfg, profile } = ctx;
  const str = (v) => (typeof v === 'string' && v ? v : null);
  const targets = [input.file_path, input.path, input.notebook_path, input.glob].map(str).filter(Boolean);
  if (tool !== 'Glob') {
    for (const t of targets) {
      const secret = secretMentioned(t, cfg);
      if (secret) deny('secret.path', `${secret} is a secret file; only code reads secrets, agents never open or print them`);
    }
  }
  const reads = targets.concat(tool === 'Glob' && str(input.pattern) ? [input.pattern] : []);
  if (profile.readDeny.length) {
    const set = pathSet(ctx, profile.readDeny);
    for (const t of reads) if (set.text(t) || set.file(resolveWord(t, ctx))) deny('path.read-deny', `your role (${ctx.profileName}) may not read there`);
  }
  if (!profile.allowAliasedPaths && reads.some((t) => SHORT_NAME.test(t))) deny('path.aliased', '8.3 short names are not allowed for your role');
  if (!WRITE_TOOLS.has(tool)) return;
  const writes = [input.file_path, input.notebook_path].map(str).filter(Boolean);
  for (const t of writes) {
    const abs = resolveWord(t, ctx);
    if (GIT_FILE.test(t) || GIT_FILE.test(abs)) deny('git.files', 'git config, refs, hooks and shell profiles are owner-only');
    const pp = protectedHit(abs, t, ctx);
    if (pp) deny(pp.id, pp.why);
    if (ctx.marked && pathSet(ctx, cfg.markerList).file(abs)) deny('protect.marker', 'role markers are written only by the lead');
    if (profile.writeDeny.length && pathSet(ctx, profile.writeDeny).file(abs)) deny('path.write-deny', `your role (${ctx.profileName}) may not write there`);
    if (profile.writeAllow && !pathSet(ctx, profile.writeAllow).file(abs, ctx.mainCmp)) deny('path.write-allow', `your role (${ctx.profileName}) writes only inside its own worktree and temp folders`);
  }
  if (!writes.some((t) => cfg.codeExtRe.test(t))) return;
  const edits = Array.isArray(input.edits) ? input.edits : [];
  const text = [input.content, input.new_string, input.new_source].concat(edits.map((e) => e && e.new_string))
    .filter((v) => typeof v === 'string' && v).join('\n');
  if (!text) return;
  const codeRules = cfg.commandRules.some((r) => r.on.includes('code') && profileMatches(r.profiles, r.exceptProfiles, ctx));
  if ((codeRules || profile.contentRules) && (text.length > 20 * cfg.maxCommandChars || longestLine(text) > cfg.maxCommandChars)) {
    deny('size.too-long', 'this code is too large for the guard to check (or has a very long line); write it in smaller parts');
  }
  applyCommandRules(text, 'code', ctx);
  if (!profile.contentRules) return;
  const secret = secretMentioned(text, cfg);
  if (secret) deny('code.secret', `code written by your role may not name secret files (${secret})`);
  if (/\bgit\b[\s\S]{0,40}?\bpush\b/i.test(text)) deny('code.git-push', 'code written by your role may not run git push; push your branch with git push -u origin HEAD');
  const data = pathSet(ctx, ['{dataStore}']);
  if (!data.empty && codeWritesInto(text, data)) deny('code.data-write', 'code written by your role may not write into the shared data store');
}

function strings(value, depth, out) {
  if (out.length >= 500) return out;
  if (typeof value === 'string') { out.push(value); return out; }
  if (!value || typeof value !== 'object' || depth > 4) return out;
  for (const k of Object.keys(value)) {
    if (out.length >= 500) break;
    out.push(k);
    strings(value[k], depth + 1, out);
  }
  return out;
}
function longestLine(text) {
  let max = 0;
  let start = 0;
  for (let i = text.indexOf('\n'); i >= 0; i = text.indexOf('\n', start)) { max = Math.max(max, i - start); start = i + 1; }
  return Math.max(max, text.length - start);
}
function checkBrowser(tool, input, ctx) {
  if (ctx.cfg.realBrowserRe.test(tool) && ctx.marked && !ctx.profile.realBrowser) {
    deny('role.real-browser', "this tool acts with the owner's real sign-ins; only the designated operator role drives it");
  }
  const all = strings(input, 0, []);
  const total = all.reduce((n, s) => n + s.length, 0);
  if (total > 2 * ctx.cfg.maxCommandChars || all.some((s) => s.length > ctx.cfg.maxCommandChars)) {
    deny('size.too-long', 'this browser input is too large for the guard to check; split it');
  }
  for (const s of all) {
    const secret = secretMentioned(s, ctx.cfg);
    if (secret) deny('secret.exfil', `${secret} is a secret file and may not leave through ${tool}`);
    let text = s;
    for (const re of ctx.profile.browserAllowRes) { re.lastIndex = 0; text = text.replace(re, '[allowed]'); }
    applyCommandRules(text, 'browser', ctx);
  }
}
function checkOther(tool, input, ctx) {
  for (const s of strings(input, 0, [])) {
    const secret = secretMentioned(s, ctx.cfg);
    if (secret) deny('secret.exfil', `${secret} is a secret file and may not leave through ${tool}`);
  }
}

// ---------------------------------------------------------------- decision

function runtimeOptions(env) {
  return {
    configPath: env.TEAM_GUARD_CONFIG || path.join(__dirname, 'guard.config.json'),
    projectDir: env.CLAUDE_PROJECT_DIR || '',
    worktreesRoot: env.TEAM_WORKTREES_ROOT || '',
    tmp: env.TEAM_GUARD_TMP || os.tmpdir(),
    home: env.TEAM_GUARD_HOME || os.homedir(),
  };
}

function decide(ev, rt) {
  const tool = String(ev.tool_name || '');
  const input = ev.tool_input && typeof ev.tool_input === 'object' ? ev.tool_input : {};
  const out = (decision, rule, reason, ctx, cfg) => ({
    decision, rule: rule || null, reason: reason || '', role: ctx ? ctx.role || '' : '',
    profile: ctx ? ctx.profileName : '', tool, cfg: cfg || null,
  });
  let cfg;
  try {
    cfg = loadConfig(rt.configPath, rt);
  } catch (e) {
    if (!(e instanceof ConfigError)) throw e;
    // Reads still pass, and so do edits of the config itself by unmarked sessions (the lead).
    if (['Read', 'Grep', 'Glob'].includes(tool)) return out('allow', null, '', null);
    const target = String(input.file_path || '');
    const cwd = toNative(String(ev.cwd || process.cwd()), undefined, rt.home);
    const marked = !!markerDir(cwd, '.team-role');
    const isConfig = target && (/(^|[\\/])guard\.config\.json$/i.test(target) ||
      cmpForm(toNative(target, cwd, rt.home), true) === cmpForm(path.resolve(rt.configPath), true));
    if (WRITE_TOOLS.has(tool) && isConfig && !marked) return out('allow', null, '', null);
    return out('deny', 'config.invalid', `guard config: ${e.message}; the lead fixes it (run node guard.cjs --check-config)`, null);
  }
  let ctx = null;
  try {
    ctx = makeContext(ev, cfg, rt);
    const dialects = Object.prototype.hasOwnProperty.call(cfg.shellTools, tool) ? cfg.shellTools[tool] : null;
    if (dialects) for (const d of dialects) checkShell(String(input.command || ''), d, ctx);
    else if (FILE_TOOLS.has(tool)) checkFile(tool, input, ctx);
    else if (cfg.browserRe.test(tool)) checkBrowser(tool, input, ctx);
    else checkOther(tool, input, ctx);
  } catch (e) {
    if (e instanceof Deny) return out('deny', e.rule, e.why, ctx, cfg);
    if (cfg.onInternalError === 'deny') return out('deny', 'guard.error', `the guard failed (${e && e.name}); write BLOCKED to the coordinator`, ctx, cfg);
    return out('error', 'guard.error', (e && e.name) || 'Error', ctx, cfg);
  }
  if (ctx.ask) return out('ask', ctx.ask.rule, ctx.ask.why, ctx, cfg);
  return out('allow', null, '', ctx, cfg);
}

function logDenial(res, input) {
  const cfg = res.cfg;
  if (!cfg || !cfg.denialLog) return;
  try {
    const file = toNative(cfg.denialLog, cfg.main);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const sha = crypto.createHash('sha256').update(JSON.stringify(input === undefined ? null : input)).digest('hex');
    fs.appendFileSync(file, `${JSON.stringify({ at: new Date().toISOString(), role: res.role, profile: res.profile, tool: res.tool, rule: res.rule, input_sha256: sha })}\n`);
  } catch (e) { /* the log is optional */ }
}

function emit(res, ev, explain) {
  const name = res.cfg ? res.cfg.name : 'team-guard';
  if (explain) {
    const decision = res.decision === 'error' ? 'allow' : res.decision;
    process.stdout.write(`${JSON.stringify({ decision, rule: res.rule, reason: res.reason, role: res.role, profile: res.profile, tool: res.tool })}\n`);
    return 0;
  }
  if (res.decision === 'allow') return 0;
  if (res.decision === 'error') { process.stderr.write(`${name} internal error (allowed): ${res.reason}\n`); return 1; }
  logDenial(res, ev.tool_input);
  const suffix = res.cfg && res.cfg.blockedSuffix ? ` ${res.cfg.blockedSuffix}` : '';
  const why = String(res.reason).replace(/[.\s]+$/, '');
  if (res.cfg && res.cfg.output === 'json') {
    process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: res.decision,
      permissionDecisionReason: `${name} [${res.rule}]: ${why}.${suffix}` } }));
    return 0;
  }
  process.stderr.write(`${name} blocked [${res.rule}]: ${why}.${suffix}\n`);
  return 2;
}

// ---------------------------------------------------------------- --check-config

function checkConfigCli() {
  const rt = runtimeOptions(process.env);
  let cfg;
  try { cfg = loadConfig(rt.configPath, rt); } catch (e) {
    process.stdout.write(`config: ${rt.configPath}\nINVALID: ${e.message}\n`);
    return 1;
  }
  const lines = [`config: ${rt.configPath}`, `main: ${cfg.main}`];
  const wt = cfg.tokens.worktreesRoot;
  lines.push(`worktreesRoot: ${wt} (${fs.existsSync(wt) ? 'exists' : 'not created yet'})`);
  lines.push(`unset paths (rules that use them are off): ${cfg.unsetPaths.join(', ') || 'none'}`);
  lines.push(`commandRules: ${cfg.commandRules.map((r) => r.id).join(', ') || 'none'}`);
  lines.push(`protectedPaths: ${cfg.protectedPaths.map((r) => r.id).join(', ') || 'none'}`);
  lines.push(`profiles: ${Object.values(cfg.profiles).map((p) => (p.chain.length > 1 ? `${p.name} (extends ${p.chain[1]})` : p.name)).join(', ')}`);
  lines.push(`roles: ${Object.entries(cfg.roles).map(([r, p]) => `${r}=${p}`).join(', ') || 'none'}`);
  lines.push(`unmarked sessions: ${cfg.unmarkedProfile}; unknown roles: ${cfg.unknownRoleProfile}; output: ${cfg.output}`);
  for (const w of cfg.warnings) lines.push(`warning: ${w}`);
  lines.push('OK');
  process.stdout.write(`${lines.join('\n')}\n`);
  return 0;
}

// ---------------------------------------------------------------- --self-test

function selfTestConfig(root) {
  return {
    version: 1,
    name: 'team-guard',
    output: 'exit2',
    blockedSuffix: 'Write BLOCKED to the coordinator.',
    paths: {
      main: `${root}/main`,
      worktreesRoot: '{main}/.claude/worktrees',
      vault: `${root}/vault`,
      dataStore: `${root}/data`,
      approvalFlag: '{main}/runtime/team/paid_enabled.flag',
      ledgers: '{main}/runtime/paid_research',
      codeDirs: ['src', 'tools', 'tests', 'web'],
      codeExtensions: ['py', 'js', 'cjs', 'mjs', 'ps1', 'sh', 'cmd', 'bat', 'ipynb'],
    },
    secretPaths: {
      patterns: [String.raw`(?<![A-Za-z0-9_$])\.env(?![A-Za-z0-9_])(?:\.[A-Za-z0-9_-]+)*`, String.raw`\.git-credentials\b`,
        String.raw`(^|[\\/\s"'~=])\.ssh([\\/]|\s|$)`, String.raw`\.(pem|p12|pfx)\b`],
      exceptions: [String.raw`^\.env(\.[A-Za-z0-9_-]+)?\.(example|sample|template)$`],
    },
    git: { pushRemotes: ['origin'], protectedBranches: ['main', 'master'], roleBranchPattern: 'team/{role}' },
    commandRules: [
      { id: 'owner.live-flag', pattern: String.raw`(\.(py|js|mjs|cjs|ts|sh|ps1)\b|\s-m\s+[\w.]+)[^\n]*\s--(live|web-run)\b`, why: 'live entrypoints are owner-only', on: ['shell', 'browser'] },
      { id: 'owner.run-control', pattern: String.raw`/api/(runs?|bots?|exchange)/(start|stop|resume)\b|/api/(play|stop)\b`, why: 'Start, Stop and Resume are owner-only', on: ['shell', 'browser'] },
      { id: 'owner.launchers', pattern: String.raw`\b(START|STOP|RESTART|INSTALL|UPDATE|IMPORT|EXPORT|CHECK)_[A-Z0-9_]+\.(cmd|bat|ps1|sh)\b`, why: 'owner launchers are owner-only', on: ['shell', 'browser'] },
      { id: 'data.download', script: 'fetch_public_data', why: 'only the platform downloads data', on: ['shell'], profiles: ['author', 'staff'] },
      { id: 'paid.direct', pattern: String.raw`\b(import|from)\s+example_ai\b|api\.example-ai\.com`, why: 'paid calls go through the harness', on: ['shell', 'code'], profiles: ['author', 'staff', 'judge'] },
      { id: 'paid.spender', pattern: String.raw`\bpaid_research(\.py)?\b[^\n|;&]*\bask\b`, unless: String.raw`--spender(=|\s+){role}(\s|$)`, why: 'spend only your own budget', on: ['shell'], profiles: ['author'] },
      { id: 'ask.demo', pattern: String.raw`\bdemo_order\b`, why: 'needs the owner', on: ['shell'], profiles: ['@unmarked'], action: 'ask' },
    ],
    protectedPaths: [
      { id: 'protect.guard', paths: ['team/tools/guard.cjs', 'team/tools/guard.config.json', '.claude/settings.json', '.claude/settings.local.json', '.claude/hooks'], why: 'the guard and the hook wiring are changed only by the lead', profiles: ['@marked'] },
      { id: 'protect.marker', paths: ['.team-role'], why: 'role markers are written only by the lead', profiles: ['@marked'] },
      { id: 'paid.flag', paths: ['{approvalFlag}'], why: 'only the coordinator enables paid calls', profiles: ['@marked'] },
    ],
    profiles: {
      lead: { integrator: true, realBrowser: true },
      engineer: { gitRestricted: true },
      author: { readDeny: ['{vault}'], writeAllow: ['{worktree}', '{tmp}', '{home}/.claude/projects'], writeDeny: ['{dataStore}', '{ledgers}', '{otherWorktrees}'], allowAliasedPaths: false, gitRestricted: true, contentRules: true },
      staff: { readDeny: ['{vault}'], writeAllow: ['{worktree}', '{tmp}', '{home}/.claude/projects'], writeDeny: ['{dataStore}', '{ledgers}', '{code}'], allowAliasedPaths: false, gitRestricted: true, contentRules: true },
      ops: { extends: 'staff', realBrowser: true, browserAllow: [String.raw`\bCHECK_PACKAGE\.cmd\b`] },
      judge: { writeAllow: ['{worktree}', '{vault}', '{tmp}', '{home}/.claude/projects'], writeDeny: ['{dataStore}', '{ledgers}', '{code}'], allowAliasedPaths: false, gitRestricted: true },
    },
    roles: { platform: 'engineer', alpha: 'author', beta: 'author', reviewer: 'staff', ops: 'ops', judge: 'judge' },
    unmarkedProfile: 'lead',
    unknownRoleProfile: 'staff',
  };
}

// [who, tool, input, expected]; expected is ALLOW, DENY, ASK or a rule id. Placeholders:
// <main> main checkout, <wt> the session's worktree, <vault>, <data>, <tmp> (scratch), <q> a double quote.
const SELF_TEST_CASES = [
  ['lead', 'Bash', { command: 'python -m pytest -q' }, 'ALLOW'],
  ['lead', 'Bash', { command: 'python executor.py --live' }, 'owner.live-flag'],
  ['lead', 'Bash', { command: 'py -3.12 -X utf8 -B one_trade.py --check' }, 'ALLOW'],
  ['lead', 'Bash', { command: 'python -m tradinglab.one_trade --live' }, 'owner.live-flag'],
  ['lead', 'Bash', { command: 'curl -X POST http://127.0.0.1:8000/api/runs/start' }, 'owner.run-control'],
  ['lead', 'Bash', { command: 'curl -s http://127.0.0.1:8000/api/runs/status' }, 'ALLOW'],
  ['lead', 'PowerShell', { command: '& .\\START_PRODUCTION.cmd' }, 'owner.launchers'],
  ['lead', 'Bash', { command: 'grep -rn -- "--live" src/' }, 'ALLOW'],
  ['lead', 'Bash', { command: "cat > notes.md <<'EOF'\nnever run executor.py --live\nEOF" }, 'ALLOW'],
  ['lead', 'Bash', { command: "bash <<'EOF'\npython executor.py --live\nEOF" }, 'owner.live-flag'],
  ['lead', 'Bash', { command: 'echo $(python executor.py --live)' }, 'owner.live-flag'],
  ['lead', 'Bash', { command: 'echo "never git push --force"' }, 'ALLOW'],
  ['lead', 'Bash', { command: 'git commit -m "docs: never git push --force or --live"' }, 'ALLOW'],
  ['lead', 'Bash', { command: "git commit -m \"$(cat <<'EOF'\nNever run --live or git push --force.\nEOF\n)\"" }, 'ALLOW'],
  ['lead', 'Bash', { command: 'git push origin main' }, 'ALLOW'],
  ['lead', 'Bash', { command: 'git push origin main # not --force' }, 'ALLOW'],
  ['lead', 'Bash', { command: 'git push --force-with-lease origin main' }, 'git.push-force'],
  ['lead', 'Bash', { command: 'git push --forc origin main' }, 'git.push-force'],
  ['lead', 'Bash', { command: 'git push -fu origin topic' }, 'git.push-force'],
  ['lead', 'Bash', { command: 'git push origin +main' }, 'git.push-force'],
  ['lead', 'Bash', { command: 'git push origin :old' }, 'git.push-force'],
  ['lead', 'Bash', { command: 'git push upstream main' }, 'git.push-remote'],
  ['lead', 'Bash', { command: 'bash -c "git push --mirror origin"' }, 'git.push-force'],
  ['lead', 'PowerShell', { command: "pwsh -Command 'git push --force origin main'" }, 'git.push-force'],
  ['lead', 'Bash', { command: 'git -c core.hooksPath=/tmp/h push origin main' }, 'git.config'],
  ['lead', 'Bash', { command: 'GIT_TRACE=1 git push origin main' }, 'git.env'],
  ['lead', 'Bash', { command: 'git reset --hard HEAD~1' }, 'git.destructive'],
  ['lead', 'Bash', { command: 'git clean -fdx' }, 'git.destructive'],
  ['lead', 'Bash', { command: 'git checkout -- .' }, 'git.destructive'],
  ['lead', 'Bash', { command: 'git filter-repo --invert-paths --path x' }, 'git.history'],
  ['lead', 'Bash', { command: 'git reflog expire --expire=now --all' }, 'git.history'],
  ['lead', 'Bash', { command: 'git remote set-url origin https://example.com/x.git' }, 'git.remote'],
  ['lead', 'Bash', { command: 'git config user.email a@example.com' }, 'git.config'],
  ['lead', 'Bash', { command: 'git config --get user.name' }, 'ALLOW'],
  ['lead', 'Bash', { command: 'echo x | xargs git push' }, 'git.indirect'],
  ['lead', 'Bash', { command: 'g=git; $g push origin main' }, 'git.indirect'],
  ['lead', 'Bash', { command: 'cat .env.local' }, 'secret.path'],
  ['lead', 'PowerShell', { command: 'Get-Content .\\.ENV.LOCAL' }, 'secret.path'],
  ['lead', 'Bash', { command: 'cat .env.example' }, 'ALLOW'],
  ['lead', 'PowerShell', { command: 'Test-Path .env.local' }, 'ALLOW'],
  ['lead', 'Bash', { command: 'grep -rn "process.env" src/' }, 'ALLOW'],
  ['lead', 'Read', { file_path: '<main>/.env.local' }, 'secret.path'],
  ['lead', 'Read', { file_path: '<main>/.env.example' }, 'ALLOW'],
  ['lead', 'SendUserFile', { files: ['report.md', '.env.local'] }, 'secret.exfil'],
  ['lead', 'PowerShell', { command: 'powershell -enc SQBFAFgA' }, 'hidden.encoded-ps'],
  ['lead', 'Bash', { command: 'echo cHl0aG9u | base64 -d | sh' }, 'hidden.decode-run'],
  ['lead', 'Bash', { command: 'export HTTPS_PROXY=http://127.0.0.1:9' }, 'hidden.proxy'],
  ['lead', 'Bash', { command: 'echo "alias g=git" >> ~/.bashrc' }, 'git.files'],
  ['lead', 'Write', { file_path: '<main>/team/tools/guard.cjs', content: '//' }, 'ALLOW'],
  ['lead', 'Write', { file_path: '<main>/.git/hooks/pre-push', content: 'x' }, 'git.files'],
  ['lead', 'Bash', { command: 'demo_order --size 1' }, 'ASK'],
  ['lead', 'Bash', { command: 'x'.repeat(100001) }, 'size.too-long'],
  ['alpha', 'Write', { file_path: '<main>/team/tools/guard.cjs', content: '//' }, 'protect.guard'],
  ['alpha', 'Bash', { command: 'rm .team-role' }, 'protect.marker'],
  ['alpha', 'Bash', { command: 'echo beta > .team-role' }, 'protect.marker'],
  ['alpha', 'Bash', { command: 'git push -u origin HEAD' }, 'ALLOW'],
  ['alpha', 'Bash', { command: 'git push -u origin team/alpha' }, 'ALLOW'],
  ['alpha', 'Bash', { command: 'git push origin HEAD:main' }, 'git.push-scope'],
  ['alpha', 'Bash', { command: 'git push origin main' }, 'git.push-scope'],
  ['alpha', 'Bash', { command: 'git push origin team/beta' }, 'git.push-scope'],
  ['alpha', 'Bash', { command: 'git push --tags origin' }, 'git.push-scope'],
  ['alpha', 'Bash', { command: 'git tag v1' }, 'git.refs'],
  ['alpha', 'Bash', { command: 'git tag --list' }, 'ALLOW'],
  ['alpha', 'Bash', { command: 'git worktree add ../x' }, 'git.refs'],
  ['alpha', 'Bash', { command: 'git worktree list' }, 'ALLOW'],
  ['alpha', 'Read', { file_path: '<vault>/holdout.csv' }, 'path.read-deny'],
  ['alpha', 'Bash', { command: 'head <vault>/holdout.csv' }, 'path.read-deny'],
  ['alpha', 'Bash', { command: 'grep -rn holdout <vault>' }, 'path.read-deny'],
  ['alpha', 'Bash', { command: 'python tools/fetch_public_data.py --inst X' }, 'data.download'],
  ['alpha', 'Bash', { command: 'grep -n def tools/fetch_public_data.py' }, 'ALLOW'],
  ['alpha', 'Bash', { command: 'python -c "import example_ai"' }, 'paid.direct'],
  ['alpha', 'Write', { file_path: '<wt>/research/a.py', content: "open('.env.local').read()" }, 'code.secret'],
  ['alpha', 'Write', { file_path: '<wt>/research/b.py', content: 'from example_ai import Client' }, 'paid.direct'],
  ['alpha', 'Write', { file_path: '<wt>/research/c.py', content: 'import subprocess\nsubprocess.run(["git", "push"])' }, 'code.git-push'],
  ['alpha', 'Write', { file_path: '<wt>/research/d.py', content: 'df.to_csv("<data>/x.csv")' }, 'code.data-write'],
  ['alpha', 'Write', { file_path: '<wt>/research/e.py', content: 'Path("<data>/x.json").write_text("{}")' }, 'code.data-write'],
  ['alpha', 'Write', { file_path: '<wt>/research/f.py', content: 'Path("out/x.json").write_text("{}")' }, 'ALLOW'],
  ['alpha', 'Bash', { command: "python -c \"open('<data>/x.csv', 'w').write('1')\"" }, 'path.write-deny'],
  ['alpha', 'Write', { file_path: '<wt>/team/authors/alpha/NOTEBOOK.md', content: '# notes' }, 'ALLOW'],
  ['alpha', 'Write', { file_path: '<main>/team/BOARD.md', content: '# board' }, 'path.write-allow'],
  ['alpha', 'Bash', { command: 'python tools/paid_research.py ask --spender beta --paid' }, 'paid.spender'],
  ['alpha', 'Bash', { command: 'python tools/paid_research.py ask --spender alpha --dry-run' }, 'ALLOW'],
  ['alpha', 'Bash', { command: 'cmd /c mklink /J link <vault>' }, 'DENY'],
  ['alpha', 'Bash', { command: 'cp research/out.csv <data>/' }, 'path.write-deny'],
  ['alpha', 'Bash', { command: 'echo x > <main>/runtime/team/paid_enabled.flag' }, 'paid.flag'],
  ['alpha', 'Bash', { command: 'mv ../beta ../beta-old' }, 'role.worktree-rename'],
  ['alpha', 'mcp__claude-in-chrome__navigate', { url: 'https://example.com' }, 'role.real-browser'],
  ['alpha', 'mcp__Claude_Browser__navigate', { url: 'http://127.0.0.1:8000/api/exchange/start' }, 'owner.run-control'],
  ['alpha@main', 'Bash', { command: 'git push origin main' }, 'git.push-scope'],
  ['reviewer', 'Write', { file_path: '<main>/src/engine.py', content: 'x = 1' }, 'path.write-deny'],
  ['reviewer', 'Write', { file_path: '<tmp>/mutant.py', content: 'x = 1' }, 'ALLOW'],
  ['reviewer', 'Bash', { command: "sed -i 's/a/b/' <main>/src/engine.py" }, 'path.write-deny'],
  ['reviewer', 'Bash', { command: 'sed -i s/a/b/ "<mainbs>\\src\\engine.py"' }, 'path.write-deny'],
  ['reviewer', 'Bash', { command: 'echo finding >> team/reviews/R1_REVIEW.md' }, 'ALLOW'],
  ['reviewer', 'Bash', { command: 'echo x > engine.py' }, 'path.write-deny'],
  ['reviewer', 'Bash', { command: 'python -m pytest <main>/tests/test_engine.py -q' }, 'ALLOW'],
  ['ops', 'mcp__claude-in-chrome__navigate', { url: 'http://127.0.0.1:8000/api/runs/stop' }, 'owner.run-control'],
  ['ops', 'mcp__claude-in-chrome__computer', { action: 'type', text: 'CHECK_PACKAGE.cmd' }, 'ALLOW'],
  ['ops', 'mcp__claude-in-chrome__computer', { action: 'type', text: 'START_PANEL.cmd' }, 'owner.launchers'],
  ['judge', 'Read', { file_path: '<vault>/holdout.csv' }, 'ALLOW'],
  ['judge', 'Write', { file_path: '<main>/tools/eval.py', content: 'x = 1' }, 'path.write-deny'],
  ['platform', 'Write', { file_path: '<wt>/src/engine.py', content: 'x = 1' }, 'ALLOW'],
  ['platform', 'Bash', { command: 'git push origin main' }, 'git.push-scope'],
  ['zeta', 'Write', { file_path: '<main>/src/engine.py', content: 'x = 1' }, 'path.write-deny'],
  ['ghost', 'Write', { file_path: '<main>/team/BOARD.md', content: '# board' }, 'path.write-allow'],
];

function selfTest(verbose) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'team-guard-selftest-'));
  let failed = 0;
  let n = 0;
  const report = (ok, label) => {
    n++;
    if (!ok) { failed++; process.stdout.write(`BAD ${label}\n`); } else if (verbose) process.stdout.write(`ok  ${label}\n`);
  };
  try {
    const main = path.join(root, 'main');
    const wtRoot = path.join(main, '.claude', 'worktrees');
    const roles = ['alpha', 'beta', 'reviewer', 'ops', 'judge', 'platform', 'zeta'];
    for (const r of roles) {
      fs.mkdirSync(path.join(wtRoot, r), { recursive: true });
      fs.writeFileSync(path.join(wtRoot, r, '.team-role'), r === 'zeta' ? `${r}\n` : Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(`${r}\r\n`, 'utf16le')]));
    }
    for (const d of ['vault', 'data', 'scratch', 'home']) fs.mkdirSync(path.join(root, d), { recursive: true });
    const cfgPath = path.join(root, 'guard.config.json');
    fs.writeFileSync(cfgPath, JSON.stringify(selfTestConfig(root.replace(/\\/g, '/')), null, 2));
    const rt = { configPath: cfgPath, projectDir: '', worktreesRoot: '', tmp: path.join(root, 'scratch'), home: path.join(root, 'home') };
    const transcript = (dir) => path.join(rt.home, '.claude', 'projects', path.resolve(dir).replace(/[^A-Za-z0-9]/g, '-'), 'session.jsonl');
    const who = (w) => {
      if (w === 'lead') return { cwd: main, transcript_path: transcript(main) };
      if (w === 'alpha@main') return { cwd: main, transcript_path: transcript(path.join(wtRoot, 'alpha')) };
      if (w === 'ghost') return { cwd: main, transcript_path: transcript(path.join(wtRoot, 'ghost')) };
      return { cwd: path.join(wtRoot, w), transcript_path: transcript(path.join(wtRoot, w)) };
    };
    const fwd = (p) => p.replace(/\\/g, '/');
    const places = {
      '<mainbs>': main.replace(/\//g, '\\'), '<main>': fwd(main), '<vault>': fwd(path.join(root, 'vault')),
      '<data>': fwd(path.join(root, 'data')), '<tmp>': fwd(path.join(root, 'scratch')),
    };
    const fill = (v, w) => {
      if (typeof v === 'string') {
        let s = v.replace(/<wt>/g, fwd(path.join(wtRoot, w.split('@')[0])));
        for (const [k, p] of Object.entries(places)) s = s.split(k).join(p);
        return s;
      }
      if (Array.isArray(v)) return v.map((x) => fill(x, w));
      if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, fill(x, w)]));
      return v;
    };
    const verdict = (res, expect) => (expect === 'ALLOW' ? res.decision === 'allow' : expect === 'DENY' ? res.decision === 'deny'
      : expect === 'ASK' ? res.decision === 'ask' : res.decision === 'deny' && res.rule === expect);
    for (const [w, tool, input, expect] of SELF_TEST_CASES) {
      const ev = Object.assign({ hook_event_name: 'PreToolUse', session_id: 'self-test', tool_name: tool, tool_input: fill(input, w) }, who(w));
      const res = decide(ev, rt);
      report(verdict(res, expect), `${expect} => ${res.decision}${res.rule ? ` ${res.rule}` : ''} | ${w} ${tool} ${JSON.stringify(input).slice(0, 140)}`);
    }
    // A repository inside the temp folder: {tmp} in writeAllow must not open the main checkout.
    const tmpRt = Object.assign({}, rt, { tmp: root });
    for (const [file, expect] of [[`${fwd(main)}/team/BOARD.md`, 'path.write-allow'],
      [`${fwd(path.join(wtRoot, 'alpha'))}/team/notes.md`, 'ALLOW'], [`${fwd(path.join(root, 'scratch'))}/notes.md`, 'ALLOW']]) {
      const r0 = decide(Object.assign({ tool_name: 'Write', tool_input: { file_path: file, content: '#' } }, who('alpha')), tmpRt);
      report(verdict(r0, expect), `${expect} => ${r0.decision}${r0.rule ? ` ${r0.rule}` : ''} | alpha Write ${file.slice(fwd(root).length)} with the repository inside {tmp}`);
    }
    // An invalid config denies commands but still lets the lead read files and fix the config.
    const badPath = path.join(root, 'bad.config.json');
    fs.writeFileSync(badPath, '{ not json');
    const badRt = Object.assign({}, rt, { configPath: badPath });
    const lead = who('lead');
    let res = decide(Object.assign({ tool_name: 'Bash', tool_input: { command: 'ls' } }, lead), badRt);
    report(res.decision === 'deny' && res.rule === 'config.invalid', `config.invalid => ${res.decision} ${res.rule} | Bash with a broken config`);
    res = decide(Object.assign({ tool_name: 'Read', tool_input: { file_path: `${main}/README.md` } }, lead), badRt);
    report(res.decision === 'allow', `ALLOW => ${res.decision} | Read with a broken config`);
    res = decide(Object.assign({ tool_name: 'Edit', tool_input: { file_path: badPath, old_string: '{', new_string: '{}' } }, lead), badRt);
    report(res.decision === 'allow', `ALLOW => ${res.decision} | lead edits a broken config`);
    // The hook contract, through a real child process.
    const env = Object.assign({}, process.env, { TEAM_GUARD_CONFIG: cfgPath, TEAM_GUARD_TMP: rt.tmp, TEAM_GUARD_HOME: rt.home });
    delete env.CLAUDE_PROJECT_DIR;
    delete env.TEAM_WORKTREES_ROOT;
    const run = (input, args) => spawnSync(process.execPath, [__filename].concat(args || []), { input, env, encoding: 'utf8' });
    let r = run(JSON.stringify(Object.assign({ tool_name: 'Bash', tool_input: { command: 'git status' } }, lead)));
    report(r.status === 0 && !r.stderr && !r.stdout, `exit 0, silent => exit ${r.status} | process: harmless command`);
    r = run(JSON.stringify(Object.assign({ tool_name: 'Bash', tool_input: { command: 'git push --force origin main' } }, lead)));
    report(r.status === 2 && /\[git\.push-force\]/.test(r.stderr), `exit 2 + reason => exit ${r.status} ${r.stderr.trim().slice(0, 80)} | process: force push`);
    r = run('not json');
    report(r.status === 0, `exit 0 => exit ${r.status} | process: stdin is not JSON`);
    r = run(JSON.stringify(Object.assign({ tool_name: 'Bash', tool_input: { command: 'cat .env' } }, lead)), ['--explain']);
    let explained = {};
    try { explained = JSON.parse(r.stdout); } catch (e) { explained = {}; }
    report(r.status === 0 && explained.decision === 'deny' && explained.rule === 'secret.path', `--explain deny secret.path => ${r.stdout.trim().slice(0, 100)}`);
    const jsonCfg = path.join(root, 'json.config.json');
    fs.writeFileSync(jsonCfg, JSON.stringify(Object.assign(selfTestConfig(root.replace(/\\/g, '/')), { output: 'json' })));
    r = spawnSync(process.execPath, [__filename], { input: JSON.stringify(Object.assign({ tool_name: 'Bash', tool_input: { command: 'git reset --hard' } }, lead)),
      env: Object.assign({}, env, { TEAM_GUARD_CONFIG: jsonCfg }), encoding: 'utf8' });
    let parsed = {};
    try { parsed = JSON.parse(r.stdout).hookSpecificOutput || {}; } catch (e) { parsed = {}; }
    report(r.status === 0 && parsed.permissionDecision === 'deny', `json output deny => exit ${r.status} ${r.stdout.slice(0, 80)} | process: json mode`);
  } finally {
    try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* temp folder */ }
  }
  process.stdout.write(failed ? `self-test: ${failed} of ${n} FAILED\n` : `self-test: all ${n} passed\n`);
  return failed ? 1 : 0;
}

// ---------------------------------------------------------------- main

function main(argv) {
  if (argv.includes('--self-test')) { process.exitCode = selfTest(argv.includes('-v')); return; }
  if (argv.includes('--check-config')) { process.exitCode = checkConfigCli(); return; }
  const explain = argv.includes('--explain');
  let raw = '';
  let tooBig = false;
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (d) => { if (raw.length < MAX_STDIN) raw += d; else tooBig = true; });
  process.stdin.on('end', () => {
    let ev;
    try { ev = JSON.parse(raw); } catch (e) { ev = null; }
    if (!ev || typeof ev !== 'object') {
      if (tooBig) { process.stderr.write('team-guard blocked [size.too-long]: the tool input is too large for the guard; split it.\n'); process.exitCode = 2; return; }
      if (explain) process.stdout.write(`${JSON.stringify({ decision: 'allow', rule: null, reason: 'stdin is not JSON', role: '', profile: '', tool: '' })}\n`);
      process.exitCode = 0;
      return;
    }
    let res;
    try { res = decide(ev, runtimeOptions(process.env)); } catch (e) {
      res = { decision: 'error', rule: 'guard.error', reason: (e && e.name) || 'Error', role: '', profile: '', tool: String(ev.tool_name || ''), cfg: null };
    }
    process.exitCode = emit(res, ev, explain);
  });
}

if (require.main === module) main(process.argv.slice(2));
module.exports = { decide, compileConfig, runtimeOptions };
