/**
 * Hostile input must render as text, never as markup.
 *
 * The audit found no XSS; these keep it that way by driving the three channels
 * an attacker can actually reach, in a real browser:
 *   - a library title  (POST /api/library takes any title)
 *   - a folder name    (user-typed)
 *   - an inbox capture (anyone who can message the bot — the most exposed)
 *
 * Each asserts the payload did NOT execute, no element was injected, and the
 * literal text is on screen — so a missing esc() fails here rather than
 * shipping.
 */

const { test, expect } = require('@playwright/test');
const { newProfile, setConfig, mockApis, openAs } = require('./helpers');

// if this ever runs, window.__xss is set and the assertions below catch it
const PAYLOAD = '<img src=x onerror="window.__xss=1">Pwned';
// folder names are capped at 40 chars by the API (real validation, not a bug),
// so that field needs a shorter payload that still executes if unescaped
const SHORT_PAYLOAD = '<img src=x onerror=window.__xss=1>Pwned';

/** true if the payload executed anywhere on the page */
const didExecute = page => page.evaluate(() => window.__xss === 1);
/** the payload's element, if the browser parsed it as markup rather than text */
const injected = page => page.locator('img[src="x"]').count();

test('a hostile library title renders as text', async ({ page, request }) => {
  const uid = await newProfile(request, `xss-lib-${Date.now()}`);
  await setConfig(request);
  await request.post(`/api/library?user=${uid}`,
    { data: { tmdb_id: 9001, media_type: 'movie', title: PAYLOAD } });

  await mockApis(page);
  await openAs(page, uid);
  await page.waitForSelector('#main .card');

  expect(await didExecute(page)).toBeFalsy();
  expect(await injected(page)).toBe(0);
  await expect(page.locator('#main .card .title')).toContainText('Pwned');
});

test('a hostile folder name renders as text', async ({ page, request }) => {
  const uid = await newProfile(request, `xss-folder-${Date.now()}`);
  await setConfig(request);
  await request.post(`/api/folders?user=${uid}`, { data: { name: SHORT_PAYLOAD, member_ids: [] } });

  await mockApis(page);
  await openAs(page, uid);
  await page.waitForSelector('.chips .chip');

  expect(await didExecute(page)).toBeFalsy();
  expect(await injected(page)).toBe(0);
  await expect(page.locator('.chips').first()).toContainText('Pwned');  // folder row, not the type row
});

test('a hostile inbox capture renders as text', async ({ page, request }) => {
  const uid = await newProfile(request, `xss-inbox-${Date.now()}`);
  await setConfig(request);
  await request.post(`/api/inbox?user=${uid}`, { data: { text: PAYLOAD, source: 'telegram' } });

  await mockApis(page, { search: [] });
  await openAs(page, uid);

  // the banner shows captured text …
  const banner = page.locator('.newband', { hasText: 'captured' });
  await expect(banner).toContainText('Pwned');
  expect(await didExecute(page)).toBeFalsy();
  expect(await injected(page)).toBe(0);

  // … and so does the sheet that resolves it
  await banner.click();
  await page.waitForSelector('#inbox-list');
  await expect(page.locator('#inbox-list')).toContainText('Pwned');
  expect(await didExecute(page)).toBeFalsy();
  expect(await injected(page)).toBe(0);
});

test('a hostile search result renders as text', async ({ page, request }) => {
  const uid = await newProfile(request, `xss-search-${Date.now()}`);
  await setConfig(request);
  // a compromised or mischievous upstream is a real source of hostile strings
  await mockApis(page, {
    search: [{ id: 9002, title: PAYLOAD, media_type: 'movie',
               poster_path: null, release_date: '2024-01-01' }],
  });
  await openAs(page, uid);

  await page.click('#tab-search');
  await page.fill('#q', 'anything');
  await page.waitForSelector('#results .card');

  expect(await didExecute(page)).toBeFalsy();
  expect(await injected(page)).toBe(0);
  await expect(page.locator('#results .card .title')).toContainText('Pwned');
});
