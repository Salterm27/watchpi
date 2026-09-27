/**
 * Regression cover for the other half of the slow-search incident (#17).
 *
 * doSearch used to `await Promise.allSettled([tmdb, rawg])`, so nothing
 * rendered until BOTH sources answered. Movies/Series skipped the RAWG call
 * and stayed fast; "All" and "Games" waited on it — which is exactly the
 * asymmetry that was reported.
 *
 * This is async orchestration, not a pure function: no unit test can catch it.
 */

const { test, expect } = require('@playwright/test');
const { newProfile, setConfig, mockApis, openAs } = require('./helpers');

const MOVIE = {
  id: 601, title: 'Fast Movie', media_type: 'movie',
  poster_path: null, release_date: '2024-01-01',
};
const GAME = { id: 701, name: 'Slow Game', background_image: null, released: '2024-01-01' };

const RAWG_DELAY = 1500;

test('TMDB results paint while a slow RAWG is still in flight', async ({ page, request }) => {
  const uid = await newProfile(request, `search-${Date.now()}`);
  await setConfig(request, { rawg_key: 'test-rawg' });
  await mockApis(page, { search: [MOVIE], games: [GAME], rawgDelayMs: RAWG_DELAY });
  await openAs(page, uid);

  await page.click('#tab-search');
  await page.fill('#q', 'fast');

  const started = Date.now();
  // the movie must be on screen well before RAWG could possibly have answered
  await expect(page.locator('#results .card', { hasText: 'Fast Movie' }))
    .toBeVisible({ timeout: RAWG_DELAY - 500 });
  const painted = Date.now() - started;
  expect(painted, `movies painted after ${painted}ms; RAWG takes ${RAWG_DELAY}ms`)
    .toBeLessThan(RAWG_DELAY);

  // and it says games are still coming rather than silently omitting them
  await expect(page.locator('#results')).toContainText(/Loading games/i);

  // then the game arrives and joins the results
  await expect(page.locator('#results .card', { hasText: 'Slow Game' }))
    .toBeVisible({ timeout: RAWG_DELAY + 5000 });
});

test('a failing RAWG leaves movies rendered and says games are unavailable',
  async ({ page, request }) => {
    const uid = await newProfile(request, `searchfail-${Date.now()}`);
    await setConfig(request, { rawg_key: 'test-rawg' });
    await mockApis(page, { search: [MOVIE] });
    await page.route('https://api.rawg.io/**', r => r.abort());
    await openAs(page, uid);

    await page.click('#tab-search');
    await page.fill('#q', 'fast');

    await expect(page.locator('#results .card', { hasText: 'Fast Movie' })).toBeVisible();
    await expect(page.locator('#results')).toContainText(/Games unavailable/i);
  });

test('a slow earlier query cannot overwrite a newer one', async ({ page, request }) => {
  const uid = await newProfile(request, `race-${Date.now()}`);
  await setConfig(request);
  // the first query is deliberately slow, the second fast
  await page.route('https://api.themoviedb.org/**', async route => {
    const u = new URL(route.request().url());
    if (u.pathname.includes('/search/multi')) {
      const q = u.searchParams.get('query');
      const slow = q === 'slowquery';
      if (slow) await new Promise(r => setTimeout(r, 1500));
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ results: [{
          id: slow ? 1 : 2, title: slow ? 'STALE result' : 'FRESH result',
          media_type: 'movie', poster_path: null, release_date: '2024-01-01' }] }),
      });
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{"results":[]}' });
  });
  await openAs(page, uid);

  await page.click('#tab-search');
  await page.fill('#q', 'slowquery');
  await page.waitForTimeout(500);          // let the slow request start
  await page.fill('#q', 'fastquery');      // supersede it

  await expect(page.locator('#results .card', { hasText: 'FRESH result' })).toBeVisible();
  await page.waitForTimeout(1800);         // long enough for the stale one to land
  await expect(page.locator('#results')).not.toContainText('STALE result');
});
