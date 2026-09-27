// Browser tests for WatchPi. These cover what unit tests structurally can't:
// rendering, event wiring and async orchestration.
//
// A real app server is booted against a throwaway DB. TMDB and RAWG are always
// mocked with page.route, so these need no API keys and never touch the network.
const { defineConfig } = require('@playwright/test');

const PORT = process.env.WATCHPI_TEST_PORT || 8099;

module.exports = defineConfig({
  testDir: './tests/browser',
  // the app is shared, so tests must not depend on each other's ordering
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['github']] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    // CI runs `playwright install chromium` and uses the matching build.
    // Set PW_CHROMIUM to reuse a browser that's already on the machine
    // instead (handy when the local build doesn't match this pinned version).
    launchOptions: process.env.PW_CHROMIUM
      ? { executablePath: process.env.PW_CHROMIUM }
      : {},
  },
  webServer: {
    command: `python3 app.py`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    env: {
      WATCHPI_PORT: String(PORT),
      WATCHPI_DB: './.pw-tmp/watchpi.db',
    },
  },
});
