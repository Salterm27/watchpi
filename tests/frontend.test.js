/**
 * Unit tests for the frontend's pure logic. Run with:  node --test tests/
 *
 * static/index.html is one file with the whole app in a <script> block, so
 * there's nothing to import. Instead we extract that block and evaluate it in
 * a vm context with just enough DOM shimmed for it to load, then assert
 * against the functions it defines.
 *
 * No browser, no dependencies, milliseconds. This covers the pure logic only —
 * rendering, event wiring and async orchestration need the Playwright suite.
 */

const { test, describe } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

/* The script ends with a boot IIFE that fetches and renders. We cut it off at
   its marker comment rather than trying to shim a whole browser for it — the
   assert below means this fails loudly if that marker ever moves. */
const BOOT_MARKER = '/* ============================================================ boot */';

function fakeEl() {
  return {
    textContent: '', innerHTML: '', value: '', hidden: false, disabled: false,
    style: {}, dataset: {}, children: [], firstElementChild: null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild() {}, prepend() {}, insertBefore() {}, remove() {},
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
    focus() {}, click() {},
    querySelector: () => fakeEl(), querySelectorAll: () => [],
  };
}

function loadApp() {
  const html = fs.readFileSync(
    path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
  const m = html.match(/<script>([\s\S]*)<\/script>/);
  assert.ok(m, 'could not find the <script> block in index.html');

  const cut = m[1].indexOf(BOOT_MARKER);
  assert.notEqual(cut, -1,
    'boot marker not found in index.html — update BOOT_MARKER in this harness');
  const source = m[1].slice(0, cut);

  const ctx = vm.createContext({
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    document: {
      getElementById: () => fakeEl(), createElement: () => fakeEl(),
      querySelector: () => fakeEl(), querySelectorAll: () => [],
      addEventListener() {}, body: fakeEl(),
    },
    fetch: () => Promise.reject(new Error('no network in unit tests')),
    setTimeout, clearTimeout, console, URL, Set, Map, Date, JSON, Math,
  });
  ctx.window = ctx;
  vm.runInContext(source, ctx);
  return ctx;
}

const app = loadApp();

/* `function` declarations land on the context, but top-level `let` bindings
   live in the script's lexical scope and are invisible from out here. Reading
   or setting that state has to happen inside the context. */
const run = expr => vm.runInContext(expr, app);

/* ------------------------------------------------ suggestions staleness
   The #17 regression. The bug was `!cache.items.length`, which treated a
   legitimately-empty batch as "no cache" and rebuilt (up to 25 requests) on
   every single visit to the Search tab. */
describe('suggestionsStale', () => {
  const friday = () => app.lastFriday();
  const older = new Date(friday().getTime() - 86400e3).toISOString();
  const newer = new Date(friday().getTime() + 3600e3).toISOString();

  test('an EMPTY but built batch is not stale (the #17 bug)', () => {
    assert.equal(
      app.suggestionsStale({ built_at: newer, seed_hash: 'h', items: [] }, 'h'),
      false);
  });

  test('a never-built cache is stale', () => {
    assert.equal(app.suggestionsStale({ items: [] }, 'h'), true);
    assert.equal(app.suggestionsStale({ built_at: null, seed_hash: 'h' }, 'h'), true);
  });

  test('same seeds are never stale, however old', () => {
    assert.equal(
      app.suggestionsStale({ built_at: older, seed_hash: 'h', items: [1] }, 'h'),
      false);
  });

  test('changed seeds rebuild only once past Friday', () => {
    assert.equal(
      app.suggestionsStale({ built_at: older, seed_hash: 'old', items: [1] }, 'new'),
      true, 'built before Friday with new seeds -> rebuild');
    assert.equal(
      app.suggestionsStale({ built_at: newer, seed_hash: 'old', items: [1] }, 'new'),
      false, 'already rebuilt since Friday -> wait for next week');
  });
});

/* ------------------------------------------------ caught-up / next episode */
describe('nextUpAired', () => {
  const show = (epCount, lastAired) => ({
    seasons: [{ season_number: 1, episode_count: epCount }],
    last_episode_to_air: lastAired
      ? { season_number: 1, episode_number: lastAired } : null,
  });
  const watched = pairs => new Set(pairs.map(([s, e]) => `${s}:${e}`));

  test('returns the next unwatched aired episode', () => {
    assert.deepEqual(app.nextUpAired(show(10, 5), watched([[1, 1], [1, 2]])),
                     { s: 1, e: 3 });
  });

  test('caught up on everything AIRED returns null', () => {
    // 10 episodes exist but only 6 have aired, and all 6 are watched
    const w = watched([[1, 1], [1, 2], [1, 3], [1, 4], [1, 5], [1, 6]]);
    assert.equal(app.nextUpAired(show(10, 6), w), null);
  });

  test('a newly aired episode brings it back', () => {
    const w = watched([[1, 1], [1, 2], [1, 3], [1, 4], [1, 5], [1, 6]]);
    assert.deepEqual(app.nextUpAired(show(10, 7), w), { s: 1, e: 7 });
  });

  test('nothing aired yet returns null', () => {
    assert.equal(app.nextUpAired(show(10, null), watched([])), null);
  });

  test('season 0 specials are ignored', () => {
    const d = {
      seasons: [{ season_number: 0, episode_count: 5 },
                { season_number: 1, episode_count: 3 }],
      last_episode_to_air: { season_number: 1, episode_number: 3 },
    };
    assert.deepEqual(app.nextUpAired(d, watched([])), { s: 1, e: 1 });
  });
});

/* ------------------------------------------------ library triage groups */
describe('groupOf', () => {
  const verdicts = new Map();

  test('movies and games group on their watched flag', () => {
    assert.equal(app.groupOf({ media_type: 'movie', watched: false }, verdicts), 'upnext');
    assert.equal(app.groupOf({ media_type: 'movie', watched: true }, verdicts), 'done');
    assert.equal(app.groupOf({ media_type: 'game', watched: true }, verdicts), 'done');
  });

  test('stopped beats everything', () => {
    assert.equal(app.groupOf({ media_type: 'movie', watched: false, stopped: true }, verdicts),
                 'stopped');
    assert.equal(app.groupOf({ media_type: 'tv', stopped: true }, verdicts), 'stopped');
  });

  test('a caught-up series is done, a mid-season one is up next', () => {
    const v = new Map([[1, null], [2, { s: 2, e: 4 }]]);
    assert.equal(app.groupOf({ id: 1, media_type: 'tv' }, v), 'done');
    assert.equal(app.groupOf({ id: 2, media_type: 'tv' }, v), 'upnext');
  });

  test('an unresolved series is up next, never hidden away', () => {
    assert.equal(app.groupOf({ id: 99, media_type: 'tv' }, new Map()), 'upnext');
  });

  test('partition keeps order and puts everything somewhere', () => {
    const items = [{ id: 1, media_type: 'movie', watched: false },
                   { id: 2, media_type: 'movie', watched: true },
                   { id: 3, media_type: 'tv', stopped: true }];
    const g = app.partition(items, new Map());
    assert.deepEqual(g.upnext.map(i => i.id), [1]);
    assert.deepEqual(g.done.map(i => i.id), [2]);
    assert.deepEqual(g.stopped.map(i => i.id), [3]);
  });
});

/* ------------------------------------------------ cinema flag */
describe('inCinemas', () => {
  test('only movies, only when in the set', () => {
    run('NOW_PLAYING = new Set([501])');
    assert.equal(run('inCinemas("movie", 501)'), true);
    assert.equal(run('inCinemas("movie", 999)'), false);
    assert.equal(run('inCinemas("tv", 501)'), false, 'series are never in cinemas');
    assert.equal(run('inCinemas("game", 501)'), false);
  });

  test('no data means no flags, not a crash', () => {
    run('NOW_PLAYING = null');
    assert.equal(run('inCinemas("movie", 501)'), false);
  });
});

/* ------------------------------------------------ escaping discipline
   The browser suite proves hostile input renders as text through the paths it
   exercises. This catches a missing esc() on a path no test covers yet, and
   is deliberately narrow — only data-bearing fields, so it stays free of
   false positives rather than needing an ever-growing allowlist. */
describe('HTML templates escape untrusted fields', () => {
  const html = fs.readFileSync(
    path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

  // fields that carry text from TMDB/RAWG, the API or the user
  const RISKY = /\b(title|name|text|query|overview|fallbackTitle)\b/;

  /**
   * Template literals that actually become DOM — the argument to el(), and
   * anything assigned to .innerHTML. Scanning only these is what keeps the
   * check precise: confirm()/toast() strings are plain text and a tag-shaped
   * regex over the whole file flags them (and any prose containing the word
   * "title") for nothing.
   */
  function domTemplates(src) {
    const out = [];
    const starts = [...src.matchAll(/(?:\bel\s*\(|\.innerHTML\s*=\s*)`/g)];
    for (const s of starts) {
      let i = s.index + s[0].length;      // first char inside the backtick
      const from = i;
      let depth = 0;                      // ${ } nesting, which may hold `…`
      for (; i < src.length; i++) {
        const c = src[i];
        if (c === '\\') { i++; continue; }
        if (c === '$' && src[i + 1] === '{') { depth++; i++; continue; }
        if (c === '}' && depth) { depth--; continue; }
        if (c === '`' && !depth) break;   // closing backtick of this template
      }
      out.push({ body: src.slice(from, i), offset: from });
    }
    return out;
  }

  test('no unescaped interpolation of a data-bearing field', () => {
    const offenders = [];
    for (const tpl of domTemplates(html)) {
      for (const m of tpl.body.matchAll(/\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}/g)) {
        // test the code, not the prose: a constant like 'open a title and
        // add it' contains the word but carries no user data
        const expr = m[1];
        const code = expr.replace(/'[^']*'|"[^"]*"/g, "''");
        if (!RISKY.test(code)) continue;
        if (/\besc\s*\(/.test(code)) continue;             // escaped: fine
        if (/^\s*userQ\(\)\s*$/.test(code)) continue;      // a query string, not markup
        const line = html.slice(0, tpl.offset + m.index).split('\n').length;
        offenders.push(`index.html:${line}  \${${expr.trim()}}`);
      }
    }
    assert.deepEqual(offenders, [],
      'interpolate user/remote text into HTML only via esc():\n  '
      + offenders.join('\n  '));
  });

  test('the scan actually looks at the markup', () => {
    // a broken scanner that finds nothing would pass the test above silently
    const tpls = domTemplates(html);
    assert.ok(tpls.length > 25, `only found ${tpls.length} DOM templates`);
    assert.ok(tpls.some(t => t.body.includes('class="card')), 'missed the card markup');
  });

  test('it would catch a missing esc()', () => {
    const bad = 'el(`<div class="title">${item.title}</div>`)';
    const found = domTemplates(bad)
      .flatMap(t => [...t.body.matchAll(/\$\{([^{}]+)\}/g)])
      .filter(m => RISKY.test(m[1]) && !/\besc\s*\(/.test(m[1]));
    assert.equal(found.length, 1, 'the scanner must flag an unescaped title');
  });

  test('esc() actually neutralises the dangerous characters', () => {
    const out = app.esc('<img src=x onerror="a">&\'');
    for (const ch of ['<', '>', '"']) assert.ok(!out.includes(ch), `esc left a raw ${ch}`);
  });
});

/* ------------------------------------------------ misc helpers */
describe('helpers', () => {
  test('seedHashOf is stable for the same seeds and changes with them', () => {
    const a = { shows: [{ media_type: 'tv', tmdb_id: 1 }], games: [] };
    const b = { shows: [{ media_type: 'tv', tmdb_id: 1 }], games: [] };
    const c = { shows: [{ media_type: 'tv', tmdb_id: 2 }], games: [] };
    assert.equal(app.seedHashOf(a), app.seedHashOf(b));
    assert.notEqual(app.seedHashOf(a), app.seedHashOf(c));
  });

  test('lastFriday is a Friday, in the past', () => {
    const f = app.lastFriday();
    assert.equal(f.getDay(), 5);
    assert.ok(f.getTime() <= Date.now());
  });

  test('esc neutralises markup', () => {
    assert.ok(!app.esc('<img src=x onerror=alert(1)>').includes('<img'));
    assert.equal(app.esc('Tom & Jerry').includes('&'), true);
  });

  test('year pulls the year, and tolerates nothing', () => {
    assert.equal(app.year('2024-05-01'), '2024');
    assert.equal(app.year(''), '');
    assert.equal(app.year(undefined), '');
  });
});
