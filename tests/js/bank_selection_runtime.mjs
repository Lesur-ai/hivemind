import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const configPath = process.argv[2];
const apiPath = process.argv[3];
const bankPath = process.argv[4];
assert.ok(configPath, 'config.js path is required');
assert.ok(apiPath, 'api.js path is required');
assert.ok(bankPath, 'bank.js path is required');

const configSource = fs.readFileSync(configPath, 'utf8');
const apiSource = fs.readFileSync(apiPath, 'utf8');
const bankSource = fs.readFileSync(bankPath, 'utf8') + `
globalThis.__bankHarness = { selectBank, renderBankTabs, app };`;

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

function response(status, body = {}) {
    return {
        status,
        headers: { get: () => null },
        async json() { return body; },
        async text() { return JSON.stringify(body); },
    };
}

function element(extra = {}) {
    return Object.assign({ innerHTML: '' }, extra);
}

function createHarness() {
    const pendingFetches = [];
    const fetchCalls = [];
    const loginCalls = [];
    const elements = {
        bankTabs: element(),
        bankCount: element(),
        bankContent: element(),
    };
    const document = {
        getElementById(id) { return elements[id] || null; },
        querySelectorAll() { return []; },
    };
    const context = {
        console,
        document,
        showLogin(msg) { loginCalls.push(msg); },
        fetch(url) {
            fetchCalls.push(url);
            assert.ok(pendingFetches.length, `unexpected fetch: ${url}`);
            return pendingFetches.shift().promise;
        },
    };
    vm.createContext(context);
    vm.runInContext(configSource, context, { filename: configPath });
    vm.runInContext(apiSource, context, { filename: apiPath });
    vm.runInContext(bankSource, context, { filename: bankPath });
    assert.ok(context.__bankHarness, 'bank.js instrumentation failed');

    return {
        bank: context.__bankHarness,
        elements,
        fetchCalls,
        loginCalls,
        enqueueFetch() {
            const item = deferred();
            pendingFetches.push(item);
            return item;
        },
    };
}

// Start request A, select B, resolve B, then resolve A: B remains selected
// and rendered (issue's literal deterministic acceptance criterion). Two
// selectBank() calls happen here, so the request-generation comparison alone
// already explains the outcome; see the dedicated *CheckAloneCatchesStaleness
// tests below for isolation of the space/filename comparisons specifically.
async function resolvingOutOfOrderKeepsNewerFileSelected() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';

    const reqA = h.enqueueFetch();
    const pendingA = h.bank.selectBank('alpha.md');
    assert.equal(h.elements.bankContent.innerHTML, '<div class="empty-state">Loading…</div>');

    const reqB = h.enqueueFetch();
    const pendingB = h.bank.selectBank('beta.md');

    reqB.resolve(response(200, { status: 'ok', content: '# Beta content' }));
    await pendingB;
    assert.match(h.elements.bankContent.innerHTML, /Beta content/);

    reqA.resolve(response(200, { status: 'ok', content: '# Alpha content' }));
    await pendingA;
    assert.match(h.elements.bankContent.innerHTML, /Beta content/);
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /Alpha content/);
}

// ABA: alpha -> beta -> alpha. The FIRST alpha request is an OLDER instance
// targeting the exact same (space, filename) as the current selection, so
// space/filename identity alone cannot tell it apart from the second alpha
// request — only the request generation can. Isolates that comparison.
async function abaSameFileReselectionKeepsNewestRequest() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';

    const reqAlpha1 = h.enqueueFetch();
    const pendingAlpha1 = h.bank.selectBank('alpha.md');

    const reqBeta = h.enqueueFetch();
    const pendingBeta = h.bank.selectBank('beta.md');
    reqBeta.resolve(response(200, { status: 'ok', content: '# Beta content' }));
    await pendingBeta;
    assert.match(h.elements.bankContent.innerHTML, /Beta content/);

    const reqAlpha2 = h.enqueueFetch();
    const pendingAlpha2 = h.bank.selectBank('alpha.md');
    reqAlpha2.resolve(response(200, { status: 'ok', content: '# Alpha second render' }));
    await pendingAlpha2;
    assert.match(h.elements.bankContent.innerHTML, /Alpha second render/);

    reqAlpha1.resolve(response(200, { status: 'ok', content: '# STALE ALPHA FIRST — must not appear' }));
    await pendingAlpha1;
    assert.match(h.elements.bankContent.innerHTML, /Alpha second render/);
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /STALE ALPHA FIRST/);
}

// Same ABA shape, but the stale first alpha request comes back as a
// server-shaped error rather than a success.
async function abaSameFileReselectionDiscardsStaleFirstError() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';

    const reqAlpha1 = h.enqueueFetch();
    const pendingAlpha1 = h.bank.selectBank('alpha.md');
    const reqBeta = h.enqueueFetch();
    const pendingBeta = h.bank.selectBank('beta.md');
    reqBeta.resolve(response(200, { status: 'ok', content: '# Beta content' }));
    await pendingBeta;

    const reqAlpha2 = h.enqueueFetch();
    const pendingAlpha2 = h.bank.selectBank('alpha.md');
    reqAlpha2.resolve(response(200, { status: 'ok', content: '# Alpha second render' }));
    await pendingAlpha2;
    assert.match(h.elements.bankContent.innerHTML, /Alpha second render/);

    reqAlpha1.resolve(response(200, { status: 'error', message: 'stale alpha first error' }));
    await pendingAlpha1;
    assert.match(h.elements.bankContent.innerHTML, /Alpha second render/);
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /stale alpha first error/);
}

// Start request A, select B, render B, then A comes back as a server error:
// the stale A error must not replace B's already-rendered content.
async function staleNonAuthErrorDoesNotReplaceNewerContent() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';

    const reqA = h.enqueueFetch();
    const pendingA = h.bank.selectBank('alpha.md');
    const reqB = h.enqueueFetch();
    const pendingB = h.bank.selectBank('beta.md');

    reqB.resolve(response(200, { status: 'ok', content: '# Beta content' }));
    await pendingB;
    assert.match(h.elements.bankContent.innerHTML, /Beta content/);

    reqA.resolve(response(200, { status: 'error', message: 'boom' }));
    await pendingA;
    assert.match(h.elements.bankContent.innerHTML, /Beta content/);
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /boom/);
}

async function newestNonStaleSuccessRendersNormally() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';
    const req = h.enqueueFetch();
    const pending = h.bank.selectBank('alpha.md');
    req.resolve(response(200, { status: 'ok', content: '# Alpha content' }));
    await pending;
    assert.match(h.elements.bankContent.innerHTML, /Alpha content/);
    assert.equal(h.fetchCalls.length, 1);
    assert.match(h.fetchCalls[0], /\/api\/bank\/space-1\/alpha\.md$/);
}

async function newestNonStaleErrorRendersSafely() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';
    const req = h.enqueueFetch();
    const pending = h.bank.selectBank('alpha.md');
    req.resolve(response(200, { status: 'error', message: 'Read failed' }));
    await pending;
    assert.match(h.elements.bankContent.innerHTML, /Read failed/);
    assert.match(h.elements.bankContent.innerHTML, /empty-state/);
}

// The old request and the new one target the SAME filename (a realistic case:
// every Hivemind space conventionally has a progress.md), through the real
// loadSpace()-shaped sequence: space switch resets currentBankFile, then the
// new space auto-selects its own same-named file (a second selectBank()
// call). A filename-only guard would wrongly call the stale response fresh;
// see *CheckAloneCatchesStaleness below for isolation of the space
// comparison specifically, independent of the generation bump this second
// call also causes.
async function spaceChangeAloneInvalidatesSameFilenameResponse() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';
    const reqOld = h.enqueueFetch();
    const pendingOld = h.bank.selectBank('progress.md');

    // Simulate loadSpace(): space switch resets currentBankFile, then the
    // newly loaded space auto-selects its own file, also named progress.md.
    h.bank.app.spaceId = 'space-2';
    h.bank.app.currentBankFile = null;
    const reqNew = h.enqueueFetch();
    const pendingNew = h.bank.selectBank('progress.md');

    reqNew.resolve(response(200, { status: 'ok', content: '# space-2 progress' }));
    await pendingNew;
    assert.match(h.elements.bankContent.innerHTML, /space-2 progress/);

    reqOld.resolve(response(200, { status: 'ok', content: '# space-1 progress WRONG-SPACE' }));
    await pendingOld;
    assert.match(h.elements.bankContent.innerHTML, /space-2 progress/);
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /WRONG-SPACE/);

    assert.match(h.fetchCalls[0], /\/api\/bank\/space-1\/progress\.md$/);
    assert.match(h.fetchCalls[1], /\/api\/bank\/space-2\/progress\.md$/);
}

// Changing spaces while a bank request is pending invalidates the old
// response even when the newly selected file has a different name too.
async function spaceChangeInvalidatesPendingResponseGenerally() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';
    const reqOld = h.enqueueFetch();
    const pendingOld = h.bank.selectBank('alpha.md');

    h.bank.app.spaceId = 'space-2';
    h.bank.app.currentBankFile = null;

    reqOld.resolve(response(200, { status: 'ok', content: '# stale alpha' }));
    await pendingOld;
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /stale alpha/);
}

// Pure isolation of the space-identity comparison: only app.spaceId changes
// after the request is issued — no second selectBank() call (so the request
// generation is untouched) and currentBankFile is left exactly as the
// in-flight request set it (so the filename comparison alone would call this
// fresh). This scenario cannot be reached through today's loadSpace(), which
// always pairs a spaceId change with resetting currentBankFile — the check
// still guards selectBank()'s own contract independently of that pairing, so
// a future loadSpace() change (e.g. remembering the last file per space)
// cannot silently reintroduce cross-space staleness.
async function spaceIdentityCheckAloneCatchesStaleness() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';
    const req = h.enqueueFetch();
    const pending = h.bank.selectBank('progress.md');

    h.bank.app.spaceId = 'space-2'; // only this changes

    req.resolve(response(200, { status: 'ok', content: '# WRONG-SPACE progress' }));
    await pending;
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /WRONG-SPACE/);
}

// Pure isolation of the filename-identity comparison: only
// app.currentBankFile changes after the request is issued — no second
// selectBank() call (generation untouched) and app.spaceId is left as-is
// (space comparison alone would call this fresh). Mirrors what loadSpace()
// does to currentBankFile alone, isolated from the spaceId change it also
// makes in the same synchronous step.
async function filenameIdentityCheckAloneCatchesStaleness() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';
    const req = h.enqueueFetch();
    const pending = h.bank.selectBank('alpha.md');

    h.bank.app.currentBankFile = 'beta.md'; // only this changes

    req.resolve(response(200, { status: 'ok', content: '# STALE ALPHA — wrong file now selected' }));
    await pending;
    assert.doesNotMatch(h.elements.bankContent.innerHTML, /STALE ALPHA/);
}

// Unauthorized must stay centrally handled (showLogin), regardless of
// staleness: selectBank must not render a local error for it.
async function unauthorizedErrorPreservesCentralizedHandling() {
    const h = createHarness();
    h.bank.app.spaceId = 'space-1';
    const req = h.enqueueFetch();
    const pending = h.bank.selectBank('alpha.md');
    req.resolve(response(401));
    await pending;
    assert.equal(h.elements.bankContent.innerHTML, '<div class="empty-state">Loading…</div>');
    assert.deepEqual(h.loginCalls, ['Session expired.']);
}

await resolvingOutOfOrderKeepsNewerFileSelected();
await abaSameFileReselectionKeepsNewestRequest();
await abaSameFileReselectionDiscardsStaleFirstError();
await staleNonAuthErrorDoesNotReplaceNewerContent();
await newestNonStaleSuccessRendersNormally();
await newestNonStaleErrorRendersSafely();
await spaceChangeAloneInvalidatesSameFilenameResponse();
await spaceChangeInvalidatesPendingResponseGenerally();
await spaceIdentityCheckAloneCatchesStaleness();
await filenameIdentityCheckAloneCatchesStaleness();
await unauthorizedErrorPreservesCentralizedHandling();
console.log('bank selection runtime: ok');
