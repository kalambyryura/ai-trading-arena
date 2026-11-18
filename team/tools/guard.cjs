#!/usr/bin/env node
// PreToolUse guard for the Jev Arena team (team/PROTOCOL_UA.md, section 6).
// Blocks owner-only financial actions, destructive or redirecting git commands and access to secret files.
// Author worktrees carry an untracked `.team-role` marker; for authors it also blocks the SEALED vault,
// their own market-data downloads and changes to shared refs. The ops and reviewer worktrees (D137) get
// the same data limits, no Jev and no code writes; only ops drives the owner's Chrome. Browser tools are
// checked like commands. Exit 2 blocks; any internal error allows.
'use strict';
const fs = require('fs');
const path = require('path');

const AUTHORS = new Set(['astra', 'sol', 'sonnet', 'opus', 'yurii']);
// D137: the operator of the other PC and the reviewer work in their own worktrees and change no code.
const STAFF = new Set(['ops', 'reviewer']);
const FILE_TOOLS = ['Read', 'Edit', 'Write', 'MultiEdit', 'Grep', 'Glob', 'NotebookEdit'];
// Claude in Chrome acts in the owner's own Chrome, including the Remote Desktop tab to the always-on PC;
// the built-in browser pane has its own profile. Both reach the local panels.
const CHROME_TOOL = /^mcp__claude-in-chrome__/;
const BROWSER_TOOL = /^(mcp__claude-in-chrome__|mcp__Claude_Browser__)/;
const WRITE_TOOLS = ['Edit', 'Write', 'MultiEdit', 'NotebookEdit'];
// shell tools and the shells their commands are read in (false = bash, true = PowerShell)
const SHELL_TOOLS = { Bash: [false], PowerShell: [true], mcp__terminal__run_in_terminal: [true, false] };
const WORKTREES = process.env.TEAM_WORKTREES_ROOT || 'C:/Projects/Trading/.claude/worktrees';
const MAX_COMMAND = 100000;

function secretEnvMentioned(text) {
  // Windows paths are case-insensitive, so .ENV.LOCAL is the same file as .env.local
  const re = /(?<![A-Za-z0-9_$])\.env(?:\.[A-Za-z0-9_-]+)*/gi;
  let m;
  while ((m = re.exec(text))) {
    if (!/^\.env(\.okx)?\.example$/i.test(m[0])) return true;
  }
  return false;
}
function secretPathMentioned(text) {
  if (secretEnvMentioned(text)) return '.env secret file';
  if (/LocalSettings([\\/]|\b)/i.test(text)) return 'LocalSettings';
  if (/\.private\.zip\b/i.test(text)) return 'private state zip';
  if (/\.git-credentials\b/i.test(text)) return 'git credentials file';
  if (/(^|[\\/\s"'~=])(\.netrc|_netrc)\b/i.test(text)) return 'netrc credentials file';
  if (/(^|[\\/\s"'~=])\.ssh([\\/]|\s|$)/i.test(text)) return 'SSH keys';
  return '';
}

function markerDir(start) {
  let dir = start;
  for (let i = 0; dir && i < 8; i++) {
    if (fs.existsSync(path.join(dir, '.team-role'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return '';
}
// Desktop worktree sessions report the main checkout as CLAUDE_PROJECT_DIR, but Claude Code names each
// session's transcript folder after its own worktree, so the transcript path pins the author's role even
// after a cd into the main checkout. The hook cwd and CLAUDE_PROJECT_DIR are the fallbacks.
function homeDir(ev) {
  const parts = String(ev.transcript_path || '').split(/[\\/]/);
  if (parts.length < 2) return '';
  let names = [];
  try { names = fs.readdirSync(WORKTREES); } catch (e) { return ''; }
  for (const name of names) {
    const dir = path.join(WORKTREES, name);
    if (parts.includes(path.resolve(dir).replace(/[^A-Za-z0-9]/g, '-')) && fs.existsSync(path.join(dir, '.team-role'))) return dir;
  }
  return '';
}
function roleBase(ev) {
  const home = homeDir(ev);
  if (home) return home;
  for (const dir of [process.env.CLAUDE_PROJECT_DIR, ev.cwd || process.cwd()]) {
    const found = dir ? markerDir(String(dir)) : '';
    if (found) return found;
  }
  return '';
}
function block(why) {
  process.stderr.write(`team-guard blocked: ${why}. See team/CHARTER_UA.md, section 7. ` +
    'If this action is legitimate, write BLOCKED for coord; do not work around the guard.\n');
  process.exit(2);
}

function check(ev) {
  const tool = String(ev.tool_name || '');
  const input = ev.tool_input && typeof ev.tool_input === 'object' ? ev.tool_input : {};
  const base = roleBase(ev);
  const role = base ? fs.readFileSync(path.join(base, '.team-role'), 'utf8').replace(/^\uFEFF/, '').split(/\r?\n/)[0].trim().toLowerCase() : '';
  const isAuthor = AUTHORS.has(role);
  const isStaff = STAFF.has(role);

  if (SHELL_TOOLS[tool]) {
    for (const ps of SHELL_TOOLS[tool]) checkShell(String(input.command || ''), ps, base, role, isAuthor, isStaff);
    return;
  }
  if (FILE_TOOLS.includes(tool)) {
    checkFileTool(tool, input, role, isAuthor, isStaff);
    return;
  }
  if (BROWSER_TOOL.test(tool)) {
    checkBrowser(tool, input, role);
    return;
  }
  // Any other tool in the hook matcher (Artifact, SendUserFile, MCP tools): no secret file may leave through it.
  for (const s of strings(input, 0)) {
    const secret = secretPathMentioned(s);
    if (secret) block(`${secret} access`);
  }
}

function strings(value, depth) {
  if (typeof value === 'string') return [value];
  if (!value || typeof value !== 'object' || depth > 4) return [];
  return Object.keys(value).flatMap((k) => [k].concat(strings(value[k], depth + 1))).slice(0, 500);
}

function checkFileTool(tool, input, role, isAuthor, isStaff) {
  const target = [input.file_path, input.path, input.notebook_path, input.glob]
    .filter(Boolean).map(String).join(' ');
  if (tool !== 'Glob') {
    const secret = secretPathMentioned(target);
    if (secret) block(`${secret} access`);
  }
  // Glob's pattern is a path; Grep's pattern is only the text searched for
  const paths = target + (tool === 'Glob' ? ' ' + String(input.pattern || '') : '');
  const who = isAuthor ? 'authors' : role;
  if ((isAuthor || isStaff) && /Trading-vault/i.test(paths)) block(`${who} may not access the SEALED vault`);
  if ((isAuthor || isStaff) && aliasedPath(paths)) block(`${who} may not use 8.3 short names or aliased paths`);
  if (!WRITE_TOOLS.includes(tool)) return;
  if (GIT_FILE.test(target)) block('git config, refs, hooks and shell profiles are owner-only');
  if (role && GUARD_FILES.test(target)) block('the guard and the Claude settings are changed only by the lead');
  if (role && /(^|[\\/])\.team-role(\s|$)/i.test(target)) block('role markers are set by the lead');
  if ((isAuthor || isStaff) && /jev_paid_enabled/i.test(target)) block('only coord enables paid Jev research calls');
  if ((isAuthor || isStaff) && /Trading-data/i.test(target)) block(`the shared data store C:/Projects/Trading-data is read-only for ${who}`);
  if (isStaff && new RegExp(CODE_PATH, 'i').test(target)) block(STAFF_CODE);
  if (isAuthor) checkAuthorCode(target, input);
}

// Code, tests and kits in the main checkout or a team worktree (Windows or Git Bash form); copies in a
// scratchpad are not covered, so the reviewer can still run mutants on its own copy.
const CODE_PATH = String.raw`(?:[A-Za-z]:|/[A-Za-z])[\\/]Projects[\\/]Trading[\\/](?:\.claude[\\/]worktrees[\\/][^\\/\s"']+[\\/])?(?:(?:trading_lab|tools|tests|web|deployment|exports)(?:[\\/]|["'\s]|$)|[^\\/\s"']+\.(?:py|pyw|cjs|js|cmd|bat|ps1)(?:["'\s]|$))`;
const STAFF_CODE = 'ops and reviewer change no code, tests or kits (D137); propose the change to platform through coord';
const COPY_VERB = /^(cp|copy|xcopy|robocopy|Copy-Item|cpi)(\.exe)?$/i;
const WRITE_VERB = /^(mv|move|Move-Item|mi|Rename-Item|rni|rm|rmdir|del|erase|Remove-Item|ri|tee|Tee-Object|touch|mkdir|md|ni|New-Item|Set-Content|sc|Add-Content|ac|Out-File|Clear-Content|clc)(\.exe)?$/i;
// Shell writes into repository code: a redirect or sed -i into it, a file-changing verb naming it, a copy
// whose destination is in it, or git applying changes to it. Copying code OUT to a scratchpad is allowed.
function writesCode(cmd, ps) {
  const code = new RegExp(CODE_PATH, 'i');
  if (!code.test(cmd)) return false;
  if (new RegExp(String.raw`>>?\s*["']?` + CODE_PATH, 'i').test(cmd)) return true;
  if (new RegExp(String.raw`\bsed\b[^\n|;&]*\s-i\b[^\n|;&]*` + CODE_PATH, 'i').test(cmd)) return true;
  for (const seg of segments(cmd, ps)) {
    if (!code.test(seg)) continue;
    const words = shellWords(seg, ps).filter((w) => !w.sep).map((w) => w.value);
    let i = 0;
    while (i < words.length && /^(xargs|sudo|exec|&)$/i.test(words[i])) i++;
    const verb = (words[i] || '').replace(/^.*[\\/]/, '');
    const operands = words.slice(i + 1).filter((w) => !w.startsWith('-') && !/^\/[A-Za-z]+(:\S*)?$/.test(w));
    if (WRITE_VERB.test(verb) && operands.some((w) => code.test(w))) return true;
    if (COPY_VERB.test(verb) && operands.length && code.test(operands[/^robocopy/i.test(verb) ? Math.min(1, operands.length - 1) : operands.length - 1])) return true;
    if (/^git(\.exe)?$/i.test(verb) && /^(apply|am|checkout|restore|mv|rm)$/i.test(operands[0] || '')) return true;
  }
  return false;
}

// Browsers act with real sign-ins: Claude in Chrome is the owner's own Chrome, with the Remote Desktop tab
// to the always-on PC that trades real money. URLs, typed text, keys, scripts and form values are checked
// like a command: no owner-only route or launcher and no secret file. Only ops drives the owner's Chrome
// (sessions without a role marker, the lead and coord, may too). During an install the owner approved,
// ops may type the package check and the panel launcher on the other PC (D137, variant A).
const OPS_LAUNCHERS = /\b(CHECK_WINDOWS_PACKAGE|START_WINDOWS_PANEL)\.cmd\b/gi;
function checkBrowser(tool, input, role) {
  const chrome = CHROME_TOOL.test(tool);
  if (chrome && role && role !== 'ops') block("only the ops session drives the owner's Chrome and the Remote Desktop tab (D137)");
  for (const s of strings(input, 0)) {
    const secret = secretPathMentioned(s);
    if (secret) block(`${secret} access`);
    const text = chrome && role === 'ops' ? s.replace(OPS_LAUNCHERS, '[launcher]') : s;
    for (const [re, why] of RULES) if (re.test(text)) block(why);
  }
}

const RULES = [
  [/one_trade(\.py)?\b[^\n]*--live\b/i, 'one_trade --live is owner-only'],
  [/--web-run\b/i, 'live --web-run is owner-only'],
  [/\/api\/exchange\/(start|stop|resume)\b/i, 'exchange Start/Stop/Resume is owner-only'],
  [/\/api\/(play|stop)\b/i, 'legacy /api/play and /api/stop are owner-only'],
  // the file system ignores case, so the launchers must be matched without it too
  [/\b(START|RESTART|INSTALL|IMPORT|EXPORT|REISSUE|CHECK)_[A-Z0-9_]+\.cmd\b/i, 'root .cmd launchers are owner-only'],
  [/restart_panel\.ps1/i, 'production panel restart is owner-only'],
  [/\bgit\s+reset\s+[^\n]*--hard\b/i, 'git reset --hard is forbidden'],
  [/\bgit\s+clean\s+-[A-Za-z]*f/i, 'git clean -f is forbidden'],
  [/\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)/i, 'discarding all working-tree changes is forbidden'],
  [/FromBase64String|\bbase64\b[^\n|]*\s(-d|--decode)\b[^\n]*\|\s*(ba|z|da)?sh\b/i, 'decoding and running hidden commands is forbidden'],
  [/(^|[\s;&|(])(https?|all)_proxy\s*=|\$env:(https?|all)_proxy\s*=/i, 'proxy overrides are owner-only'],
];

function checkShell(raw, ps, base, role, isAuthor, isStaff) {
  // the doc-stripping regexes stay fast only on commands of sane size
  if (raw.length > MAX_COMMAND || (raw.match(/<</g) || []).length > 64) block('the command is too long or complex for the guard; split it');
  const cmd = scanText(raw, ps);
  const segs = segments(cmd, ps);
  // Search, echo and git-read text is data, unless something on the line runs code.
  const runsCode = segs.some((s) => EXEC_SEG.test(s));
  const active = runsCode ? cmd : segs.filter((s) => !dataSegment(s, ps) && !(GIT_READ_SEG.test(s) && inert(s, ps))).join('\n');
  for (const [re, why] of RULES) if (re.test(active)) block(why);
  if (encodedPowershell(segs, ps)) block('encoded PowerShell commands are forbidden');
  checkGit(active, ps, role, isAuthor);
  checkXargs(cmd, ps);
  if (writesTo(cmd, GIT_FILE, ps)) block('git config, refs, hooks and shell profiles are owner-only');
  if (role && writesTo(cmd, /\.team-role\b/i, ps)) block('role markers are set by the lead');
  if (role && writesTo(cmd, GUARD_FILES, ps)) block('the guard and the Claude settings are changed only by the lead');
  for (const seg of segs) {
    if (LISTING_SEG.test(seg)) continue;
    const secret = secretPathMentioned(searchOperands(seg));
    if (secret) block(`${secret} access`);
  }
  if (!isAuthor && !isStaff) return;
  const who = isAuthor ? 'authors' : role;
  // a grep pattern naming the vault reads nothing in it; its file operands, or a pattern file, do
  if (segs.some((s) => /Trading-vault/i.test(/\s(-f|--file)\b/.test(s) ? s : searchOperands(s)))) block(`${who} may not access the SEALED vault`);
  if (aliasedPath(cmd)) block(`${who} may not use 8.3 short names or aliased paths`);
  if (runs(cmd, 'public_history')) block(`${who} do not download market data; platform provides DEV data`);
  if (runs(cmd, 'news_recorder')) block('only the platform runs the news recorder');
  if (writesSharedData(cmd) || CODE_WRITES_SHARED_DATA.test(cmd)) block(`the shared data store C:/Projects/Trading-data is read-only for ${who}`);
  if (isAuthor) {
    checkAuthorJevCommand(cmd, base, role, ps);
    return;
  }
  // ops and reviewer: no Jev calls of any kind, and no writes into code, tests or kits
  if (runs(cmd, 'jev_research') || runs(cmd, 'jev_lab') || runs(cmd, 'jev_gate_probe') ||
      (/(^|[\s;&|(])(py|python3?|pythonw|node)(\.exe)?(\s|$)/i.test(cmd) && DIRECT_JEV.test(cmd))) {
    block(`${role} makes no Jev calls (D137)`);
  }
  if (writesTo(cmd, /jev_paid_enabled/i, ps)) block('only coord enables paid Jev research calls');
  if (writesCode(cmd, ps)) block(STAFF_CODE);
}

// Documentation text is data, not a command: drop heredoc bodies that are only written to a file
// (cat/tee/git commit -F -), PowerShell here-strings written with Set-Content/Out-File, and commit,
// merge, tag, stash and notes messages. Heredocs and here-strings fed to an interpreter or written to a
// script file are still scanned, and so are the $(...) substitutions the shell runs inside them.
const INTERPRETER = /\b(bash|sh|zsh|dash|ksh|python3?|py|node|pwsh|powershell|cmd|perl|ruby|iex|Invoke-Expression)\b/i;
const SUBST = /\$\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)|`[^`\n]*`/g;
const HERE_STRING = /@(['"])\r?\n[\s\S]*?\r?\n\1@/g;
const MESSAGE = /(\bgit(?:\s+-\S+)*\s+(?:commit|merge|tag|stash|notes)\b[^;&|\n]*?\s(?:-[a-zA-Z]*m|--message)(?:=|\s+))("(?:[^"\\]|\\.)*"|'[^']*')/;
function keepSubst(body) {
  const found = body.match(SUBST);
  return found ? ' ; ' + found.join(' ; ') : '';
}
function scanText(cmd, ps) {
  let out = cmd.replace(/<<-?[ \t]*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1([^\n]*)\n([\s\S]*?)\n[ \t]*\2[ \t]*(?=\r?\n|$)/g,
    (match, quote, tag, rest, body, offset, whole) => {
      // judge only the command that owns the heredoc, not other commands on the same line
      const before = whole.slice(whole.lastIndexOf('\n', offset) + 1, offset).split(/;|&&|\|\||&/).pop();
      const own = before + rest.split(/;|&&|\|\||&/)[0];
      if (INTERPRETER.test(own) || scriptTarget(own)) return match;
      return `<<${tag} [doc]${quote ? '' : keepSubst(body)}`;
    });
  if (!/\b(Invoke-Expression|iex)\b/i.test(out) && !scriptTarget(out.replace(HERE_STRING, ' '))) {
    out = out.replace(HERE_STRING, (match, quote, offset, whole) => {
      const after = whole.slice(offset + match.length, offset + match.length + 80);
      if (/^\s*\|\s*&?\s*["']?(bash|sh|zsh|dash|ksh|python3?|py|node|pwsh|powershell|cmd|perl|ruby)\b/i.test(after)) return match;
      return `@[doc]@${quote === '"' ? keepSubst(match) : ''}`;
    });
  }
  out = stripComments(out, ps);
  // a double-quoted message that substitutes a command runs it, so only plain messages are masked
  for (let i = 0; i < 50; i++) {
    let changed = false;
    out = out.replace(MESSAGE, (all, pre, msg) => {
      SUBST.lastIndex = 0;
      if (msg[0] === '"' && SUBST.test(msg) && !/^"\$\(cat <<\w+ \[doc\]\s*\)"$/.test(msg)) return all;
      changed = true;
      return pre + '[msg]';
    });
    if (!changed) break;
  }
  return out;
}

// A # that starts a word (after whitespace, ; or a line start) is a comment to the end of the line in
// bash and PowerShell. Not after { or $, so ${#var} and $# stay code.
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

// xargs appends arguments from stdin, so under it git may only run a read-only subcommand.
const GIT_SAFE_UNDER_XARGS = new Set(['status', 'log', 'diff', 'show', 'ls-files', 'rev-parse', 'blame', 'grep', 'for-each-ref',
  'check-ignore', 'merge-base', 'cat-file', 'shortlog', 'describe', 'rev-list', 'ls-tree', 'name-rev']);
function checkXargs(cmd, ps) {
  for (const seg of segments(cmd, ps)) {
    const words = (seg.match(/"[^"]*"|'[^']*'|\S+/g) || []).map((s) => s.replace(/^["']|["']$/g, ''));
    let i = words.findIndex((w) => /^xargs(\.exe)?$/i.test(w));
    if (i < 0) continue;
    for (i++; i < words.length && words[i].startsWith('-');) i += /^-[InLPdasEe]$/.test(words[i]) ? 2 : 1;
    const prog = (words[i] || '').replace(/^.*[\\/]/, '').replace(/\.exe$/i, '').toLowerCase();
    if (/^(sh|bash|zsh|dash|pwsh|powershell|cmd|py|python3?|node)$/.test(prog) && words.slice(i + 1).some((w) => /\bgit\b/i.test(w))) {
      block('git through xargs may only read');
    }
    if (prog !== 'git') continue;
    let j = i + 1;
    while (j < words.length && words[j].startsWith('-')) j += GIT_VALUE_OPTS.has(words[j]) ? 2 : 1;
    if (!GIT_SAFE_UNDER_XARGS.has((words[j] || '').toLowerCase())) block('git through xargs may only read');
  }
}

// Walk text the way the shell groups it: inside '…' or "…" the separators ; & | and the comment #
// are literal, and an escaped quote (\" in bash, `" in PowerShell, '' inside a PowerShell '…', \' in a
// bash $'…') does not open or close a string. cb(ch, active) sees active=true only for an unquoted,
// unescaped character, so a separator or a comment # can no longer be hidden behind an escaped quote.
// ps picks the shell: bash escapes with \ (and with \ inside $'…'), PowerShell with ` and '' doubling.
function eachShellChar(text, ps, cb) {
  let quote = '';
  let ansiC = false; // bash $'…': a backslash escapes the next character, including the closing quote
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
    if (active && (c === '\n' || c === ';' || c === '&' || c === '|')) { out.push(cur); cur = ''; }
    else cur += c;
  });
  out.push(cur);
  return out;
}
// Characters inside quotes or escaped are turned into spaces, so a rule can match only the real,
// unquoted command and its flags (a -e inside a -Command "…" string is not the -e flag itself).
function maskQuoted(seg, ps) {
  let out = '';
  eachShellChar(seg, ps, (c, active) => { out += active ? c : ' '; });
  return out;
}
// Encoded PowerShell (-e/-ec/-en/-enc/-EncodedCommand) hides the command it runs. Checked per segment
// so a quoted ; cannot pad it out of range, on the masked text so a -e inside quotes is ignored, and
// by token so a quoted executable name ("powershell") is still caught.
const PS_BIN = /(^|[\\/])(pwsh|powershell)(\.exe)?$/i;
const ENC_FLAG = /^-(e|ec|en|enc|encodedcommand)$/i;
const ENC_MASKED = /(^|[\s;&|(])(pwsh|powershell)(\.exe)?(\s[^\n]*?)?\s-(e|ec|en|enc|encodedcommand)\b/i;
function encodedPowershell(segs, ps) {
  for (const seg of segs) {
    const words = shellWords(seg, ps);
    // powershell must be the command of the segment, not a search pattern (grep -e powershell) or an
    // argument; skip leading separators and bash VAR=value assignments to find the command word.
    let i = 0;
    while (i < words.length && (words[i].sep || (!ps && /^[A-Za-z_]\w*=/.test(words[i].value)))) i++;
    if (i >= words.length || !PS_BIN.test(words[i].value)) continue;
    // then catch -enc among its flags, on the masked text (a quoted ; or $'…' cannot pad it out of
    // range) and by token (a quoted executable name is still the command).
    if (ENC_MASKED.test(maskQuoted(seg, ps))) return true;
    for (let j = i + 1; j < words.length && !words[j].sep; j++) if (ENC_FLAG.test(words[j].value)) return true;
  }
  return false;
}
const LEAD = String.raw`^\s*[({]*\s*(&\s*)?["']?`;
const EXEC_SEG = new RegExp(LEAD + String.raw`((bash|sh|zsh|dash|ksh|pwsh|powershell|cmd|eval|exec|source|xargs|iex|Invoke-Expression|Invoke-Command|icm|Start-Process|start|py|python3?|pythonw|node|perl|ruby|awk|gawk|curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm)(\.exe)?|\.|[^\s"']+\.(ps1|sh|bash|py|cmd|bat))["']?(\s|$)`, 'i');
// search, print and PowerShell text writers: their text is data unless it goes into a script file
const DATA_SEG = new RegExp(LEAD + String.raw`(grep|egrep|fgrep|rg|findstr|Select-String|sls|echo|printf|Write-Output|Write-Host|where|which|Get-Command|gcm|Add-Content|ac|Set-Content|sc|Out-File)(\.exe)?(\s|$)`, 'i');
// read-only git with no inline config: its patterns and paths are data too
const GIT_READ_SEG = /^\s*[({]*\s*git(\.exe)?\s+(-C\s+("[^"]*"|'[^']*'|\S+)\s+|--no-pager\s+|-P\s+)*(log|show|diff|blame|grep|ls-files|cat-file|shortlog|status|rev-parse|for-each-ref)\b/i;
const LISTING_SEG = /^\s*[({]*\s*(ls|dir|Get-ChildItem|gci|Test-Path|git(\.exe)?\s+(-C\s+("[^"]*"|'[^']*'|\S+)\s+)?(status|check-ignore))(\s|$)/i;
const READ_VERB = /^(cat|type|head|tail|less|more|grep|egrep|fgrep|rg|findstr|Get-Content|gc|Select-String|sls|ls|dir|Get-ChildItem|gci|Test-Path|stat|wc|file|git|find(?![^\n]*\s-(exec|execdir|delete|ok|okdir|fprint\w*|fls)\b)|sha\d*sum|md5sum|cksum|Get-FileHash|Get-Item|gi|Get-ItemProperty|gp|Get-Acl)(\.exe)?(\s|$)/i;
// Text runs nothing only without substitutions: $( ), bash backticks, or PowerShell ( ) outside quotes,
// which executes the command inside (echo (git push ...)).
function inert(seg, ps) {
  if (/\$\(/.test(seg) || (!ps && /`/.test(seg))) return false;
  let paren = false;
  eachShellChar(seg, ps, (c, active) => { if (active && c === '(') paren = true; });
  return !paren;
}
function dataSegment(seg, ps) {
  return DATA_SEG.test(seg) && inert(seg, ps) && !scriptTarget(seg);
}
// For grep/rg/findstr/Select-String the pattern is text, not a file; echo text is not a file either.
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
// A redirect into a matching path, or a segment naming one whose command is not a plain read.
function writesTo(cmd, re, ps) {
  for (const seg of segments(cmd, ps)) {
    if (!re.test(seg)) continue;
    const targets = (seg.match(/>>?\s*("[^"]*"|'[^']*'|[^\s|;&]+)/g) || [])
      .concat(seg.match(/--output(=|\s+)("[^"]*"|'[^']*'|[^\s|;&]+)/g) || []);
    if (targets.some((t) => re.test(t.replace(/^(>>?|--output=?)\s*/, '')))) return true;
    if (!READ_VERB.test(seg.replace(/^[\s({&]+/, ''))) return true;
  }
  return false;
}
// Linear on purpose: a write operator, then a script extension later in the same segment.
const WRITE_OP = />>?|\btee\b|\b(Set-Content|Add-Content|Out-File|New-Item)\b/i;
const SCRIPT_EXT = /\.(sh|bash|zsh|ps1|psm1|py|pyw|js|cjs|mjs|cmd|bat)\b/i;
function scriptTarget(text) {
  for (const seg of String(text).split(/[\n|;&]/)) {
    const m = WRITE_OP.exec(seg);
    if (m && SCRIPT_EXT.test(seg.slice(m.index))) return true;
  }
  return false;
}
function aliasedPath(text) {
  return /[\\/][^\\/\s"']{1,6}~\d([\\/\s"']|$)/.test(text) ||
    /\b(subst|mklink|New-PSDrive|ndr)\b|\bfsutil\s+hardlink\b|\bNew-Item\b[^\n]*-ItemType\s+["']?(Junction|SymbolicLink|HardLink)/i.test(text);
}

// Writes into the shared data store: redirections into it, or file-changing commands naming it.
function writesSharedData(cmd) {
  if (/>>?\s*["']?[^\s"'|;&]*Trading-data/i.test(cmd)) return true;
  // the verb must be a command word, not the .md of NOTEBOOK_UA.md or a word inside a path
  if (/(^|[\n|;&({]\s*|\b(xargs|sudo|exec)\s+)(cp|mv|rm|rmdir|del|erase|copy|move|xcopy|robocopy|tee|touch|mkdir|md|ni|Copy-Item|cpi|Move-Item|mi|Remove-Item|ri|Rename-Item|rni|Set-Content|sc|Add-Content|ac|Out-File|New-Item|Clear-Content|clc)(\.exe)?\s[^\n|;&]*Trading-data/i.test(cmd)) return true;
  if (/\|\s*(tee|Tee-Object|Out-File|Set-Content|Add-Content)\b[^\n|;&]*Trading-data/i.test(cmd)) return true;
  if (/\bsed\b[^\n|;&]*\s-i\b[^\n|;&]*Trading-data/i.test(cmd)) return true;
  return false;
}
// Inline or file code that writes into the shared data store (best effort: a path in a variable escapes it).
const CODE_WRITES_SHARED_DATA = new RegExp([
  String.raw`\bopen\s*\(\s*[^)\n]*Trading-data[^)\n]*,\s*(mode\s*=\s*)?[rbu]?['"][^'"]*[wax+]`,
  String.raw`Path\s*\(\s*[^)\n]*Trading-data[^)\n]*\)\s*\.\s*(write_text|write_bytes|touch|mkdir|unlink|rename|replace|rmdir)`,
  String.raw`(shutil\s*\.\s*(copy\w*|move)|os\s*\.\s*(rename|replace))\s*\([^,\n]*,\s*[^)\n]*Trading-data`,
  String.raw`(os\s*\.\s*(remove|unlink|rmdir|makedirs|mkdir)|shutil\s*\.\s*rmtree)\s*\(\s*[^)\n]*Trading-data`,
  String.raw`(writeFileSync|appendFileSync|writeFile|appendFile|createWriteStream|mkdirSync|rmSync|unlinkSync|rmdirSync)\s*\(\s*[^)\n]*Trading-data`,
  String.raw`(renameSync|copyFileSync|cpSync)\s*\([^,\n]*,\s*[^)\n]*Trading-data`,
  String.raw`\.to_(csv|parquet|json|pickle|feather|hdf|excel)\s*\(\s*[^)\n]*Trading-data`,
  String.raw`\[(System\.)?IO\.(File|Directory)\]::(Write\w*|Append\w*|Create\w*|Delete|Move|Copy|Replace)\s*\([^)\n]*Trading-data`,
].join('|'), 'i');
function runs(cmd, name) {
  const invoked = new RegExp(String.raw`(^|[\s;&|(])(py|python3?|pythonw|node)(\.exe)?\s[^\n|;&]*?(${name}\.py\b|-m\s+(tools\.)?${name}\b)`, 'i');
  if (invoked.test(cmd)) return true;
  const reads = /[^\n|;&]*\b(grep|egrep|fgrep|rg|findstr|Select-String|sls|git|cat|head|tail|less|more|Get-Content|gc|type|wc|sha\d*sum|Get-FileHash|diff)\b[^\n|;&]*/gi;
  return new RegExp(String.raw`(^|[\s;&|(]|[\\/])${name}\.py(\s|$)`, 'i').test(cmd.replace(reads, ''));
}

// Push to origin is pre-approved in .claude/settings.json (D123), so this check is what keeps it safe.
// Commands are split into words the way the shell does, so quotes, escapes, abbreviated options and
// nested `bash -c "..."` strings can't hide a force push, mirror, prune or remote ref deletion. Every
// push goes to origin with literal arguments and no inline config. Remotes, git config, refs, hooks,
// git environment overrides and stored credentials are owner-only, so an approved push can't be
// redirected, run code or print the token. Authors never move shared refs (main, tags, replace refs:
// the news anchors in main depend on that) and push only their own branch, never with a destination.
const GIT_FILE = /(^|[\\/\s"'=~])(\.git[\\/](config|hooks|remotes|branches|refs([\\/]|\b)|packed-refs|HEAD\b|info[\\/]grafts|objects[\\/]info[\\/]alternates|worktrees[\\/][^\\/\s"']+[\\/](config|HEAD|commondir|gitdir))|\.gitconfig\b|\.config[\\/]git[\\/]|etc[\\/]gitconfig\b|\.bashrc\b|\.bash_profile\b|\.bash_login\b|\.profile\b|\.zshrc\b|\.zprofile\b)|[\w.]*_profile\.ps1\b/i;
const GUARD_FILES = /(^|[\\/\s"'=])(team[\\/]tools[\\/](test_)?guard\.cjs|\.claude[\\/](settings(\.local)?\.json|hooks([\\/]|\b)))/i;
const GIT_ENV_NAMES = String.raw`CONFIG\w*|DIR|WORK_TREE|COMMON_DIR|INDEX_FILE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|REPLACE_REF_BASE|NAMESPACE|SSH\w*|ASKPASS|EXEC_PATH|TEMPLATE_DIR|PROXY_COMMAND|ALLOW_PROTOCOL|PROTOCOL_FROM_USER|TRACE\w*|CURL_VERBOSE|SSL\w*|HTTP_\w+|REDACT\w*|CEILING_DIRECTORIES|DISCOVERY_ACROSS_FILESYSTEM`;
const GIT_ENV = new RegExp(String.raw`(\$env:|^|[\s;&|(])GIT_(${GIT_ENV_NAMES})\s*=|\b(setx\s+|(Set-Item|New-Item|si|ni)\s+(-Path\s+)?env:)GIT_(${GIT_ENV_NAMES})\b|SetEnvironmentVariable\(\s*["']GIT_`, 'i');
const CRED_STORE = /\b(cmdkey|vaultcmd)(\.exe)?\b|PasswordVault|\bCredRead|Get-StoredCredential|\bCredentialManager\b/i;
const GIT_BIN = /(^|[\\/])git(\.exe)?$/i;
const GIT_LOWLEVEL_BIN = /(^|[\\/])git-(send-pack|http-push|receive-pack|credential[\w-]*|remote-[\w-]+)(\.exe)?$/i;
const GIT_VALUE_OPTS = new Set(['-c', '-C', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env', '--attr-source', '--list-cmds']);
const PUSH_SAFE_GLOBALS = new Set(['-C', '-P', '--no-pager', '-p', '--paginate', '--no-optional-locks']);
const PUSH_DANGER = ['force', 'force-with-lease', 'force-if-includes', 'mirror', 'delete', 'prune', 'receive-pack', 'exec', 'repo'];
const UNSAFE_PUSH = 'force push, mirror, prune and remote ref deletion are forbidden';
const INLINE_DANGER = /^\s*(alias|core\.(hookspath|sshcommand|fsmonitor|askpass|gitproxy)|credential|include|url|remote|protocol)\b/i;

function checkGit(cmd, ps, role, isAuthor) {
  scanShell(cmd, ps, 0, (call) => checkGitCall(call, role, isAuthor));
  if (GIT_ENV.test(cmd)) block('git environment overrides (GIT_CONFIG*, GIT_DIR, GIT_SSH*, GIT_TRACE*, ...) are owner-only');
  if (CRED_STORE.test(cmd)) block('stored credentials are owner-only');
  if (/["'`]git(\.exe)?["'`]\s*,\s*\[?\s*["'`]push["'`]/i.test(cmd)) block('git push from code is forbidden; run git push origin ... directly');
  if (/\b(alias\s+[\w-]+=\s*["']?|(Set|New)-Alias\s+(-Name\s+)?\S+\s+(-Value\s+)?)git\b/i.test(cmd)) block('shell aliases for git are forbidden');
}

// Shell words with quotes and escapes resolved (`\` in bash, backtick in PowerShell). Separators are
// kept as their own entries. Words that still hold spaces or shell syntax are scanned again as
// commands in both shells: bash -c "...", pwsh -Command '...', "$(...)", subprocess strings.
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
      else if (quote === '"' && esc && i + 1 < text.length) { value += text[++i]; raw += text[i]; }
      else value += c;
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

function scanShell(text, ps, depth, onCall) {
  const words = shellWords(text, ps);
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (w.sep) continue;
    if (GIT_LOWLEVEL_BIN.test(w.value)) block('low-level git push and credential programs are forbidden');
    const next = words[i + 1];
    if (w.raw.includes('$') && next && !next.sep && next.value.toLowerCase() === 'push') block('git must be called by name, not through a variable');
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
    // bare arguments are not commands (quoted ones are rescanned below), so skip them: linear time
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

function checkGitCall(call, role, isAuthor) {
  const { opts, sub, args } = call;
  const vals = args.map((a) => a.value);
  const first = (vals.find((v) => !v.startsWith('-')) || '').toLowerCase();
  for (const o of opts) {
    if ((o.name === '-c' || o.name === '--config-env') && INLINE_DANGER.test(o.value)) block('inline git config for aliases, hooks, credentials or remotes is forbidden');
  }
  if (sub === 'push') {
    checkPush(call, isAuthor || STAFF.has(role));
    return;
  }
  if (['send-pack', 'http-push', 'receive-pack', 'filter-branch', 'filter-repo', 'replace', 'fast-import'].includes(sub) || sub.startsWith('remote-')) {
    block(`git ${sub} is owner-only`);
  }
  if (sub.startsWith('credential')) block('git credential helpers print the GitHub token; they are owner-only');
  if (sub === 'subtree' && first === 'push') block('git subtree push is forbidden; push branches with git push origin');
  if (sub === 'remote' && /^(add|set-url|rename|remove|rm|set-branches)$/.test(first)) block('changing git remotes is owner-only');
  if (sub === 'config') checkConfig(vals, first);
  if ((sub === 'init' || sub === 'clone') && vals.some((v) => /^--(template|separate-git-dir|config)\b/.test(v) || v === '-c')) {
    block('git init or clone with templates, a separate git dir or config is owner-only');
  }
  if (sub === 'reflog' && /^(expire|delete)$/.test(first)) block('deleting reflog history is forbidden');
  if (sub === 'symbolic-ref' && (vals.filter((v) => !v.startsWith('-')).length >= 2 || vals.some((v) => /^(-d|--delete)$/.test(v)))) {
    block('changing symbolic refs is owner-only');
  }
  if (sub === 'reset' && vals.includes('--hard')) block('git reset --hard is forbidden');
  if (sub === 'clean' && vals.some((v) => /^-[A-Za-z]*f/.test(v) || v === '--force')) block('git clean -f is forbidden');
  if ((sub === 'checkout' || sub === 'restore') && (vals[0] === '.' || (vals[0] === '--' && vals[1] === '.'))) {
    block('discarding all working-tree changes is forbidden');
  }
  if (!role) return;
  if (sub === 'update-ref') block('only coord manages branches and tags');
  if (sub === 'branch' && vals.some((v) => /^-[A-Za-z]*[fdDmMcC]/.test(v) || /^--(force|delete|move|copy)\b/.test(v))) {
    block('only coord manages branches and tags');
  }
  if (sub === 'tag' && !tagListing(vals)) block('only coord creates, moves or deletes tags');
  if ((sub === 'checkout' && vals.some((v) => /^-[A-Za-z]*B/.test(v))) ||
      (sub === 'switch' && vals.some((v) => /^-[A-Za-z]*C/.test(v) || v === '--force-create')) ||
      (sub === 'worktree' && vals.some((v) => /^-[A-Za-z]*B/.test(v)))) {
    block('only coord resets branches');
  }
  if (sub === 'fetch' && vals.some((v) => !v.startsWith('-') && /[:+]/.test(v))) block('authors fetch without refspecs; coord updates main');
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
  // `git config <key>` with a single operand is a read: git infers the action from the operands
  if (!writes && operands.length === 1 && /^[\w-]+(\.[^\s=]+)?\.[\w-]+$/.test(operands[0])) return;
  block('changing git config is owner-only; read it with git config --get or --list');
}

function checkPush(call, isAuthor) {
  const { opts, args, end, ps } = call;
  for (const o of opts) if (!PUSH_SAFE_GLOBALS.has(o.name)) block(`git ${o.name} with push is forbidden; push plainly with git push origin ...`);
  if (['(', '{', '[', '`'].includes(end)) block('git push arguments must be literal');
  const positional = [];
  let endOfOpts = false;
  for (let i = 0; i < args.length; i++) {
    const v = args[i].value;
    const raw = args[i].raw;
    if (/^[*\d]?(>>?|<)/.test(v)) {
      if (/^[*\d]?(>>?|<)$/.test(v)) i++;
      continue;
    }
    if (raw.includes('$') || (!ps && raw.includes('`'))) block('git push arguments must be literal (no variables or substitution)');
    if (!endOfOpts && v === '--') { endOfOpts = true; continue; }
    if (!endOfOpts && v.startsWith('--')) {
      // git accepts any unique prefix of a long option (gitcli), so --mirr is --mirror
      const name = v.slice(2).split('=')[0].toLowerCase();
      if (PUSH_DANGER.some((d) => d.startsWith(name))) block(UNSAFE_PUSH);
      if (!v.includes('=') && ['push-option', 'recurse-submodules'].some((d) => d.startsWith(name))) i++;
      continue;
    }
    if (!endOfOpts && /^-./.test(v)) {
      for (let c = 1; c < v.length; c++) {
        if (v[c] === 'f' || v[c] === 'd') block(UNSAFE_PUSH);
        if (v[c] === 'o') {
          if (c === v.length - 1) i++;
          break;
        }
      }
      continue;
    }
    positional.push(v);
  }
  const [remote, ...refspecs] = positional;
  if (remote !== undefined && remote !== 'origin') block('the team pushes only to origin');
  for (const r of refspecs) {
    if (r.startsWith('+') || r.startsWith(':')) block(UNSAFE_PUSH);
    if (isAuthor && r.includes(':')) block('authors push only their own branch (git push -u origin HEAD); coord updates main');
  }
}

// Paid Jev research goes only through tools/jev_research.py and its per-author ledger (G7, D107).
const PAID_FLAG = 'C:/Projects/Trading/runtime/team/jev_paid_enabled.flag';
const MAIN_HARNESS = 'C:/Projects/Trading/tools/jev_research.py';
const DIRECT_JEV = /\b(send_once|JevClient|BudgetLedger)\b|openrouter\.ai|\b(import|from)\s+openrouter\b|alpha\/decisions/i;

// sha256 over bytes with only CRLF->LF normalised, as the core hashes (D098).
function lfSha256(file) {
  const crypto = require('crypto');
  return crypto.createHash('sha256').update(fs.readFileSync(file, 'latin1').replace(/\r\n/g, '\n'), 'latin1').digest('hex');
}

function hasFlag(cmd, flag) {
  return new RegExp(`(^|\\s)${flag}(?=\\s|=|$)`, 'i').test(cmd);
}

function checkAuthorJevCommand(rawCmd, base, role, ps) {
  // Join shell line continuations and collapse whitespace so flags are matched as whole tokens.
  const cmd = rawCmd.replace(/\\\r?\n/g, ' ').replace(/`\r?\n/g, ' ').replace(/[ \t]+/g, ' ');
  if (/(^|[\s;&|(])(py|python3?|pythonw|node)(\.exe)?\s[^\n|;&]*tools[\\/]jev_[^\s]*[*?[]/i.test(cmd)) {
    block('globs in Jev tool paths are not allowed; name tools/jev_research.py explicitly');
  }
  const asks = /jev_research(\.py)?\b[^\n|;&]*\bask\b/i.test(cmd) && !hasFlag(cmd, '--help') && !hasFlag(cmd, '-h');
  if (asks && hasFlag(cmd, '--questions')) block('jev_research v1 CLI (--questions) is retired; use --spec of v1.1');
  if (asks) {
    const author = (cmd.match(/(^|\s)--author(?:=|\s+)([A-Za-z0-9_-]+)/i) || [])[2];
    if (!author || author.toLowerCase() !== role) block(`--author must be your own role (${role}); each author spends only their own Jev budget`);
  }
  if (asks && !hasFlag(cmd, '--dry-run')) {
    // Replay and paid calls only with the reviewed harness: the worktree copy must equal main's.
    let same = false;
    try { same = lfSha256(path.join(base, 'tools', 'jev_research.py')) === lfSha256(MAIN_HARNESS); } catch (e) { same = false; }
    if (!same) block('tools/jev_research.py in this worktree differs from main (or main has none yet): merge main before --replay or --paid');
  }
  if (/jev_lab\.py|jev_gate_probe\.py/i.test(cmd)) block('old Jev labs bypass the per-author research ledger; use tools/jev_research.py');
  if (/(^|[\s;&|(])(py|python3?|node)(\.exe)?(\s|$)/i.test(cmd) && DIRECT_JEV.test(cmd)) {
    block('direct Jev/OpenRouter calls bypass the per-author research ledger; use tools/jev_research.py (read code with the Read/Grep tools)');
  }
  if (writesTo(cmd, /jev_paid_enabled/i, ps)) block('only coord enables paid Jev research calls');
  if (/jev_research(\.py)?\b[^\n|;&]*\breconcile\b/i.test(cmd)) block('jev_research reconcile is coord-only');
  if (asks && !hasFlag(cmd, '--dry-run') && !hasFlag(cmd, '--replay') && !fs.existsSync(PAID_FLAG)) {
    block('paid Jev research calls are not enabled yet (D107: harness fix, re-review and coord confirmation first); use --dry-run or --replay');
  }
}

function checkAuthorCode(target, input) {
  if (!/\.(py|pyw|js|cjs|mjs|ts|ps1|psm1|sh|bash|cmd|bat|ipynb)(\s|$)/i.test(target)) return;
  const edits = Array.isArray(input.edits) ? input.edits : [];
  const text = [input.content, input.new_string, input.new_source]
    .concat(edits.map((e) => e && e.new_string))
    .filter(Boolean).map(String).join('\n');
  if (DIRECT_JEV.test(text)) block('author code may not call Jev/OpenRouter directly; use tools/jev_research.py');
  if (secretEnvMentioned(text) || /LocalSettings[\\/]/i.test(text)) block('author code may not read secret files');
  if (/\bgit\b[\s\S]{0,40}?\bpush\b/i.test(text)) block('author code may not run git push; push your branch with git push -u origin HEAD');
  if (CODE_WRITES_SHARED_DATA.test(text)) block('author code may not write into the shared data store C:/Projects/Trading-data');
}

let raw = '';
process.stdin.on('data', (d) => { raw += d; });
process.stdin.on('end', () => {
  try {
    check(JSON.parse(raw || '{}'));
  } catch (e) {
    process.exit(0);
  }
  process.exit(0);
});
