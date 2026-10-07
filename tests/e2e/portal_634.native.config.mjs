import { defineConfig } from '@playwright/test';
import config from './playwright.config.mjs';

// Explicit local supplement. No skip or change to portable CI discovery.
export default defineConfig({
    ...config,
    testMatch: /admin_portal_634\.(acceptance\.spec|home\.spec|native)\.mjs$/,
    grep: /zoom-200|native zoom setup/,
    projects: [{ name: 'chrome-native-200', metadata: { nativeChromeZoom: 200 } }],
});
