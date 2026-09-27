/**
 * Shared setup for the browser tests.
 *
 * All tests share one app server, so isolation comes from the per-profile
 * library: each test makes its own profile and only ever asserts on its own
 * titles. Global config (TMDB key, region) is set once per test since it's
 * Pi-wide.
 *
 * TMDB and RAWG are always mocked — no keys, no network, no flake.
 */

/** Create a profile and return its id. Unique name per test keeps them apart. */
async function newProfile(request, name) {
  const r = await request.post('/api/users', { data: { name } });
  return (await r.json()).id;
}

async function setConfig(request, cfg = {}) {
  await request.put('/api/config', {
    data: { tmdb_key: 'test-key', region: 'US', ...cfg },
  });
}

async function addTitle(request, uid, { tmdb_id, media_type, title }) {
  const r = await request.post(`/api/library?user=${uid}`,
    { data: { tmdb_id, media_type, title } });
  return (await r.json()).id;
}

async function markEpisode(request, uid, itemId, season, episode) {
  await request.put(`/api/library/${itemId}/episodes?user=${uid}`,
    { data: { episodes: [{ season, episode }], watched: true } });
}

/**
 * Install TMDB/RAWG mocks. `opts`:
 *   nowPlaying   {region: [tmdbId,…]}   /movie/now_playing
 *   search       [result,…]             /search/multi
 *   games        [result,…]             RAWG /games
 *   rawgDelayMs  number                 make RAWG slow, to prove the UI
 *                                       doesn't block on it
 *   onTmdb       fn(pathname)           request spy
 */
async function mockApis(page, opts = {}) {
  const {
    nowPlaying = {}, search = [], games = [], rawgDelayMs = 0, onTmdb = () => {},
  } = opts;
  const json = (route, body) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

  await page.route('https://api.themoviedb.org/**', route => {
    const u = new URL(route.request().url());
    onTmdb(u.pathname, u);
    if (u.pathname.includes('/movie/now_playing')) {
      const ids = nowPlaying[u.searchParams.get('region')] || [];
      return json(route, { page: 1, total_pages: 1, results: ids.map(id => ({ id })) });
    }
    if (u.pathname.includes('/search/multi')) return json(route, { results: search });
    if (u.pathname.includes('/recommendations')) return json(route, { results: [] });
    if (/\/tv\/\d+$/.test(u.pathname)) {
      return json(route, {
        id: Number(u.pathname.split('/').pop()), name: 'Show', number_of_episodes: 10,
        seasons: [{ season_number: 1, episode_count: 10 }],
        last_episode_to_air: { season_number: 1, episode_number: 10 },
        episode_run_time: [45], 'watch/providers': { results: {} },
      });
    }
    return json(route, {
      id: Number(u.pathname.split('/').pop()) || 1, title: 'Film', runtime: 120,
      'watch/providers': { results: {} },
    });
  });

  await page.route('https://api.rawg.io/**', async route => {
    if (rawgDelayMs) await new Promise(r => setTimeout(r, rawgDelayMs));
    json(route, { results: games });
  });
}

/**
 * Boot the app as a given profile, skipping the "Who's watching?" picker.
 *
 * Readiness is detected from the DOM, not from app state: the app's top-level
 * `let` declarations are lexical bindings, so `window.USER` is always
 * undefined no matter how far boot has got.
 */
async function openAs(page, uid) {
  // runs on every navigation, so it must not clobber state the app persisted
  // (a reload has to keep things like the remembered group-collapse choice).
  // Each test gets a fresh context, so there's nothing to clear anyway.
  await page.addInitScript(id => {
    if (!localStorage.getItem('watchpi_user'))
      localStorage.setItem('watchpi_user', String(id));
  }, uid);
  await page.goto('/');
  await page.waitForFunction(() => {
    const m = document.getElementById('main');
    return m && m.children.length > 0 && !m.querySelector('.spin');
  });
}

/** Titles currently rendered in the main pane, with whether they're flagged. */
async function cards(page) {
  return page.evaluate(() => [...document.querySelectorAll('#main .card')].map(c => ({
    title: c.querySelector('.title')?.textContent ?? '',
    cinema: !!c.querySelector('.cinema'),
  })));
}

module.exports = {
  newProfile, setConfig, addTitle, markEpisode, mockApis, openAs, cards,
};
