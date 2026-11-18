import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: process.env.CREATOR_RADAR_TEST_URL || 'http://127.0.0.1:8765',
    channel: process.env.CREATOR_RADAR_TEST_BROWSER,
  },
});
