import fs from 'node:fs';
import { expect } from '@playwright/test';
import { test, routePortal, configureViewport, evidence, VIEWPORTS, ORIGIN } from './fixtures/portal-634.mjs';

// Runs only through the explicit local native config. This verifies the proof
// mechanism; the other native cases qualify the exercised Portal journeys.
test('634 native zoom setup records real Chrome preferences and pixel dimensions', async ({ page }, info) => {
    await configureViewport(page, VIEWPORTS[3]);
    const state = await routePortal(page);
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await expect(page.locator('#dashRunningJobs')).not.toBeEmpty();
    await evidence(page, state, info, 'native-zoom-setup', VIEWPORTS[3]);
    const packet = JSON.parse(fs.readFileSync(info.outputPath('native-zoom-setup.json'), 'utf8'));
    expect(packet.zoomMethod).toBe('Chrome Settings native 200%');
    expect(packet.provenance).toEqual({ mode: 'qualification', status: 'git-verified' });
    expect(packet.sourceHead).toMatch(/^[0-9a-f]{40}$/);
    expect(packet.dirty).toBe('');
    expect(packet.nativeZoom.headless).toBe(true);
    expect(packet.nativeZoom.settingsValue).toBe('2');
    expect(packet.nativeZoom.browserVersion).toMatch(/^\d+\./);
    expect(packet.measurements).toMatchObject({ width: 720, height: 450, dpr: 2 });
});
