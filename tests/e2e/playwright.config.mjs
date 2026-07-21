import { defineConfig } from '@playwright/test';

// Headless chromium, no web server (each spec intercepts every request via
// page.route and serves the real static bundle + controlled API from disk).
export default defineConfig({
    testDir: '.',
    testMatch: /.*\.spec\.mjs$/,
    fullyParallel: false,
    forbidOnly: !!process.env.CI,
    retries: 0,
    workers: 1,
    timeout: 30_000,
    expect: { timeout: 10_000 },
    reporter: 'line',
    use: {
        headless: true,
        actionTimeout: 10_000,
        // Chromium refuses to launch as root without --no-sandbox. CI runs the
        // spec inside the Playwright container as root, so it sets PW_NO_SANDBOX;
        // local runs keep the sandbox on.
        launchOptions: process.env.PW_NO_SANDBOX ? { args: ['--no-sandbox'] } : {},
    },
});
