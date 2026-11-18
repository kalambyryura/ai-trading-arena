// Public strategy passport: a closed schema, approval gating by recipe hash, and a card that carries text only.
// Minimal fake DOM, no browser, network or keys. Invisible characters are built from code points so the file stays plain text.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Passport = require('../web/passport.js');

class FakeNode {
  constructor(tag) { this.tag = tag; this.className = ''; this._text = ''; this.children = []; this.attrs = {}; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
}
const fakeDocument = { createElement: tag => new FakeNode(tag) };
const walk = (node, found = []) => { found.push(node); node.children.forEach(child => walk(child, found)); return found; };
const text = node => node.textContent;
const NBSP = String.fromCharCode(0xa0);
const chr = code => String.fromCharCode(code);

const A = 'ab12'.repeat(16), B = 'cd34'.repeat(16), C = 'ef56'.repeat(16);
function base(overrides = {}) {
  return structuredClone({
    recipe_sha256: A, version: 2, replaces: B, name: 'Тестова стратегія', author: { name: 'Автор Тест', kind: 'model', model: 'test-model-1' },
    tagline: 'Коротко про ідею.', idea: 'Опис ідеї без обіцянок.', numbers: 'shown',
    waits_for: ['Чекає падіння на 0,35 %.'], when_silent: ['Майже завжди мовчить.'], exits: ['Стоп 1 %.'], risk: ['Один лот.'],
    weak_regimes: ['Слабка в падінні.'], jev: { role: 'gate', text: 'Може лише відхилити вхід.' },
    attempts: { count: 930, note: 'Перебрано варіанти на навчальних даних.' }, author_note: { text: 'Очікую близько до Cash.', signed_by: 'Автор Тест' },
    evidence: ['Модель дала мінус на угоду.'], evidence_asof: '2026-10-03', links: [{ label: 'Ліга', href: '/league' }],
    approval: { status: 'APPROVED', author_approved_at: '2026-10-03', owner_approved_at: '2026-10-04' }, ...overrides });
}
const draft = (overrides = {}) => base({ approval: { status: 'DRAFT', author_approved_at: '2026-10-03', owner_approved_at: null }, ...overrides });
const file = entries => ({ schema: 'arena-passports/v1', updated: '2026-10-03', passports: entries });
const problems = change => { const entry = base(); change(entry); return Passport.check(entry); };
const shipped = JSON.parse(fs.readFileSync(path.join(__dirname, '../web/passports.json'), 'utf8'));

test('the shipped file is sound and cannot show an unapproved card to the public', () => {
  const report = Passport.validate(shipped);
  assert.deepEqual(report.errors, []);
  assert.equal(report.ok, true);
  assert.equal(report.approved + report.drafts, shipped.passports.length);
  for (const entry of shipped.passports) {
    assert.deepEqual(Passport.mismatches(entry, { recipe_sha256: entry.recipe_sha256, name: entry.name, version: entry.version, author: { ...entry.author, attempts: entry.attempts.count } }), []);
    assert.ok(Passport.byRecipe(shipped, entry.recipe_sha256, { preview: true }), 'a sound entry is found for preview');
    const publicly = Passport.byRecipe(shipped, entry.recipe_sha256);
    assert.equal(publicly !== null, entry.approval.status === 'APPROVED', 'only an approved entry is public');
    assert.equal(Passport.render(fakeDocument, entry) !== null, entry.approval.status === 'APPROVED');
  }
  const forbidden = /prompt|original_text|api[_-]?key|secret|token|password|https?:|www\./i;
  assert.equal(forbidden.test(JSON.stringify(shipped).replace(/recipe_sha256|signed_by|author_approved_at/g, '')), false, 'no prompts, keys or links in the public file');
});

test('a sound passport has no problems, and the limits are inclusive', () => {
  assert.deepEqual(Passport.check(base()), []);
  const edges = {
    name: 'я'.repeat(60), tagline: 'я'.repeat(140), idea: 'я'.repeat(400), 'author.name': 'я'.repeat(80), 'author.model': 'я'.repeat(150),
    'attempts.note': 'я'.repeat(400), 'author_note.text': 'я'.repeat(400), 'author_note.signed_by': 'я'.repeat(80), 'jev.text': 'я'.repeat(300) };
  for (const [field, value] of Object.entries(edges)) {
    const entry = base(), keys = field.split('.');
    keys.slice(0, -1).reduce((target, key) => target[key], entry)[keys.at(-1)] = value;
    assert.deepEqual(Passport.check(entry), [], field + ' at its limit');
    keys.slice(0, -1).reduce((target, key) => target[key], entry)[keys.at(-1)] = value + 'я';
    assert.ok(Passport.check(entry).some(error => error.path === 'passport.' + field), field + ' one over its limit');
  }
  const full = base({ waits_for: Array(6).fill('я'.repeat(300)), when_silent: Array(5).fill('а'), exits: Array(6).fill('а'), risk: Array(5).fill('а'),
    weak_regimes: Array(4).fill('а'), evidence: Array(4).fill('а'), links: Array(3).fill({ label: 'я'.repeat(60), href: '/review?mode=OKX_LIVE&id=ab-12#top' }),
    version: 9999, attempts: { count: 1000000, note: 'а' } });
  assert.deepEqual(Passport.check(full), []);
  const sparse = base({ version: 1, attempts: { count: 0, note: 'а' } });
  delete sparse.replaces; delete sparse.weak_regimes; delete sparse.author_note; delete sparse.evidence; delete sparse.evidence_asof; delete sparse.links;
  assert.deepEqual(Passport.check(sparse), [], 'every optional part may be absent');
  assert.deepEqual(Passport.check({ ...sparse, attempts: { count: null, note: 'а' }, weak_regimes: [], evidence: [], links: [] }), []);
  const human = base({ author: { name: 'Людина', kind: 'human', model: '' } });
  assert.deepEqual(Passport.check(human), []);
  assert.deepEqual(Passport.check(base({ approval: { status: 'APPROVED', author_approved_at: '2026-10-03', owner_approved_at: '2026-10-03' } })), [], 'the author and the owner may approve on one day');
  assert.deepEqual(Passport.check(base({ author: { name: 'Приклад', kind: 'example' } })), []);
});

const BAD = [
  ['an unknown field at the top', entry => { entry.prompt = 'x'; }, 'passport.prompt'],
  ['an unknown nested field', entry => { entry.author.email = 'x'; }, 'passport.author.email'],
  ['an unknown field in approval', entry => { entry.approval.by = 'x'; }, 'passport.approval.by'],
  ['an uppercase hash', entry => { entry.recipe_sha256 = A.toUpperCase(); }, '.recipe_sha256'],
  ['a short hash', entry => { entry.recipe_sha256 = A.slice(1); }, '.recipe_sha256'],
  ['a non-hex hash', entry => { entry.recipe_sha256 = 'g'.repeat(64); }, '.recipe_sha256'],
  ['a missing hash', entry => { delete entry.recipe_sha256; }, '.recipe_sha256'],
  ['version zero', entry => { entry.version = 0; }, '.version'],
  ['a fractional version', entry => { entry.version = 1.5; }, '.version'],
  ['a text version', entry => { entry.version = '2'; }, '.version'],
  ['version 10000', entry => { entry.version = 10000; }, '.version'],
  ['replacing itself', entry => { entry.replaces = A; }, '.replaces'],
  ['a malformed replaced hash', entry => { entry.replaces = 'abc'; }, '.replaces'],
  ['an empty name', entry => { entry.name = ''; }, '.name'],
  ['a name with edge spaces', entry => { entry.name = ' Назва'; }, '.name'],
  ['a non-text name', entry => { entry.name = 5; }, '.name'],
  ['an unknown kind', entry => { entry.author.kind = 'robot'; }, '.author.kind'],
  ['a model author without a model', entry => { entry.author = { name: 'x', kind: 'model' }; }, '.author.model'],
  ['a human author naming a model', entry => { entry.author = { name: 'x', kind: 'human', model: 'gpt' }; }, '.author.model'],
  ['an author that is not an object', entry => { entry.author = 'x'; }, '.author'],
  ['an unknown numbers mode', entry => { entry.numbers = 'maybe'; }, '.numbers'],
  ['a missing numbers mode', entry => { delete entry.numbers; }, '.numbers'],
  ['an empty required list', entry => { entry.waits_for = []; }, '.waits_for'],
  ['a missing required list', entry => { delete entry.exits; }, '.exits'],
  ['a list that is not a list', entry => { entry.risk = 'one'; }, '.risk'],
  ['too many waits_for', entry => { entry.waits_for = Array(7).fill('а'); }, '.waits_for'],
  ['too many when_silent', entry => { entry.when_silent = Array(6).fill('а'); }, '.when_silent'],
  ['too many exits', entry => { entry.exits = Array(7).fill('а'); }, '.exits'],
  ['too many risk lines', entry => { entry.risk = Array(6).fill('а'); }, '.risk'],
  ['too many weak regimes', entry => { entry.weak_regimes = Array(5).fill('а'); }, '.weak_regimes'],
  ['too much evidence', entry => { entry.evidence = Array(5).fill('а'); }, '.evidence'],
  ['an over-long list item', entry => { entry.exits = ['я'.repeat(301)]; }, '.exits[0]'],
  ['a non-text list item', entry => { entry.exits = [5]; }, '.exits[0]'],
  ['a zero-width space', entry => { entry.idea = 'а' + chr(0x200b) + 'б'; }, '.idea'],
  ['a right-to-left override', entry => { entry.tagline = 'а' + chr(0x202e) + 'б'; }, '.tagline'],
  ['a soft hyphen', entry => { entry.idea = 'а' + chr(0xad) + 'б'; }, '.idea'],
  ['a byte order mark', entry => { entry.risk = [chr(0xfeff) + 'а']; }, '.risk[0]'],
  ['a line separator', entry => { entry.idea = 'а' + chr(0x2028) + 'б'; }, '.idea'],
  ['a newline', entry => { entry.idea = 'а\nб'; }, '.idea'],
  ['a tab', entry => { entry.tagline = 'а\tб'; }, '.tagline'],
  ['a control character', entry => { entry.idea = 'а' + chr(0x85) + 'б'; }, '.idea'],
  ['a web address', entry => { entry.idea = 'дивись https://evil.example/x'; }, '.idea'],
  ['a bare www address', entry => { entry.idea = 'дивись www.evil.example'; }, '.idea'],
  ['an e-mail address', entry => { entry.idea = 'пиши a@evil.example'; }, '.idea'],
  ['a key-like token', entry => { entry.idea = 'ключ ' + 'A1'.repeat(16); }, '.idea'],
  ['an unknown Jev role', entry => { entry.jev.role = 'trader'; }, '.jev.role'],
  ['an empty Jev text', entry => { entry.jev.text = ''; }, '.jev.text'],
  ['an unknown Jev field', entry => { entry.jev.model = 'x'; }, '.jev.model'],
  ['a missing Jev block', entry => { delete entry.jev; }, '.jev'],
  ['a negative attempts count', entry => { entry.attempts.count = -1; }, '.attempts.count'],
  ['a fractional attempts count', entry => { entry.attempts.count = 1.5; }, '.attempts.count'],
  ['a text attempts count', entry => { entry.attempts.count = '930'; }, '.attempts.count'],
  ['an attempts count over the limit', entry => { entry.attempts.count = 1000001; }, '.attempts.count'],
  ['a missing attempts count', entry => { delete entry.attempts.count; }, '.attempts.count'],
  ['an empty attempts note', entry => { entry.attempts.note = ''; }, '.attempts.note'],
  ['an author note without a signature', entry => { delete entry.author_note.signed_by; }, '.author_note.signed_by'],
  ['an unknown author note field', entry => { entry.author_note.mood = 'x'; }, '.author_note.mood'],
  ['evidence without a date', entry => { delete entry.evidence_asof; }, '.evidence_asof'],
  ['a date without evidence', entry => { delete entry.evidence; }, '.evidence_asof'],
  ['a date with empty evidence', entry => { entry.evidence = []; }, '.evidence_asof'],
  ['a date that does not exist', entry => { entry.evidence_asof = '2026-02-30'; }, '.evidence_asof'],
  ['a date with a time', entry => { entry.evidence_asof = '2026-10-03T00:00:00Z'; }, '.evidence_asof'],
  ['four links', entry => { entry.links = Array(4).fill({ label: 'а', href: '/league' }); }, '.links'],
  ['an external link', entry => { entry.links = [{ label: 'а', href: 'https://evil.example' }]; }, '.links[0].href'],
  ['a protocol-relative link', entry => { entry.links = [{ label: 'а', href: '//evil.example/x' }]; }, '.links[0].href'],
  ['a javascript link', entry => { entry.links = [{ label: 'а', href: 'javascript:alert(1)' }]; }, '.links[0].href'],
  ['a dot-segment link', entry => { entry.links = [{ label: 'а', href: '/league/../live' }]; }, '.links[0].href'],
  ['a link to the old live panel', entry => { entry.links = [{ label: 'а', href: '/live' }]; }, '.links[0].href'],
  ['a link with markup in the query', entry => { entry.links = [{ label: 'а', href: '/league?x=<b>' }]; }, '.links[0].href'],
  ['a link without a label', entry => { entry.links = [{ href: '/league' }]; }, '.links[0].label'],
  ['an unknown link field', entry => { entry.links = [{ label: 'а', href: '/league', rel: 'x' }]; }, '.links[0].rel'],
  ['an unknown status', entry => { entry.approval.status = 'PUBLISHED'; }, '.approval.status'],
  ['approved without an owner date', entry => { entry.approval.owner_approved_at = null; }, '.approval'],
  ['approved without an author date', entry => { entry.approval.author_approved_at = null; }, '.approval'],
  ['the author approving after the owner', entry => { entry.approval.author_approved_at = '2026-10-05'; }, '.approval'],
  ['a draft with an owner date', entry => { entry.approval.status = 'DRAFT'; }, '.approval.owner_approved_at'],
  ['an approval date that does not exist', entry => { entry.approval.owner_approved_at = '2026-13-01'; }, '.approval.owner_approved_at'],
  ['no approval block', entry => { delete entry.approval; }, '.approval'],
  ['a hash one character too long', entry => { entry.recipe_sha256 = A + 'a'; }, '.recipe_sha256'],
  ['a hash with a prefix', entry => { entry.recipe_sha256 = 'x' + A; }, '.recipe_sha256'],
  ['an external link that ends in a known page', entry => { entry.links = [{ label: 'а', href: 'https://evil.example/league' }]; }, '.links[0].href'],
  ['a link with a space in the fragment', entry => { entry.links = [{ label: 'а', href: '/league#a b' }]; }, '.links[0].href'],
  ['a link with an over-long query', entry => { entry.links = [{ label: 'а', href: '/league?' + 'a'.repeat(201) }]; }, '.links[0].href'],
  ['a link with an over-long fragment', entry => { entry.links = [{ label: 'а', href: '/league#' + 'a'.repeat(61) }]; }, '.links[0].href'],
  ['a link with an empty query', entry => { entry.links = [{ label: 'а', href: '/league?' }]; }, '.links[0].href'],
  ['a link to an unknown page', entry => { entry.links = [{ label: 'а', href: '/admin' }]; }, '.links[0].href'],
  ['a link with a capital letter in the path', entry => { entry.links = [{ label: 'а', href: '/League' }]; }, '.links[0].href'],
  ['a link that is not a text', entry => { entry.links = [{ label: 'а', href: 5 }]; }, '.links[0].href'],
  ['a link entry that is not an object', entry => { entry.links = ['/league']; }, '.links[0]'],
  ['links that are not a list', entry => { entry.links = '/league'; }, '.links'],
  ['an over-long link label', entry => { entry.links = [{ label: 'я'.repeat(61), href: '/league' }]; }, '.links[0].label'],
  ['an author note that is not an object', entry => { entry.author_note = 'x'; }, '.author_note'],
  ['a name of 61 characters made of two-unit symbols', entry => { entry.name = String.fromCodePoint(0x1f600).repeat(61); }, '.name'],
  ['a null where text belongs', entry => { entry.idea = null; }, '.idea'],
  ['a null approval date on an approved passport', entry => { entry.approval.author_approved_at = null; }, '.approval'],
  ['an approval date written with a slash', entry => { entry.approval.author_approved_at = '2026/10/03'; }, '.approval.author_approved_at'],
  ['a replaced hash in capitals', entry => { entry.replaces = B.toUpperCase(); }, '.replaces'],
  ['a replaced hash that is not a text', entry => { entry.replaces = 5; }, '.replaces'],
  ['a null where a list belongs', entry => { entry.exits = null; }, '.exits'],
  ['a null where an object belongs', entry => { entry.attempts = null; }, '.attempts'],
  ['a null Jev role', entry => { entry.jev.role = null; }, '.jev.role'],
  ['a prototype key as the Jev role', entry => { entry.jev.role = 'constructor'; }, '.jev.role'],
  ['a prototype key as the author kind', entry => { entry.author.kind = 'toString'; }, '.author.kind'],
  ['an upper-case www address', entry => { entry.idea = 'дивись WWW.EVIL.EXAMPLE'; }, '.idea'],
  ['an upper-case e-mail address', entry => { entry.idea = 'пиши A@EVIL.EXAMPLE'; }, '.idea'],
  ['a null link', entry => { entry.links = [null]; }, '.links[0]'],
  ['a link with an empty fragment', entry => { entry.links = [{ label: 'а', href: '/league#' }]; }, '.links[0].href'],
  ['links as a short text', entry => { entry.links = 'x'; }, '.links'],
  ['links as an object', entry => { entry.links = {}; }, '.links'],
];
for (const [label, change, where] of BAD) {
  test('rejected: ' + label, () => {
    const errors = problems(change);
    assert.ok(errors.length > 0, 'something must be reported');
    assert.ok(errors.some(error => error.path.includes(where)), 'reported at ' + where + ', got ' + JSON.stringify(errors.map(error => error.path)));
  });
}

test('every invisible range is rejected at both ends, and its neighbours are ordinary text', () => {
  const rejected = [0x00, 0x09, 0x0a, 0x0d, 0x1f, 0x7f, 0x80, 0x9f, 0xad, 0x61c, 0x180e, 0x200b, 0x200d, 0x200f, 0x2028, 0x2029, 0x202a, 0x202e, 0x2060, 0x2064, 0x2066, 0x2069, 0x206f, 0xfeff];
  const accepted = [0x20, 0xa0, 0xac, 0xae, 0x61b, 0x61d, 0x180d, 0x180f, 0x200a, 0x2010, 0x2027, 0x202f, 0x205f, 0x2070, 0xfefe, 0xff00, 0x2212, 0x2264, 0x1f600];
  for (const code of rejected) {
    const found = problems(entry => { entry.idea = 'а' + String.fromCodePoint(code) + 'б'; });
    assert.ok(found.some(error => error.path === 'passport.idea'), '0x' + code.toString(16) + ' must be rejected');
  }
  for (const code of accepted) assert.deepEqual(problems(entry => { entry.idea = 'а' + String.fromCodePoint(code) + 'б'; }), [], '0x' + code.toString(16) + ' is ordinary text');
});

test('a key-like token needs 32 characters, and the link parts have inclusive limits', () => {
  assert.deepEqual(problems(entry => { entry.idea = 'ключ ' + 'A1'.repeat(15) + 'A'; }), [], '31 characters are an ordinary word');
  assert.ok(problems(entry => { entry.idea = 'ключ ' + 'A1'.repeat(16); }).length > 0, '32 characters are not');
  assert.ok(problems(entry => { entry.idea = 'ключ ' + 'a-'.repeat(16); }).length > 0, 'dashes and underscores count');
  assert.ok(problems(entry => { entry.idea = 'ключ ' + 'a_'.repeat(16); }).length > 0);
  assert.deepEqual(problems(entry => { entry.links = [{ label: 'а', href: '/league?' + 'a'.repeat(200) + '#' + 'b'.repeat(60) }]; }), []);
  for (const page of ['arena', 'league', 'review', 'workshop']) assert.deepEqual(problems(entry => { entry.links = [{ label: 'а', href: '/' + page }]; }), [], page);
  assert.deepEqual(problems(entry => { entry.links = [{ label: 'а', href: '/arena?a=1&b=2:3%20x.y_z-w' }]; }), []);
  assert.deepEqual(problems(entry => { entry.name = String.fromCodePoint(0x1f600).repeat(60); }), [], 'length counts symbols, not code units');
});

test('options of any kind never throw and never open a draft', () => {
  const doc = file([draft()]);
  for (const odd of [null, 5, 'preview', [], [true], () => true, true, false, NaN]) {
    assert.equal(Passport.byRecipe(doc, A, odd), null, String(odd));
    assert.equal(Passport.render(fakeDocument, draft(), odd), null, String(odd));
    assert.equal(Passport.view(fakeDocument, doc, A, odd).className, 'passport-missing', String(odd));
    assert.equal(Passport.render(fakeDocument, base(), odd).attrs['data-state'], 'approved', 'an approved card ignores odd options');
  }
});

test('an entry that is not an object is one problem', () => {
  for (const value of [null, 5, 'x', [], undefined]) assert.equal(Passport.check(value).length, 1);
});

test('withheld thresholds forbid digits in the rule texts but not in evidence or notes', () => {
  assert.deepEqual(Passport.check(base({ numbers: 'withheld', waits_for: ['Чекає різкого падіння.'], exits: ['Стоп і ціль.'], risk: ['Мінімальний розмір.'],
    weak_regimes: ['У падінні.'], tagline: 'Коротко.', idea: 'Ідея.', jev: { role: 'none', text: 'Без Jev.' } })), []);
  const withheld = change => { const entry = base({ numbers: 'withheld', waits_for: ['а'], exits: ['а'], risk: ['а'], weak_regimes: ['а'], tagline: 'а', idea: 'а', jev: { role: 'none', text: 'а' } }); change(entry); return Passport.check(entry); };
  for (const field of ['tagline', 'idea']) assert.ok(withheld(entry => { entry[field] = 'поріг 35'; }).some(error => error.path === 'passport.' + field), field);
  for (const field of ['waits_for', 'when_silent', 'exits', 'risk', 'weak_regimes']) assert.ok(withheld(entry => { entry[field] = ['а', 'поріг 35']; }).some(error => error.path === 'passport.' + field + '[1]'), field);
  assert.ok(withheld(entry => { entry.jev.text = 'поріг 35'; }).some(error => error.path === 'passport.jev.text'));
  assert.ok(withheld(entry => { entry.risk = ['поріг 0']; }).some(error => error.path === 'passport.risk[0]'), 'a lone zero is a digit');
  assert.ok(withheld(entry => { entry.idea = 'поріг 9'; }).some(error => error.path === 'passport.idea'), 'and so is a nine');
  assert.deepEqual(withheld(entry => { entry.evidence = ['Дало 35 угод.']; entry.attempts.note = 'Перебрано 930 варіантів.'; entry.author_note.text = 'Очікую 0 %.'; }), []);
  assert.deepEqual(Passport.check(base({ numbers: 'shown', tagline: 'Поріг 35.' })), []);
});

test('the whole file: schema, duplicates, size and counts', () => {
  const sound = Passport.validate(file([base(), draft({ recipe_sha256: B, replaces: undefined })]));
  assert.deepEqual(sound.errors, []);
  assert.deepEqual([sound.ok, sound.approved, sound.drafts], [true, 1, 1]);
  const withPaths = Passport.validate(file([base(), draft({ recipe_sha256: B, tagline: '' })]));
  assert.equal(withPaths.ok, false);
  assert.ok(withPaths.errors.some(error => error.path === 'passports[1].tagline'));
  assert.deepEqual([withPaths.approved, withPaths.drafts], [1, 0], 'a broken entry is counted as neither');
  assert.ok(Passport.validate(file([base(), base()])).errors.some(error => error.path === 'passports[1].recipe_sha256' && /duplicate/.test(error.message)));
  assert.equal(Passport.validate({ ...file([]), schema: 'arena-passports/v2' }).ok, false);
  assert.equal(Passport.validate({ ...file([]), extra: 1 }).ok, false);
  assert.equal(Passport.validate({ ...file([]), updated: '2026-02-31' }).ok, false);
  assert.equal(Passport.validate({ schema: 'arena-passports/v1', passports: [] }).ok, true, 'the date of the file is optional');
  assert.equal(Passport.validate({ schema: 'arena-passports/v1' }).ok, false);
  assert.equal(Passport.validate(file(Array(65).fill(null))).ok, false);
  const many = count => file(Array.from({ length: count }, (_, index) => base({ recipe_sha256: index.toString(16).padStart(64, '0'), replaces: undefined })));
  assert.deepEqual(Passport.validate(many(64)).errors, [], 'sixty-four passports fit');
  assert.equal(Passport.validate(many(65)).ok, false, 'a sixty-fifth does not');
  for (const value of [null, 5, 'x', [], undefined]) assert.equal(Passport.validate(value).ok, false);
});

test('lookup is by exact hash and shows only approved cards unless a preview is asked for', () => {
  const doc = file([base(), draft({ recipe_sha256: B, replaces: undefined })]);
  assert.equal(Passport.byRecipe(doc, A).recipe_sha256, A);
  assert.equal(Passport.byRecipe(doc, B), null, 'a draft is not public');
  assert.equal(Passport.byRecipe(doc, B, { preview: true }).recipe_sha256, B);
  for (const loose of [1, 'true', {}, undefined, null]) assert.equal(Passport.byRecipe(doc, B, { preview: loose }), null, 'only preview: true opens a draft');
  for (const sha of [C, A.toUpperCase(), A.slice(1), A + 'a', '', null, undefined, 5, {}, [A]]) assert.equal(Passport.byRecipe(doc, sha), null);
  assert.equal(Passport.byRecipe({ ...doc, schema: 'x' }, A), null);
  assert.equal(Passport.byRecipe({ schema: 'arena-passports/v1', passports: 'x' }, A), null);
  assert.equal(Passport.byRecipe(null, A), null);
  assert.equal(Passport.byRecipe(file([base(), base()]), A), null, 'two entries for one hash are ambiguous');
  const mixed = file([null, base({ prompt: 'x' }), base({ recipe_sha256: C, replaces: undefined })]);
  assert.equal(Passport.byRecipe(mixed, A), null, 'a broken entry is not shown');
  assert.equal(Passport.byRecipe(mixed, C).recipe_sha256, C, 'and it does not hide the others');
  assert.equal(Passport.byRecipe(mixed, A, { preview: true }), null, 'a preview does not excuse a broken entry');
});

test('a passport is compared with the catalogue strategy it claims to describe', () => {
  const entry = base();
  const strategy = { recipe_sha256: A, name: entry.name, version: 2, author: { name: entry.author.name, kind: 'model', model: 'test-model-1', attempts: 930 } };
  assert.deepEqual(Passport.mismatches(entry, strategy), []);
  assert.deepEqual(Passport.mismatches(entry, { recipe_sha256: A, name: entry.name, author: strategy.author }), [], 'a catalogue without a version is not a mismatch');
  assert.deepEqual(Passport.mismatches(entry, { ...strategy, author: { name: entry.author.name, kind: 'model', model: 'test-model-1' } }), [], 'nor one without attempts');
  const differences = [
    ['recipe_sha256', { ...strategy, recipe_sha256: B }], ['name', { ...strategy, name: 'Інша' }], ['author.name', { ...strategy, author: { ...strategy.author, name: 'Хтось' } }],
    ['author.kind', { ...strategy, author: { ...strategy.author, kind: 'human' } }], ['author.model', { ...strategy, author: { ...strategy.author, model: 'other' } }],
    ['version', { ...strategy, version: 3 }], ['attempts.count', { ...strategy, author: { ...strategy.author, attempts: 931 } }]];
  for (const [field, other] of differences) {
    const found = Passport.mismatches(entry, other);
    assert.equal(found.length, 1, field);
    assert.match(found[0], new RegExp('^' + field.replace('.', '\\.') + ':'));
  }
  assert.deepEqual(Passport.mismatches({ ...entry, author: { name: 'Людина', kind: 'human', model: '' } }, { ...strategy, author: { name: 'Людина', kind: 'human', model: 'ігнорується' } }).filter(line => /^author\.model/.test(line)), [],
    'the model is compared only for a model author');
  assert.deepEqual(Passport.mismatches(entry, { recipe_sha256: A, name: entry.name }).map(line => line.split(':')[0]), ['author.name', 'author.kind', 'author.model'], 'a strategy without an author is a mismatch, not a crash');
  for (const pair of [[null, strategy], [entry, null], ['x', strategy], [entry, []]]) assert.deepEqual(Passport.mismatches(...pair), ['nothing to compare']);
});

const headings = card => walk(card).filter(node => /^h\d$/.test(node.tag));
const sections = card => walk(card).filter(node => node.tag === 'section');

test('the card lays out every part in a fixed order, with the right heading levels', () => {
  const card = Passport.render(fakeDocument, base());
  assert.equal(card.tag, 'article');
  assert.equal(card.className, 'passport');
  assert.equal(card.attrs['data-state'], 'approved');
  assert.equal(card.attrs['aria-label'], 'Паспорт стратегії: Тестова стратегія');
  assert.deepEqual(sections(card).map(node => text(node.children[0])), ['Ідея', 'Чого чекає', 'Коли не торгує', 'Як виходить', 'Ризик', 'Де слабка', 'Роль Jev',
    'Скільки варіантів перебрано', 'Автор сам оцінює', 'Що вже відомо про результати · станом на 3 жовтня 2026']);
  assert.deepEqual(headings(card).map(node => node.tag), ['h3', ...Array(10).fill('h4')]);
  assert.deepEqual(sections(card).map(node => node.className), ['idea', 'waits', 'silent', 'exits', 'risk', 'weak', 'jev', 'attempts', 'author', 'evidence'].map(part => 'passport-section passport-part-' + part), 'stable hooks for the stylesheet');
  assert.equal(text(headings(card)[0]), 'Тестова стратегія');
  const all = text(card);
  for (const part of ['Коротко про ідею.', 'Опис ідеї без обіцянок.', 'Чекає падіння на 0,35 %.', 'Майже завжди мовчить.', 'Стоп 1 %.', 'Один лот.', 'Слабка в падінні.',
    'Може лише відхилити вхід.', 'Перебрано варіанти на навчальних даних.', 'Очікую близько до Cash.', '— Автор Тест', 'Модель дала мінус на угоду.'])
    assert.ok(all.includes(part), part);
  assert.equal(card.children.at(-1).className, 'passport-disclaimer');
  assert.equal(text(card.children.at(-1)), Passport.DISCLAIMER);
  assert.match(Passport.DISCLAIMER, /не прогноз/);
  const quote = walk(card).find(node => node.tag === 'blockquote');
  assert.equal(text(quote), 'Очікую близько до Cash.');
  assert.deepEqual(card.children.map(node => node.tag), ['header', 'p', ...Array(10).fill('section'), 'p', 'ul', 'p']);
  assert.equal(card.children[1].className, 'passport-tagline');
});

test('heading levels follow the page, and anything out of range falls back to h3', () => {
  assert.deepEqual(headings(Passport.render(fakeDocument, base(), { headingLevel: 2 })).map(node => node.tag).slice(0, 2), ['h2', 'h3']);
  assert.deepEqual(headings(Passport.render(fakeDocument, base(), { headingLevel: 5 })).map(node => node.tag).slice(0, 2), ['h5', 'h6']);
  for (const level of [1, 6, 0, -1, 3.5, '4', null, undefined, NaN]) assert.equal(headings(Passport.render(fakeDocument, base(), { headingLevel: level }))[0].tag, 'h3', String(level));
});

test('the author line and the origin note say who named what, and that nobody checked it', () => {
  const lines = entry => { const card = Passport.render(fakeDocument, entry); return [text(walk(card).find(node => node.className === 'passport-author')), text(walk(card).find(node => node.className === 'passport-origin'))]; };
  assert.deepEqual(lines(base()), ['Автор: Автор Тест · ШІ-модель · test-model-1', 'Назву моделі вказав сам автор подання; платформа її не перевіряє.']);
  assert.deepEqual(lines(base({ author: { name: 'Людина', kind: 'human' } })), ['Автор: Людина · людина', 'Авторство вказав сам автор подання; платформа особу не перевіряє.']);
  assert.deepEqual(lines(base({ author: { name: 'Приклад', kind: 'example', model: '' } })), ['Автор: Приклад · навчальний приклад', 'Навчальний приклад показує формат і не є чиєюсь змагальною стратегією.']);
});

test('a draft is refused without a preview, and a preview always opens with its banner', () => {
  assert.equal(Passport.render(fakeDocument, draft()), null);
  for (const loose of [1, 'true', {}]) assert.equal(Passport.render(fakeDocument, draft(), { preview: loose }), null);
  const card = Passport.render(fakeDocument, draft(), { preview: true });
  assert.equal(card.attrs['data-state'], 'draft');
  assert.equal(card.children[0].className, 'passport-banner');
  assert.equal(text(card.children[0]), Passport.DRAFT);
  assert.match(Passport.DRAFT, /ЧЕРНЕТКА/);
  assert.equal(card.children.at(-1).className, 'passport-disclaimer');
  const approved = Passport.render(fakeDocument, base());
  assert.equal(walk(approved).some(node => node.className === 'passport-banner'), false);
  assert.equal(walk(Passport.render(fakeDocument, base(), { preview: true })).some(node => node.className === 'passport-banner'), false, 'an approved card needs no banner');
});

test('a card is never built from an unsound passport', () => {
  for (const bad of [base({ prompt: 'x' }), base({ links: [{ label: 'а', href: 'javascript:alert(1)' }] }), base({ recipe_sha256: 'x' }), null, 'x', [], undefined, 5])
    for (const preview of [false, true]) assert.equal(Passport.render(fakeDocument, bad, { preview }), null);
});

test('optional parts appear only when given', () => {
  const lean = base();
  delete lean.weak_regimes; delete lean.author_note; delete lean.evidence; delete lean.evidence_asof; delete lean.links; delete lean.replaces;
  const card = Passport.render(fakeDocument, lean);
  assert.deepEqual(sections(card).map(node => text(node.children[0])), ['Ідея', 'Чого чекає', 'Коли не торгує', 'Як виходить', 'Ризик', 'Роль Jev', 'Скільки варіантів перебрано']);
  assert.equal(walk(card).some(node => node.tag === 'blockquote' || node.tag === 'a'), false);
  assert.equal(text(walk(card).find(node => node.className === 'passport-fine')), 'Версія 2 · відбиток рецепта ' + A.slice(0, 12) + '…');
  const empty = Passport.render(fakeDocument, base({ weak_regimes: [], evidence: [], evidence_asof: undefined, links: [] }));
  assert.equal(sections(empty).length, 8, 'empty lists are the same as absent ones (author note included)');
});

test('the version line names the version, a short hash, and what it replaces', () => {
  const fine = text(walk(Passport.render(fakeDocument, base())).find(node => node.className === 'passport-fine'));
  assert.equal(fine, 'Версія 2 · відбиток рецепта ' + A.slice(0, 12) + '… · замінює версію з відбитком ' + B.slice(0, 12) + '…');
  assert.equal(fine.includes(A), false, 'only a short prefix of the hash is shown');
});

test('thresholds withheld by the author are announced on the card', () => {
  const note = entry => walk(Passport.render(fakeDocument, entry)).filter(node => node.className === 'passport-note').map(text);
  assert.deepEqual(note(base()), []);
  assert.deepEqual(note(base({ numbers: 'withheld', tagline: 'а', idea: 'а', waits_for: ['а'], when_silent: ['а'], exits: ['а'], risk: ['а'], weak_regimes: ['а'], jev: { role: 'none', text: 'а' } })), ['Числові пороги автор не розкриває.']);
});

test('the Jev role is shown as a fixed sentence before the author text', () => {
  const part = role => { const card = Passport.render(fakeDocument, base({ jev: { role, text: 'Пояснення автора.' } })); const section = sections(card).find(node => text(node.children[0]) === 'Роль Jev'); return section.children.slice(1).map(text); };
  assert.deepEqual(part('none'), ['Jev не бере участі: рішення ухвалюють лише правила.', 'Пояснення автора.']);
  assert.deepEqual(part('gate'), ['Jev може лише дозволити або заборонити вхід, який правила вже допустили.', 'Пояснення автора.']);
  assert.deepEqual(part('advisor'), ['Jev радить щодо входу, утримання й виходу (окремий режим із власною таблицею).', 'Пояснення автора.']);
});

test('the number of tried variants is written in Ukrainian with grouped thousands', () => {
  const said = count => { const card = Passport.render(fakeDocument, base({ attempts: { count, note: 'а' } })); return text(walk(card).find(node => node.className === 'passport-attempts')); };
  const expected = [[0, '0', 'варіантів'], [1, '1', 'варіант'], [2, '2', 'варіанти'], [4, '4', 'варіанти'], [5, '5', 'варіантів'], [11, '11', 'варіантів'], [12, '12', 'варіантів'],
    [14, '14', 'варіантів'], [21, '21', 'варіант'], [22, '22', 'варіанти'], [101, '101', 'варіант'], [111, '111', 'варіантів'], [930, '930', 'варіантів'],
    [1000, '1' + NBSP + '000', 'варіантів'], [1073, '1' + NBSP + '073', 'варіанти'], [1000000, '1' + NBSP + '000' + NBSP + '000', 'варіантів']];
  for (const [count, number, word] of expected) assert.equal(said(count), number + NBSP + word + '.', String(count));
  assert.equal(said(null), 'Кількість не зафіксовано.');
});

test('the date of the evidence is written out in Ukrainian', () => {
  const title = day => text(sections(Passport.render(fakeDocument, base({ evidence_asof: day }))).at(-1).children[0]);
  assert.equal(title('2026-01-05'), 'Що вже відомо про результати · станом на 5 січня 2026');
  assert.equal(title('2026-12-31'), 'Що вже відомо про результати · станом на 31 грудня 2026');
  assert.equal(title('2026-03-09'), 'Що вже відомо про результати · станом на 9 березня 2026');
});

test('links are the only elements with an href, and the href is exactly the validated one', () => {
  const card = Passport.render(fakeDocument, base({ links: [{ label: 'Ліга', href: '/league' }, { label: 'Розбір', href: '/review?mode=OKX_LIVE&id=ab-12#top' }] }));
  const anchors = walk(card).filter(node => node.tag === 'a');
  assert.deepEqual(anchors.map(node => [text(node), node.attrs.href, node.className]), [['Ліга', '/league', 'passport-link'], ['Розбір', '/review?mode=OKX_LIVE&id=ab-12#top', 'passport-link']]);
  assert.deepEqual(walk(card).filter(node => node.attrs.href !== undefined).length, 2);
  for (const node of walk(card).filter(node => node.tag !== 'a')) assert.deepEqual(Object.keys(node.attrs).filter(key => key !== 'data-state' && key !== 'aria-label'), [], node.tag + ' carries no extra attribute');
});

test('hostile text reaches the page only as text', () => {
  const hostile = '<img src=x onerror=alert(1)><script>alert(2)</script> & "quoted"';
  const entry = base({ name: 'Назва ' + hostile.slice(0, 40), tagline: hostile, idea: hostile, waits_for: [hostile], when_silent: [hostile], exits: [hostile], risk: [hostile], weak_regimes: [hostile],
    jev: { role: 'gate', text: hostile }, attempts: { count: 1, note: hostile }, author_note: { text: hostile, signed_by: hostile.slice(0, 40) }, evidence: [hostile],
    author: { name: hostile.slice(0, 50), kind: 'model', model: hostile.slice(0, 50) }, links: [{ label: hostile.slice(0, 50), href: '/league' }] });
  assert.deepEqual(Passport.check(entry), []);
  const card = Passport.render(fakeDocument, entry);
  assert.ok(card, 'angle brackets are plain text and are allowed');
  assert.deepEqual([...new Set(walk(card).map(node => node.tag))].sort(), ['a', 'article', 'blockquote', 'h3', 'h4', 'header', 'li', 'p', 'section', 'ul']);
  const direct = walk(card).filter(node => node._text.includes('<img'));
  assert.ok(direct.length > 5, 'the markup is carried inside text nodes');
  assert.ok(text(card).includes(hostile));
});

test('a page shows the approved card or a plain note that none is published', () => {
  const doc = file([base(), draft({ recipe_sha256: B, replaces: undefined })]);
  assert.equal(Passport.view(fakeDocument, doc, A).tag, 'article');
  for (const sha of [B, C, 'x', null]) {
    const note = Passport.view(fakeDocument, doc, sha);
    assert.equal(note.tag, 'p');
    assert.equal(note.className, 'passport-missing');
    assert.equal(text(note), Passport.MISSING);
  }
  assert.equal(Passport.view(fakeDocument, doc, B, { preview: true }).attrs['data-state'], 'draft');
  assert.equal(Passport.view(fakeDocument, null, A).className, 'passport-missing');
  assert.equal(text(Passport.renderMissing(fakeDocument)), Passport.MISSING);
  assert.match(Passport.MISSING, /не опубліковано/);
});

test('the module is pure: no markup sinks, network, storage, navigation or extra attributes', () => {
  const source = fs.readFileSync(path.join(__dirname, '../web/passport.js'), 'utf8');
  for (const sink of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/, /new Function/, /\bfetch\s*\(/, /XMLHttpRequest/, /localStorage/, /sessionStorage/,
    /\blocation\b/, /\bimport\s*\(/, /createElementNS/, /\.src\s*=/, /\.href\s*=/, /addEventListener/, /\.style\b/, /\.cssText/, /\bsetTimeout\b/, /\bsetInterval\b/])
    assert.equal(sink.test(source), false, String(sink));
  const attributes = [...source.matchAll(/setAttribute\(\s*"([^"]+)"/g)].map(match => match[1]).sort();
  assert.deepEqual(attributes, ['aria-label', 'data-state', 'href']);
  const created = new Set([...source.matchAll(/make\(doc, (?:"([a-z0-9]+)"|"h" \+ [^,]+)/g)].map(match => match[1] || 'h'));
  assert.deepEqual([...created].sort(), ['a', 'article', 'blockquote', 'h', 'header', 'li', 'p', 'section', 'ul']);
  const hidden = [...source].filter(char => { const code = char.codePointAt(0); return (code < 32 && code !== 9 && code !== 10 && code !== 13) || (code >= 0x7f && code <= 0x9f) || code === 0xa0 || code === 0xad || (code >= 0x200b && code <= 0x200f) || (code >= 0x2028 && code <= 0x202e) || (code >= 0x2060 && code <= 0x2064) || (code >= 0x2066 && code <= 0x2069) || code === 0xfeff; });
  assert.deepEqual(hidden, [], 'the source itself hides no invisible characters');
});
