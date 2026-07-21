import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const apiPath = process.argv[2];
const appPath = process.argv[3];
assert.ok(apiPath, 'admin-api.js path is required');
assert.ok(appPath, 'admin-app.js path is required');

const apiSource = fs.readFileSync(apiPath, 'utf8');
const appSource = fs.readFileSync(appPath, 'utf8') + `
globalThis.__sessionHarness = {
    begin: _beginSessionGeneration,
    current: currentSessionGeneration,
    isCurrent: sessionGenerationIsCurrent,
    context: _ctx,
    showLogin,
    hideLogin,
    login: doLogin,
    logout: doLogout,
    boot: _bootAuthenticated,
    identity: () => _currentIdentity,
};`;

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

function flushTasks() {
    return new Promise(resolve => setImmediate(resolve));
}

function response(status, body = {}) {
    return {
        status,
        headers: { get: () => null },
        async json() { return body; },
        async text() { return JSON.stringify(body); },
    };
}

function classList(initial = []) {
    const values = new Set(initial);
    return {
        add(value) { values.add(value); },
        remove(value) { values.delete(value); },
        contains(value) { return values.has(value); },
        toggle(value, force) {
            if (force === true) values.add(value);
            else if (force === false) values.delete(value);
            else if (values.has(value)) values.delete(value);
            else values.add(value);
        },
    };
}

function element(extra = {}) {
    return Object.assign({
        innerHTML: '',
        textContent: '',
        value: '',
        disabled: false,
        isConnected: true,
        classList: classList(),
        focus() {},
        addEventListener() {},
        replaceChildren() { this.innerHTML = ''; },
        remove() { this.isConnected = false; },
        querySelector() { return null; },
        setAttribute() {},
        removeAttribute() {},
    }, extra);
}

function createHarness() {
    const fetches = [];
    const pendingFetches = [];
    const listeners = {};
    const elements = {
        loginOverlay: element({ classList: classList(['hidden']) }),
        loginError: element(),
        loginToken: element(),
        loginBtn: element({ textContent: 'Sign in' }),
        content: element(),
        identityBlock: element(),
        adminModal: element(),
        toastStack: element(),
        sidebarNav: element(),
        sidebarNavOperator: element(),
        logoutBtn: element(),
    };

    const document = {
        getElementById(id) { return elements[id] || null; },
        querySelectorAll() { return []; },
        querySelector() { return null; },
        addEventListener(type, handler) { listeners[type] = handler; },
        createElement() { return element(); },
        body: element({ appendChild() {} }),
    };
    const window = { addEventListener() {} };
    const location = {
        hash: '#/dashboard',
        replace(value) { this.hash = value; },
    };

    const context = {
        console,
        document,
        window,
        location,
        navigator: {},
        setTimeout() { return 0; },
        clearTimeout() {},
        fetch(url, options) {
            fetches.push({ url, options });
            assert.ok(pendingFetches.length, `unexpected fetch: ${url}`);
            return pendingFetches.shift().promise;
        },
    };
    vm.createContext(context);
    vm.runInContext(apiSource, context, { filename: apiPath });
    vm.runInContext(appSource, context, { filename: appPath });

    return {
        api: context,
        session: context.__sessionHarness,
        elements,
        fetches,
        listeners,
        enqueueFetch() {
            const item = deferred();
            pendingFetches.push(item);
            return item;
        },
    };
}

async function rejectsUnauthorized(promise) {
    await assert.rejects(promise, /Unauthorized/);
}

async function current401InvalidatesSynchronously() {
    const h = createHarness();
    h.session.begin();
    h.session.hideLogin();
    const before = h.session.current();
    const request = h.enqueueFetch();
    const pending = h.api.callTool('space_list', {});
    request.resolve(response(401));
    await rejectsUnauthorized(pending);
    assert.equal(h.session.current(), before + 1);
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), false);
    assert.equal(h.elements.loginError.textContent, 'Session expired.');
}

async function stale401CannotWipeNewerSession() {
    const h = createHarness();
    h.session.begin();
    h.session.hideLogin();
    const oldRequest = h.enqueueFetch();
    const pending = h.api.callTool('space_list', {});

    // Same displayed identity would still be a distinct cookie session.
    h.session.begin();
    const newerGeneration = h.session.current();
    h.session.hideLogin();
    h.elements.loginError.textContent = 'new session';

    oldRequest.resolve(response(401));
    await rejectsUnauthorized(pending);
    assert.equal(h.session.current(), newerGeneration);
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), true);
    assert.equal(h.elements.loginError.textContent, 'new session');
}

async function staleSuccessCannotCrossSession() {
    const h = createHarness();
    h.session.begin();
    const oldRequest = h.enqueueFetch();
    const pending = h.api.callTool('space_list', {});
    h.session.begin();
    oldRequest.resolve(response(200, { status: 'ok', spaces: ['secret-a'] }));
    await assert.rejects(pending, /Stale session/);
}

async function staleNetworkFailureCannotCrossSession() {
    const h = createHarness();
    h.session.begin();
    const oldRequest = h.enqueueFetch();
    const pending = h.api.callTool('space_list', {});
    h.session.begin();
    oldRequest.reject(new Error('old network failure'));
    await assert.rejects(pending, /Stale session/);
}

async function staleBodyFailureCannotCrossSession() {
    const h = createHarness();
    h.session.begin();
    const oldRequest = h.enqueueFetch();
    const body = deferred();
    const textStarted = deferred();
    const pending = h.api.callTool('space_list', {});
    oldRequest.resolve({
        status: 200,
        headers: { get: () => null },
        text() {
            textStarted.resolve();
            return body.promise;
        },
    });
    await textStarted.promise;
    h.session.begin();
    body.reject(new Error('old body failure'));
    await assert.rejects(pending, /Stale session/);
}

async function staleWhoamiCannotRestoreIdentityOrDom() {
    const h = createHarness();
    h.session.begin();
    h.session.hideLogin();
    const whoami = h.enqueueFetch();
    const pending = h.session.boot();

    h.session.showLogin('Signed out');
    const invalidatedGeneration = h.session.current();
    const wipedHtml = h.elements.content.innerHTML;
    whoami.resolve(response(200, {
        status: 'ok',
        client_name: 'same-name',
        permissions: ['admin'],
    }));
    await pending;

    assert.equal(h.session.current(), invalidatedGeneration);
    assert.deepEqual(Object.keys(h.session.identity()), []);
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), false);
    assert.equal(h.elements.content.innerHTML, wipedHtml);
    assert.doesNotMatch(h.elements.content.innerHTML, /same-name/);
}

async function logoutInvalidatesBeforeNetworkCompletes() {
    const h = createHarness();
    h.session.begin();
    h.session.hideLogin();
    h.elements.content.innerHTML = 'privileged';
    const before = h.session.current();
    const logout = h.enqueueFetch();
    const pending = h.session.logout();

    assert.equal(h.session.current(), before + 1);
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), false);
    assert.doesNotMatch(h.elements.content.innerHTML, /privileged/);
    assert.match(h.elements.content.innerHTML, /state-loading/);
    assert.equal(h.elements.loginBtn.disabled, true);

    h.elements.loginToken.value = 'must-not-race-logout';
    await h.session.login();
    assert.equal(h.fetches.length, 1, 'Enter during logout must not start login');
    assert.equal(h.session.current(), before + 1);
    await h.session.logout();
    assert.equal(h.fetches.length, 1, 'overlapping logout Set-Cookie is forbidden');

    logout.resolve(response(200, { status: 'ok' }));
    await pending;
    assert.equal(h.elements.loginBtn.disabled, false);
    assert.equal(h.elements.loginBtn.textContent, 'Sign in');
}

async function supersededLoginCannotBoot() {
    const h = createHarness();
    h.elements.loginOverlay.classList.remove('hidden');
    h.elements.loginToken.value = 'token-a';
    const login = h.enqueueFetch();
    const pending = h.session.login();
    const loginGeneration = h.session.current();
    assert.equal(h.elements.loginBtn.disabled, true);

    h.session.showLogin('Superseded');
    assert.notEqual(h.session.current(), loginGeneration);
    assert.equal(h.elements.loginBtn.disabled, true);
    h.elements.loginToken.value = 'token-b';
    await h.session.login();
    assert.equal(h.fetches.length, 1, 'overlapping login Set-Cookie is forbidden');
    login.resolve(response(200, { status: 'ok' }));
    await pending;

    assert.equal(h.fetches.length, 1, 'stale login must not issue system_whoami');
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), false);
    assert.deepEqual(Object.keys(h.session.identity()), []);
    assert.equal(h.elements.loginBtn.disabled, false);
}

async function staleInitialProbeCannotUndoManualLogin() {
    const h = createHarness();
    h.elements.loginOverlay.classList.remove('hidden');
    const health = h.enqueueFetch();
    const initialProbe = h.enqueueFetch();
    h.listeners.DOMContentLoaded();

    h.elements.loginToken.value = 'manual-login';
    const login = h.enqueueFetch();
    const pendingLogin = h.session.login();
    const manualGeneration = h.session.current();
    const whoami = h.enqueueFetch();
    login.resolve(response(200, { status: 'ok' }));
    await flushTasks();
    assert.equal(h.fetches.length, 4, 'manual login must start system_whoami');
    whoami.resolve(response(200, {
        status: 'ok',
        client_name: 'manual-session',
        permissions: ['admin'],
    }));
    await pendingLogin;
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), true);
    assert.equal(h.session.identity().client_name, 'manual-session');

    initialProbe.resolve(response(401));
    health.resolve(response(200, {}));
    await flushTasks();
    assert.equal(h.session.current(), manualGeneration);
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), true);
    assert.equal(h.session.identity().client_name, 'manual-session');
}

function contextExposesGeneration() {
    const h = createHarness();
    const generation = h.session.begin();
    assert.equal(h.session.context().sessionGeneration, generation);
    assert.equal(h.session.isCurrent(generation), true);
    assert.equal(h.session.isCurrent(generation - 1), false);
}

// P10-4 (#192) Mesh REST client (_meshFetch, used by meshAdminStatus/
// meshAdminMembers/meshAdminAction) — same stale-session race callTool()
// guards against, but _meshFetch RETURNS an error-shaped object instead of
// throwing, so these assert on the returned value rather than assert.rejects.
async function staleMesh401CannotWipeNewerSession() {
    const h = createHarness();
    h.session.begin();
    h.session.hideLogin();
    const oldRequest = h.enqueueFetch();
    const pending = h.api.meshAdminAction('cancel', { pair_id: 'pair_x' });

    h.session.begin();
    const newerGeneration = h.session.current();
    h.session.hideLogin();
    h.elements.loginError.textContent = 'new session';

    oldRequest.resolve(response(401));
    const result = await pending;
    assert.equal(result.status, 'error');
    assert.equal(result.message, 'Unauthorized');
    assert.equal(h.session.current(), newerGeneration);
    assert.equal(h.elements.loginOverlay.classList.contains('hidden'), true, 'a stale 401 must not re-show the login overlay over the newer session');
    assert.equal(h.elements.loginError.textContent, 'new session');
}

async function staleMeshSuccessCannotCrossSession() {
    const h = createHarness();
    h.session.begin();
    const oldRequest = h.enqueueFetch();
    const pending = h.api.meshAdminAction('cancel', { pair_id: 'pair_x' });
    h.session.begin();
    oldRequest.resolve(response(200, { status: 'ok', pair_id: 'pair_x', state: 'cancelled' }));
    const result = await pending;
    assert.equal(result.status, 'error');
    assert.equal(result.message, 'Stale session');
}

await current401InvalidatesSynchronously();
await stale401CannotWipeNewerSession();
await staleSuccessCannotCrossSession();
await staleNetworkFailureCannotCrossSession();
await staleBodyFailureCannotCrossSession();
await staleWhoamiCannotRestoreIdentityOrDom();
await logoutInvalidatesBeforeNetworkCompletes();
await supersededLoginCannotBoot();
await staleInitialProbeCannotUndoManualLogin();
contextExposesGeneration();
await staleMesh401CannotWipeNewerSession();
await staleMeshSuccessCannotCrossSession();
console.log('admin session generation runtime: ok');
