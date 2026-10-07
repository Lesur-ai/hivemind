import { defineConfig } from '@playwright/test';
import config from './playwright.config.mjs';

// Ticket-only convenience; acceptance also runs in ordinary Admin E2E CI.
export default defineConfig({ ...config, testMatch: /admin_portal_634\.(spec|acceptance\.spec|empty\.spec|home\.spec)\.mjs$/ });
