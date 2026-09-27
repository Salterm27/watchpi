/**
 * The "in cinemas" flag: correct surfaces, correct exclusions, and — the part
 * that matters for a Raspberry Pi — exactly one now_playing request, not one
 * per card.
 */

const { test, expect } = require('@playwright/test');
const { newProfile, setConfig, addTitle, mockApis, openAs, cards } = require('./helpers');

const IN_US = 501, WATCHED_IN_US = 502, NOT_SHOWING = 503, SERIES = 600;

async function seedLibrary(request, uid) {
  await addTitle(request, uid, { tmdb_id: IN_US, media_type: 'movie', title: 'Showing Now' });
  const seen = await addTitle(request, uid,
    { tmdb_id: WATCHED_IN_US, media_type: 'movie', title: 'Already Seen' });
  await addTitle(request, uid, { tmdb_id: NOT_SHOWING, media_type: 'movie', title: 'Old Film' });
  await addTitle(request, uid, { tmdb_id: SERIES, media_type: 'tv', title: 'A Series' });
  await request.patch(`/api/library/${seen}?user=${uid}`, { data: { watched: true } });
}

test('flags only unwatched movies that are actually showing', async ({ page, request }) => {
  const uid = await newProfile(request, `cine-${Date.now()}`);
  await setConfig(request);
  await seedLibrary(request, uid);
  await mockApis(page, { nowPlaying: { US: [IN_US, WATCHED_IN_US] } });
  await openAs(page, uid);
  await page.waitForSelector('#main .card');

  const flagged = (await cards(page)).filter(c => c.cinema).map(c => c.title);
  expect(flagged).toEqual(['Showing Now']);   // not the watched one, not TV, not the old film
});

test('fetches now_playing once, not once per card', async ({ page, request }) => {
  const uid = await newProfile(request, `cinecache-${Date.now()}`);
  await setConfig(request);
  await seedLibrary(request, uid);

  let calls = 0;
  await mockApis(page, {
    nowPlaying: { US: [IN_US] },
    onTmdb: p => { if (p.includes('now_playing')) calls++; },
  });
  await openAs(page, uid);
  await page.waitForSelector('#main .card');
  expect(calls, 'one request for the whole library').toBe(1);

  // re-render: the cached set must be reused
  await page.click('#tab-search');
  await page.waitForTimeout(300);
  await page.click('#tab-library');
  await page.waitForSelector('#main .card');
  expect(calls, 'no refetch on re-render').toBe(1);
});

test('the detail sheet says so, and only for a film that is showing',
  async ({ page, request }) => {
    const uid = await newProfile(request, `cinedetail-${Date.now()}`);
    await setConfig(request);
    await seedLibrary(request, uid);
    await mockApis(page, { nowPlaying: { US: [IN_US] } });
    await openAs(page, uid);
    await page.waitForSelector('#main .card');

    await page.evaluate(id => openDetail('movie', id, 'Showing Now'), IN_US);
    await expect(page.locator('#sheet')).toContainText(/In cinemas now/i);

    await page.evaluate(() => closeSheet());
    await page.evaluate(id => openDetail('movie', id, 'Old Film'), NOT_SHOWING);
    await expect(page.locator('#sheet')).not.toContainText(/In cinemas now/i);
  });

test('changing region refetches and re-flags', async ({ page, request }) => {
  const uid = await newProfile(request, `cineregion-${Date.now()}`);
  await setConfig(request);
  await seedLibrary(request, uid);

  const regions = [];
  await mockApis(page, {
    nowPlaying: { US: [IN_US], AR: [NOT_SHOWING] },
    onTmdb: (p, u) => { if (p.includes('now_playing')) regions.push(u.searchParams.get('region')); },
  });
  await openAs(page, uid);
  await page.waitForSelector('#main .card');
  expect((await cards(page)).filter(c => c.cinema).map(c => c.title)).toEqual(['Showing Now']);

  await page.evaluate(() => openSettings());
  await page.fill('#set-region', 'AR');
  await page.click('#set-save');
  await page.waitForTimeout(800);
  await page.click('#tab-library');
  await page.waitForSelector('#main .card');

  expect(regions).toEqual(['US', 'AR']);
  expect((await cards(page)).filter(c => c.cinema).map(c => c.title)).toEqual(['Old Film']);
});
