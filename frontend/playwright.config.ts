import { defineConfig } from '@playwright/test';

// Local test traffic must not be sent to a machine-wide HTTP proxy.
process.env.NO_PROXY = [process.env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(',');

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 7_000 },
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:5173', browserName: 'chromium', viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce', trace: 'retain-on-failure' },
  webServer: { command: 'npm.cmd run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: !process.env.CI, timeout: 30_000 },
});
