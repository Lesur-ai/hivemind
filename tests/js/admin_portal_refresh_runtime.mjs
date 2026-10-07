import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const subject = process.argv[2];
assert.ok(subject, 'portal-refresh.js path is required');
const source = fs.readFileSync(subject, 'utf8');
const KEY = 'hivemind.portal.autoRefresh';
const settle = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function harness(stored = null, storageThrows = false) {
    const timers = [], writes = [], events = [], listeners = {};
    let epoch = 1, generation = 1, now = 1000;
    const context = {
        console, Date: { now: () => ++now }, Promise,
        AdminRouter: { get epoch() { return epoch; } },
        sessionGenerationIsCurrent: value => value === generation,
        localStorage: {
            getItem(key) { assert.equal(key, KEY); if (storageThrows) throw new Error('storage unavailable'); return stored; },
            setItem(key, value) { if (storageThrows) throw new Error('storage unavailable'); writes.push({ key, value }); stored = value; },
        },
        CustomEvent: class { constructor(type) { this.type = type; } },
        document: {
            hidden: false,
            addEventListener(name, fn) { listeners[name] = fn; },
            dispatchEvent(event) { events.push(event.type); },
        },
        setTimeout(fn, ms) { const id = timers.length + 1; timers.push({ id, fn, ms }); return id; },
        clearTimeout(id) { const timer = timers.find(t => t.id === id); if (timer) timer.cancelled = true; },
    };
    vm.createContext(context);
    vm.runInContext(source + '\nglobalThis.controller = PortalRefresh;', context, { filename: subject });
    const api = context.controller;
    const pending = () => timers.filter(t => !t.cancelled && !t.fired);
    return {
        api, context, writes, events, pending,
        register(refresh, canAuto) { api.register({ refresh, canAuto }, { epoch, sessionGeneration: generation }); },
        async fire(timer = pending()[0]) { assert.ok(timer, 'expected a pending timer'); timer.fired = true; timer.fn(); await settle(); },
        async visibility(hidden) { context.document.hidden = hidden; listeners.visibilitychange(); await settle(); },
        nextEpoch() { epoch += 1; }, nextSession() { generation += 1; },
    };
}

// Strict, non-secret preference; inaccessible storage never prevents manual use.
for (const bad of [null, '', '{', 'null', '[]', '{}', '{"enabled":true,"intervalSeconds":10}', '{"enabled":"yes","intervalSeconds":15}', '{"enabled":true,"intervalSeconds":15,"token":"secret"}']) {
    const h = harness(bad);
    assert.equal(h.api.state().enabled, false);
    assert.equal(h.api.state().intervalSeconds, 15);
    assert.equal(h.api.state().lastSuccess, null);
}
{
    const h = harness('{"enabled":true,"intervalSeconds":30}');
    h.register(async () => {});
    assert.equal(h.api.state().available, false, 'no session before identity');
    assert.equal(h.pending().length, 0);
    h.api.beginSession(); h.register(async () => {});
    assert.equal(h.pending()[0].ms, 30000);
    h.api.configure({ enabled: true, intervalSeconds: 60, identity: 'secret' });
    assert.equal(h.api.state().enabled, false, 'extra configuration fields fail closed');
    h.api.configure({ enabled: true, intervalSeconds: 60 });
    for (const write of h.writes) {
        assert.equal(write.key, KEY);
        assert.deepEqual(Object.keys(JSON.parse(write.value)).sort(), ['enabled', 'intervalSeconds']);
        assert.equal(write.value.includes('secret'), false);
    }
    h.api.endSession();
    assert.equal(h.api.state().enabled, true, 'session expiry retains the non-secret preference');
    assert.equal(h.api.state().available, false);
    h.api.endSession({ logout: true });
    assert.equal(JSON.parse(h.writes.at(-1).value).enabled, false);
    assert.equal(h.pending().length, 0);
    assert.equal(h.api.state().available, false);
}
{
    const h = harness(null, true); let calls = 0;
    h.api.beginSession(); h.register(async () => { calls += 1; });
    h.api.configure({ enabled: true, intervalSeconds: 30 });
    assert.equal(h.api.state().enabled, false); assert.equal(h.api.state().intervalSeconds, 15);
    await h.api.refresh(); assert.equal(calls, 1);
}

// Manual read works while off. A completion-based loop serializes manual and automatic calls.
{
    const h = harness(); let calls = 0, automatic, guard;
    const first = deferred();
    h.api.beginSession();
    h.register(({ automatic: value, isCurrent }) => { calls += 1; automatic = value; guard = isCurrent; return first.promise; });
    assert.equal(h.api.state().available, true); assert.equal(h.api.state().eligible, true);
    const a = h.api.refresh(); const b = h.api.refresh();
    await settle(); assert.equal(calls, 1); assert.equal(automatic, false); assert.equal(guard(), true);
    assert.equal(h.api.state().busy, true); assert.equal(h.pending().length, 0);
    first.resolve({ polling: { recommended: false }, jobs: [{ polling: { recommended: false } }] }); await Promise.all([a, b]);
    assert.ok(h.api.state().lastSuccess); assert.equal(h.api.state().busy, false);
    assert.equal(h.pending().length, 0, 'off never follows automatically');
    h.api.configure({ enabled: true, intervalSeconds: 15 });
    assert.equal(h.pending()[0].ms, 15000);
    await h.fire(); assert.equal(calls, 2); assert.equal(automatic, true);
    assert.equal(h.pending().length, 1, 'agent polling advice does not stop the human UI');
    assert.equal(h.events.every(event => event === 'portal:refresh-change'), true);
}
{
    const h = harness(); let calls = 0; const wait = deferred();
    h.api.beginSession(); h.register(() => { calls += 1; return wait.promise; });
    h.api.configure({ enabled: true, intervalSeconds: 15 }); await h.fire();
    const manual = h.api.refresh(); await settle(); assert.equal(calls, 1);
    assert.equal(h.pending().length, 0, 'nothing scheduled while callback is unresolved');
    wait.resolve({}); await manual; await settle(); assert.equal(h.pending().length, 1);
}

// Hide pauses reads; visibility return causes one read, never accumulated ticks.
{
    const h = harness(); let calls = 0;
    h.api.beginSession(); h.register(async () => { calls += 1; });
    h.api.configure({ enabled: true, intervalSeconds: 15 });
    const staleTimer = h.pending()[0];
    await h.visibility(true); assert.equal(h.pending().length, 0);
    await h.fire(staleTimer); await h.api.refresh(); assert.equal(calls, 0);
    await h.visibility(false); assert.equal(calls, 1); assert.equal(h.pending().length, 1);
    await h.visibility(false); assert.equal(calls, 1, 'duplicate visibility event is not a new wakeup');
}

// Stale requests cannot publish success/error or restart a route/session/disabled loop.
// Manual requests made while hidden are coalesced and retained even with auto off.
{
    const h = harness(); let calls = 0;
    h.api.beginSession(); h.register(async ({ automatic }) => { assert.equal(automatic, false); calls++; }, () => false);
    await h.visibility(true);
    await Promise.all([h.api.refresh(), h.api.refresh()]);
    assert.equal(calls, 0); assert.equal(h.api.state().lastSuccess, null);
    await h.visibility(false);
    assert.equal(calls, 1, 'returning runs the deferred manual read with auto off');
    assert.equal(h.pending().length, 0);
    await h.visibility(false); assert.equal(calls, 1);
}
{
    const h = harness(); let calls = 0;
    h.api.beginSession(); h.register(async () => { calls++; });
    const flight = h.api.refresh();
    await h.visibility(true); await flight;
    assert.equal(calls, 0, 'hiding before dispatch defers the manual read');
    assert.equal(h.api.state().lastSuccess, null);
    await h.visibility(false); assert.equal(calls, 1);
}
for (const change of ['route', 'session', 'clear', 'logout', 'replace']) {
    const h = harness(); let calls = 0;
    h.api.beginSession(); h.register(async () => { calls++; });
    await h.visibility(true); await h.api.refresh();
    if (change === 'route') h.nextEpoch();
    if (change === 'session') h.nextSession();
    if (change === 'clear') h.api.clearRoute();
    if (change === 'logout') h.api.endSession({ logout: true });
    if (change === 'replace') h.register(async () => { calls++; });
    await h.visibility(false);
    assert.equal(calls, 0, change + ' discards the obsolete deferred request');
}
{
    const h = harness(); const wait = deferred(); let calls = 0;
    h.api.beginSession(); h.register(() => { calls++; return calls === 1 ? wait.promise : {}; });
    const flight = h.api.refresh(); await settle();
    await h.visibility(true);
    const hiddenRequests = [h.api.refresh(), h.api.refresh()]; await settle();
    await h.visibility(false); assert.equal(calls, 1, 'visibility cannot overlap a pending read');
    wait.resolve({}); await Promise.all([flight, ...hiddenRequests]); await settle();
    assert.equal(calls, 2, 'the coalesced manual request survives the previous flight');
    assert.equal(h.pending().length, 0);
}

for (const change of ['route', 'session', 'disable', 'clear', 'logout']) {
    const h = harness(); const wait = deferred(); let guard;
    h.api.beginSession(); h.register(({ isCurrent }) => { guard = isCurrent; return wait.promise; });
    h.api.configure({ enabled: true, intervalSeconds: 15 }); await h.fire();
    if (change === 'route') h.nextEpoch();
    if (change === 'session') h.nextSession();
    if (change === 'disable') h.api.configure({ enabled: false, intervalSeconds: 15 });
    if (change === 'clear') h.api.clearRoute();
    if (change === 'logout') h.api.endSession({ logout: true });
    assert.equal(guard(), false, change + ' invalidates painting');
    wait.resolve({}); await settle();
    assert.equal(h.pending().length, 0, change + ' cannot rearm');
    assert.equal(h.api.state().lastSuccess, null, change + ' cannot publish freshness');
}
{
    const h = harness(); const wait = deferred();
    h.api.beginSession(); h.register(() => wait.promise);
    const old = h.api.refresh(); await settle(); h.api.clearRoute(); h.nextEpoch();
    h.register(async () => ({})); await h.api.refresh(); const fresh = h.api.state().lastSuccess;
    wait.reject(new Error('old request')); await assert.rejects(old, /old request/);
    assert.equal(h.api.state().error, null); assert.equal(h.api.state().lastSuccess, fresh);
}

// A disable/re-enable cycle cannot revive the old request or overlap it.
{
    const h = harness(); const wait = deferred(); let guard, calls = 0;
    h.api.beginSession(); h.register(({ isCurrent }) => { guard = isCurrent; calls += 1; return wait.promise; });
    h.api.configure({ enabled: true, intervalSeconds: 15 }); await h.fire();
    h.api.configure({ enabled: false, intervalSeconds: 15 });
    h.api.configure({ enabled: true, intervalSeconds: 15 });
    assert.equal(guard(), false, 're-enabling cannot revive old automatic painting');
    assert.equal(h.pending().length, 0, 're-enabled loop waits for cancelled read to settle');
    wait.resolve({}); await settle();
    assert.equal(h.api.state().lastSuccess, null);
    assert.equal(h.api.state().busy, false);
    assert.equal(h.pending().length, 1, 'explicit re-enable starts one fresh cycle');
    await h.fire(); assert.equal(calls, 2); assert.ok(h.api.state().lastSuccess);
}

// Bounded exponential wait retains previous freshness; errors are also thrown to manual callers.
{
    const h = harness(); let fails = false;
    h.api.beginSession(); h.register(async () => { if (fails) throw new Error('fixture failure'); return {}; });
    await h.api.refresh(); const success = h.api.state().lastSuccess;
    h.api.configure({ enabled: true, intervalSeconds: 15 }); fails = true;
    for (const ms of [30000, 60000, 120000, 120000]) {
        await h.fire(); assert.equal(h.pending()[0].ms, ms);
        assert.ok(h.api.state().error); assert.equal(h.api.state().lastSuccess, success);
    }
    await assert.rejects(h.api.refresh(), /fixture failure/);
    fails = false; await h.fire(); assert.equal(h.pending()[0].ms, 15000);
    assert.equal(h.api.state().error, null);
}

// Eligibility and explicit terminal follow prevent ticks but retain manual actions.
// A superseded consumer result is neither success nor failure, including backoff/follow.
{
    const h = harness(); let result = {}, fails = false;
    h.api.beginSession(); h.register(async () => { if (fails) throw new Error('current failure'); return result; });
    await h.api.refresh(); const success = h.api.state().lastSuccess;
    h.api.configure({ enabled: true, intervalSeconds: 15 });
    fails = true; await assert.rejects(h.api.refresh(), /current failure/);
    assert.equal(h.pending()[0].ms, 30000);
    fails = false; result = { skipped: true, follow: false }; await h.api.refresh();
    assert.equal(h.api.state().lastSuccess, success, 'superseded read cannot claim freshness');
    assert.equal(h.api.state().error, 'Refresh failed', 'superseded read cannot clear the previous failure');
    assert.equal(h.pending()[0].ms, 30000, 'superseded read retains backoff and follow');
    result = { follow: false }; await h.api.refresh();
    const terminalSuccess = h.api.state().lastSuccess;
    result = { skipped: true, follow: true }; await h.api.refresh();
    assert.equal(h.api.state().lastSuccess, terminalSuccess);
    assert.equal(h.pending().length, 0, 'superseded read cannot revive terminal follow');
}

{
    const h = harness(); let calls = 0, allowed = false;
    h.api.beginSession(); h.register(async () => { calls += 1; return { follow: false }; }, () => allowed);
    h.api.configure({ enabled: true, intervalSeconds: 15 });
    assert.equal(h.api.state().available, true); assert.equal(h.api.state().eligible, false);
    assert.equal(h.pending().length, 0); allowed = true;
    await h.api.refresh(); assert.equal(calls, 1); assert.equal(h.pending().length, 0);
    await h.api.refresh(); assert.equal(calls, 2, 'terminal detail remains manually readable');
    h.api.clearRoute(); assert.equal(h.api.state().available, false);
}
console.log('portal refresh runtime: preferences, ownership, single-flight, visibility, backoff and terminal follow: ok');
