// Final Dashboard contract (§5.2/§17.3), controlled data and real static bundle.
import { expect } from '@playwright/test';
import { test, homeInventory as inventory, tabTo, routePortal, configureViewport, evidence, VIEWPORTS, ORIGIN, HOSTILE } from './fixtures/portal-634.mjs';
const cards = page => page.locator('#dashRecentSpaces .dash-space-card');
const ids = page => cards(page).evaluateAll(elements => elements.map(el => el.dataset.space));
async function ready(page, state) {
    await page.goto(`${ORIGIN}/admin.html#/dashboard`);
    await expect(page.locator('#dashInventoryFreshness')).toContainText('Updated');
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(state.reads('system_health')).toHaveLength(0);
}
async function bounded(page, state, expected) {
    expect(await ids(page)).toEqual(expected);
    expect(state.reads('space_list').map(call => call.arguments)).toEqual([{ include_counts: false }]);
    expect(state.reads('bank_consolidation_queues').map(call => call.arguments.space_ids)).toEqual(expected.length ? [expected.join(',')] : []);
    for (const tool of ['live_read', 'space_info']) expect(state.reads(tool)).toHaveLength(0);
    await expect(page.locator('#dashPrevPage, #dashNextPage')).toHaveCount(0);
    for (const selector of ['.dash-notes', '.dash-diagnostics']) await expect(page.locator(selector)).toHaveJSProperty('open', false);
    await expect(page.locator('.dash-operations')).toHaveJSProperty('open', true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
}
for (const viewport of VIEWPORTS) for (const count of [0, 1, 20, 25]) {
    test(`#633 temporal inventory ${count}, responsive cards and scoped reads — ${viewport.name}`, async ({ page }, info) => {
        await configureViewport(page, viewport);
        const data = inventory(count), state = await routePortal(page, data); await ready(page, state);
        if (count > 1) {
            expect(data.expectedRecent).not.toEqual(data.spaces.map(s => s.space_id).sort());
            expect(data.expectedRecent).not.toEqual(data.spaces.toReversed().map(s => s.space_id));
        }
        const limit = viewport.name === 'desktop' ? 6 : viewport.name === 'tablet' ? 4 : viewport.name === 'mobile' ? 2 : 2;
        const expected = data.expectedRecent.slice(0, limit);
        await evidence(page, state, info, `home-inventory-${count}`, viewport); await bounded(page, state, expected);
        for (const id of expected) {
            const card = page.locator(`.dash-space-card[data-space="${id}"]`), source = data.spaces.find(s => s.space_id === id);
            await expect(card).toContainText('Lifetime totals');
            expect((await card.locator('.dash-space-card-heading [title]').getAttribute('title')).split(' · ')[0]).toBe(source.last_consolidation);
            await expect(card.locator('dd')).toHaveText([String(source.consolidation_count), String(source.total_notes_processed)]);
            await expect(card).toContainText(HOSTILE); await expect(card.locator('img')).toHaveCount(0);
        }
    });
}
test('#633 timezone order, ASCII ties, invalid dates and unavailable counters', async ({ page, context }, info) => {
    const viewport = VIEWPORTS[0]; await configureViewport(page, viewport);
    const spaces = [
        { space_id: 'tie-A', last_consolidation: '2026-09-30T14:00:00+02:00', consolidation_count: 2, total_notes_processed: 50 },
        { space_id: 'a-oldest', last_consolidation: '2026-09-30T10:00:00Z', consolidation_count: 1, total_notes_processed: 20 },
        { space_id: 'z-latest', last_consolidation: '2026-09-30T13:00:00Z', consolidation_count: Number.MAX_SAFE_INTEGER + 1 },
        { space_id: 'tie-z', last_consolidation: '2026-09-30T12:00:00Z', consolidation_count: -1, total_notes_processed: '60' },
        { space_id: 'never', last_consolidation: null, consolidation_count: 0, total_notes_processed: 0 },
        { space_id: 'impossible', last_consolidation: '2026-02-30T12:00:00Z' },
        { space_id: 'unqualified', last_consolidation: '2026-09-30T13:00:00' },
        { space_id: '<invalid>', last_consolidation: '2026-09-30T15:00:00Z' },
    ];
    const state = await routePortal(page, { spaces, lanes: spaces.map(s => ({ space_id: s.space_id, lane_state: 'idle', queued_count: 0 })) });
    await ready(page, state); await bounded(page, state, ['z-latest', 'tie-A', 'tie-z', 'a-oldest']);
    await expect(page.locator('#dashCardScope')).toContainText('2 consolidation date(s) unavailable');
    for (const id of ['tie-z', 'z-latest']) await expect(page.locator(`.dash-space-card[data-space="${id}"] dd`)).toHaveText(['Unavailable', 'Unavailable']);
    await page.locator('.dash-notes > summary').click(); await expect(page.locator('#dashNoteSpace option[value="never"]')).toHaveCount(1);
    await evidence(page, state, info, 'home-date-order-invalid-counters', viewport);
    for (const kind of ['ascii', 'reverse']) {
        const mutantPage = await context.newPage(); await configureViewport(mutantPage, viewport);
        const mutant = await routePortal(mutantPage, { spaces, lanes: state.lanes, mutationDashboardOrder: kind });
        await ready(mutantPage, mutant);
        const expected = ['z-latest', 'tie-A', 'tie-z', 'a-oldest'];
        const actual = await ids(mutantPage);
        expect(actual).not.toEqual(expected);
        await expect(bounded(mutantPage, mutant, expected)).rejects.toThrow('toEqual');
        mutant.observations = { rankingRejected: { kind, expected, actual } };
        await evidence(mutantPage, mutant, info, `mutation-ranking-${kind}`, viewport);
        expect(mutant.mutation.kind).toBe(`dashboard ranking ${kind}`);
        await mutantPage.close();
    }
});
for (const variant of ['invalid', 'missing-history', 'never']) {
    test(`#633 ${variant} dates never fabricate recency or read empty queues`, async ({ page }, info) => {
        const viewport = VIEWPORTS[0]; await configureViewport(page, viewport);
        const space = { space_id: 'demo', consolidation_count: variant === 'never' ? 0 : 3, total_notes_processed: variant === 'never' ? 0 : 50,
            last_consolidation: variant === 'invalid' ? 'not-an-ISO-date' : null };
        const state = await routePortal(page, { spaces: [space], lanes: [] }); await ready(page, state); await bounded(page, state, []);
        await evidence(page, state, info, `home-date-${variant}`, viewport);
        await expect(page.locator('#dashSpacesEmpty')).toContainText(variant === 'never' ? 'No consolidations yet' : 'Consolidation dates unavailable');
        await expect(page.locator('#dashActivityEmpty')).toContainText('No recent spaces to follow');
    });
}
test('#633 resize changes only visible queue scope and preserves surviving focus', async ({ page }, info) => {
    const viewport = VIEWPORTS[0]; await configureViewport(page, viewport);
    const state = await routePortal(page, inventory(25)); await ready(page, state);
    const recent = state.expectedRecent;
    const link = cards(page).first().getByRole('link', { name: recent[0], exact: true }); await tabTo(page, link);
    await page.setViewportSize({ width: 390, height: 844 }); await expect(cards(page)).toHaveCount(2);
    await expect.poll(() => state.reads('bank_consolidation_queues').length).toBe(2); await expect(link).toBeFocused();
    expect(state.reads('space_list')).toHaveLength(1);
    expect(state.reads('bank_consolidation_queues').at(-1).arguments.space_ids).toBe(recent.slice(0, 2).join(','));
    await evidence(page, state, info, 'home-resize-mobile', VIEWPORTS[2]);
    await page.setViewportSize({ width: 1920, height: 2160 }); await expect(cards(page)).toHaveCount(12);
    await expect.poll(() => state.reads('bank_consolidation_queues').length).toBe(3);
    expect(state.reads('space_list')).toHaveLength(1);
    expect(state.reads('bank_consolidation_queues').at(-1).arguments.space_ids.split(',')).toEqual(await ids(page));
    await expect(link).toBeFocused(); await evidence(page, state, info, 'home-resize-cap12', { name: 'large-cap12', width: 1920, height: 2160 });
});
test('#633 late inventory, busy resize, opt-in tick and read budget', async ({ page }, info) => {
    const viewport = VIEWPORTS[0]; await configureViewport(page, viewport); await page.clock.install();
    const data = inventory(25); let release;
    const state = await routePortal(page, { ...data, defer: { tool: 'space_list', promise: new Promise(resolve => { release = resolve; }) } });
    await page.goto(`${ORIGIN}/admin.html#/dashboard`); await expect.poll(() => state.reads('space_list').length).toBe(1);
    expect(state.reads('bank_consolidation_queues')).toHaveLength(0); await expect(page.locator('#dashSpacesEmpty')).toContainText('Loading');
    await page.setViewportSize({ width: 390, height: 844 }); release(); await expect(cards(page)).toHaveCount(2);
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(state.reads('bank_consolidation_queues').map(call => call.arguments.space_ids)).toEqual([data.expectedRecent.slice(0, 2).join(',')]);
    const baseline = state.calls.length; await page.clock.runFor(60001); expect(state.calls).toHaveLength(baseline);
    await page.locator('.dash-notes > summary').click();
    const selected = [];
    for (const s of data.spaces.slice(0, 3)) {
        selected.push(s.space_id); const selectionStart = state.calls.length;
        await page.locator('#dashNoteSpace').selectOption(s.space_id); await page.locator('#dashAddNotes').click();
        await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
        expect(state.calls.slice(selectionStart).map(call => call.tool)).toEqual(['bank_consolidation_queues', ...selected.map(() => 'live_read')]);
        expect(state.calls.slice(selectionStart).filter(call => call.tool === 'live_read').map(call => call.arguments.space_id)).toEqual(selected);
    }
    expect(state.reads('live_read')).toHaveLength(6);
    for (const call of state.reads('live_read')) expect(call.arguments.limit).toBe(20);
    await expect(page.locator('#dashAddNotes')).toBeDisabled(); await page.locator('#portalAutoEnabled').check();
    const before = state.calls.length; await page.clock.runFor(15001);
    await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
    expect(state.calls.slice(before).map(call => call.tool)).toEqual(['space_list', 'bank_consolidation_queues', 'live_read', 'live_read', 'live_read']);
    expect(state.reads('space_list').every(call => call.arguments.include_counts === false)).toBe(true);
    expect(state.reads('system_health')).toHaveLength(0); await evidence(page, state, info, 'home-late-catalog-budget', VIEWPORTS[2]);
});
for (const failure of ['metadata', 'queue', 'partial']) {
    test(`#633 ${failure} error remains distinguishable from idle and empty`, async ({ page }, info) => {
        const viewport = VIEWPORTS[0]; await configureViewport(page, viewport);
        const state = await routePortal(page, inventory(25)); await ready(page, state); const previous = await ids(page);
        if (failure === 'partial') state.queueEnvelope = { lanes: [], denied_spaces: [{ space_id: state.expectedRecent[0], message: `Synthetic denied ${HOSTILE}` }] };
        else state.fail = { tool: failure === 'metadata' ? 'space_list' : 'bank_consolidation_queues', message: `Synthetic ${failure} unavailable ${HOSTILE}` };
        await page.locator('#portalRefresh').click(); await expect.poll(() => page.evaluate(() => PortalRefresh.state().busy)).toBe(false);
        expect(await ids(page)).toEqual(previous);
        await expect(page.locator(failure === 'metadata' ? '#dashInventoryFreshness' : failure === 'queue' ? '#dashLanesFreshness' : '#dashQueueWarnings')).toContainText(failure === 'partial' ? 'Synthetic denied' : `Synthetic ${failure} unavailable`);
        if (failure === 'partial') await expect(page.locator('#dashActivityEmpty')).not.toContainText('No active jobs');
        for (const tool of ['live_read', 'space_info']) expect(state.reads(tool)).toHaveLength(0);
        await evidence(page, state, info, `home-${failure}-error`, viewport);
    });
}
