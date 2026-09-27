/**
 * The library's triage grouping and the quick-capture inbox — both are
 * render-and-wiring behaviour that unit tests can't reach.
 */

const { test, expect } = require('@playwright/test');
const { newProfile, setConfig, addTitle, markEpisode, mockApis, openAs } = require('./helpers');

/** read the groups by walking the rendered sections in order */
async function groups(page) {
  return page.evaluate(() => {
    const area = document.getElementById('main').lastElementChild;
    const out = { upnext: [], done: [], stopped: [] };
    let cur = 'upnext';
    for (const ch of area.children) {
      if (ch.tagName === 'H3') cur = 'upnext';
      else if (ch.classList.contains('grp-head'))
        cur = ch.textContent.includes('Watched') ? 'done' : 'stopped';
      else if (ch.classList.contains('grid'))
        for (const t of ch.querySelectorAll('.title')) out[cur].push(t.textContent);
    }
    return out;
  });
}

test('sorts titles into up next / watched it all / stopped', async ({ page, request }) => {
  const uid = await newProfile(request, `groups-${Date.now()}`);
  await setConfig(request);
  const midBinge = await addTitle(request, uid,
    { tmdb_id: 7001, media_type: 'tv', title: 'Mid Binge' });
  await markEpisode(request, uid, midBinge, 1, 1);
  await addTitle(request, uid, { tmdb_id: 7002, media_type: 'movie', title: 'Unseen Film' });
  const seen = await addTitle(request, uid,
    { tmdb_id: 7003, media_type: 'movie', title: 'Seen Film' });
  await request.patch(`/api/library/${seen}?user=${uid}`, { data: { watched: true } });
  const dropped = await addTitle(request, uid,
    { tmdb_id: 7004, media_type: 'tv', title: 'Abandoned' });
  await request.patch(`/api/library/${dropped}?user=${uid}`, { data: { stopped: true } });

  await mockApis(page);
  await openAs(page, uid);
  await page.waitForSelector('#main .card');
  await page.waitForTimeout(600);           // let the caught-up pass settle

  const g = await groups(page);
  expect(g.upnext.sort()).toEqual(['Mid Binge', 'Unseen Film']);
  expect(g.done).toContain('Seen Film');
  expect(g.stopped).toEqual(['Abandoned']);
});

test('tail groups start collapsed and remember being opened', async ({ page, request }) => {
  const uid = await newProfile(request, `collapse-${Date.now()}`);
  await setConfig(request);
  await addTitle(request, uid, { tmdb_id: 7101, media_type: 'movie', title: 'To Watch' });
  const seen = await addTitle(request, uid,
    { tmdb_id: 7102, media_type: 'movie', title: 'Finished' });
  await request.patch(`/api/library/${seen}?user=${uid}`, { data: { watched: true } });

  await mockApis(page);
  await openAs(page, uid);
  const head = page.locator('.grp-head', { hasText: 'Watched it all' });
  await expect(head).toBeVisible();
  await expect(head).not.toHaveClass(/open/);

  await head.click();
  await expect(head).toHaveClass(/open/);

  // the choice is remembered on this device
  await page.reload();
  await page.waitForSelector('.grp-head');
  await expect(page.locator('.grp-head', { hasText: 'Watched it all' })).toHaveClass(/open/);
});

test('an inbox capture surfaces, resolves and clears', async ({ page, request }) => {
  const uid = await newProfile(request, `inbox-${Date.now()}`);
  await setConfig(request);
  await request.post(`/api/inbox?user=${uid}`, { data: { text: 'Severance', source: 'test' } });

  await mockApis(page, {
    search: [{ id: 95396, name: 'Severance', media_type: 'tv',
               poster_path: null, first_air_date: '2022-02-18' }],
  });
  await openAs(page, uid);

  const banner = page.locator('.newband', { hasText: 'captured' });
  await expect(banner).toContainText('Severance');

  await banner.click();
  await page.waitForSelector('#inbox-list .grid .card');
  await page.locator('#inbox-list .card', { hasText: 'Severance' }).click();
  await page.waitForTimeout(800);

  // it's in the library and the capture is gone
  const lib = await (await request.get(`/api/library?user=${uid}`)).json();
  expect(lib.map(i => i.title)).toContain('Severance');
  expect(await (await request.get(`/api/inbox?user=${uid}`)).json()).toEqual([]);
});
