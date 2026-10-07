import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const accessPath = process.argv[2];
const spacesPath = process.argv[3];
assert.ok(accessPath && spacesPath, 'access and spaces view paths are required');

const flush = async () => {
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
};

const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

function node(value = '') {
    const attributes = {};
    const listeners = {};
    const item = {
        value,
        attributes,
        hidden: false,
        disabled: false,
        checked: false,
        isConnected: true,
        textContent: '',
        innerHTML: '',
        style: { display: 'block' },
        classList: { contains: name => name === 'hidden', add() {}, remove() {} },
        addEventListener(name, callback) { listeners[name] = callback; },
        dispatch(name) { if (listeners[name]) return listeners[name]({ target: this }); },
        removeAttribute(name) { delete attributes[name]; },
        setAttribute(name, value) { attributes[name] = String(value); },
        querySelectorAll() { return []; },
        cloneNode() {
            const clone = node(this.value);
            clone.hidden = this.hidden;
            clone.disabled = this.disabled;
            clone.checked = this.checked;
            clone.textContent = this.textContent;
            clone.innerHTML = this.innerHTML;
            Object.entries(this.attributes).forEach(([name, value]) => clone.setAttribute(name, value));
            return clone;
        },
        replaceWith(replacement) {
            this.isConnected = false;
            this.replacement = replacement;
        },
    };
    return item;
}

function accessHarness(identity, overrides = {}) {
    let render;
    let modal = null;
    const actions = {};
    const calls = [];
    const toasts = [];
    const cache = { spaces: [{ space_id: 'stale-admin-space' }], tokens: [{ hash: 'secret-admin-row' }] };
    const editBoxes = [node('alpha'), node('beta')];
    let grantBoxes = [];
    const copied = [];
    const elements = new Map([
        ['loginOverlay', node()],
        ['adminModal', node()],
        ['accessBanners', node()],
        ['accessMsg', node()],
        ['accessTable', node()],
        ['accessCount', node()],
        ['ctName', node('manager-child')],
        ['ctNameErr', node()],
        ['ctNameHint', node()],
        ['ctPerms', node('read,write')],
        ['ctPermsErr', node()],
        ['ctSpaces', node('')],
        ['ctSpacesHint', node()],
        ['ctExpires', node('0')],
        ['ctEmail', node('')],
        ['ctPendingEscape', node()],
        ['ctStopWaiting', node()],
        ['ctSecret', node()],
        ['ctTokenHash', node()],
        ['ctCopyBtn', node()],
        ['ctCopyHashBtn', node()],
        ['itSpace', node('fresh-space')],
        ['itHash', node('sha256:' + 'a'.repeat(64))],
        ['itErr', node()],
        ['etPerms', node('read,write')],
        ['etEmail', node('')],
        ['etSpacesHint', node()],
        ['etErr', node()],
        ['editTokenPicker', node()],
        ['editTokenPickerErr', node()],
    ]);
    elements.get('adminModal').querySelectorAll = selector => selector === '.ao-space' ? grantBoxes : [];

    const responseFor = async (tool, args) => {
        if (overrides[tool]) return await overrides[tool](args);
        if (tool === 'admin_list_tokens') return { status: 'ok', tokens: [], total: 0 };
        if (tool === 'space_list') return { status: 'ok', spaces: [{ space_id: 'fresh-space' }] };
        if (tool === 'space_invite_token') return { status: 'ok', space_id: args.space_id, added: true };
        if (tool === 'admin_update_token') return { status: 'ok', message: 'Token updated' };
        if (tool === 'admin_revoke_token') return { status: 'ok', message: 'Token revoked' };
        if (tool === 'admin_delete_token') return { status: 'deleted', message: 'Token deleted' };
        if (tool === 'admin_create_token') {
            return {
                status: 'created', name: 'manager-child', token: 'lm_legacy_admin_secret',
                permissions: args.permissions.split(','), space_ids: [],
                info: 'admin scope normalized to empty', scope_normalized: true,
            };
        }
        if (tool === 'token_create') {
            return { status: 'error', message: 'synthetic stop after routing proof' };
        }
        throw new Error(`unexpected tool ${tool}`);
    };

    const context = {
        console,
        URL,
        window: { addEventListener() {} },
        location: { hash: '#/access' },
        navigator: { clipboard: { async writeText(value) { copied.push(value); } } },
        cache,
        document: {
            body: { appendChild() {}, removeChild() {} },
            addEventListener() {},
            getElementById(id) { return elements.get(id) || null; },
            querySelectorAll(selector) { return selector === '.et-space' ? editBoxes : selector === '.ao-space' ? grantBoxes : []; },
            createElement() { return node(); },
            execCommand() { return false; },
        },
        AdminRouter: { epoch: 1, refresh() {} },
        AdminViews: { register(name, fn) { if (name === 'access') render = fn; } },
        _ctx: () => ({ identity }),
        registerAction(name, fn) { actions[name] = fn; },
        callTool: async (tool, args) => {
            calls.push({ tool, args });
            return await responseFor(tool, args);
        },
        showModal(title, body, verb, onConfirm) {
            modal = { title, body, verb, onConfirm };
            elements.get('adminModal').style.display = 'block';
            for (const key of [...elements.keys()]) if (key.startsWith('ao')) elements.delete(key);
            for (const match of body.matchAll(/id="(ao[^" ]+)"/g)) elements.set(match[1], node());
            if (elements.has('aoSpaces')) {
                let html = '';
                Object.defineProperty(elements.get('aoSpaces'), 'innerHTML', {
                    get() { return html; },
                    set(value) {
                        html = value;
                        grantBoxes = [...value.matchAll(/<input\b[^>]*class="ao-space"[^>]*>/g)].map(match => {
                            const box = node(match[0].match(/value="([^"]*)"/)[1]);
                            box.checked = /\bchecked\b/.test(match[0]);
                            box.disabled = /\bdisabled\b/.test(match[0]);
                            return box;
                        });
                    },
                });
            }
        },
        showDestructiveModal() {},
        closeModal() {},
        showToast(kind, message) { toasts.push({ kind, message }); },
        renderIdentityBlock() {},
        esc: escapeHtml,
        icon: () => '',
        pageHeader: (title, actionsHtml = '') => `HEADER:${title}:${actionsHtml}`,
        panel: body => `PANEL:${body}`,
        dataTable: (_headers, rows) => `TABLE:${rows}`,
        pill: (_kind, label) => `PILL:${label}`,
        copyable: (full, shown) => `COPY:${shown}:${full}`,
        monoBlock: value => `MONO:${value}`,
        stateEmpty: options => `EMPTY:${options.title}:${options.hint || ''}:${options.actionHtml || ''}`,
        stateError: options => `ERROR:${options.title || ''}:${options.message || ''}`,
        stateUnavailable: message => `UNAVAILABLE:${message}`,
        stateLoading: message => `LOADING:${message}`,
        serverMessage: message => `SERVER:${escapeHtml(message)}`,
        fmtTimestamp: value => ({ text: String(value), title: String(value) }),
    };
    vm.createContext(context);
    const original = fs.readFileSync(accessPath, 'utf8');
    const instrumented = original.replace(
        "AdminViews.register('access', render);",
        "globalThis.__access = { render, openCreateModal, openInviteModal }; AdminViews.register('access', render);",
    );
    vm.runInContext(instrumented, context, { filename: accessPath });
    vm.runInContext(fs.readFileSync(spacesPath, 'utf8'), context, { filename: spacesPath });
    assert.equal(typeof render, 'function');

    const content = node();
    return {
        actions,
        cache,
        calls,
        content,
        elements,
        editBoxes,
        get grantBoxes() { return grantBoxes; },
        context, copied,
        setIdentity(value) { identity = value; },
        get modal() { return modal; },
        render() { render(content, {}, { epoch: 1, identity }); },
        toasts,
    };
}

async function proveAdminEditPromotionAndDowngradeScopeTransitions() {
    const identity = {
        client_name: 'admin', auth_type: 'token',
        permissions: ['read', 'write', 'manage', 'admin'],
        token_hash: 'sha256:' + '4'.repeat(64),
    };

    const promotedHash = 'sha256:' + '5'.repeat(64);
    const promoted = accessHarness(identity, {
        admin_list_tokens: async () => ({
            status: 'ok',
            tokens: [{ name: 'writer', hash: promotedHash, permissions: ['read', 'write'], space_ids: ['alpha'] }],
            total: 1,
        }),
    });
    promoted.render();
    await flush();
    promoted.elements.get('etPerms').value = 'read,write';
    promoted.actions['access-edit']({
        hash: promotedHash,
    });
    await flush();
    promoted.editBoxes[0].checked = false;
    promoted.editBoxes[1].checked = true;
    promoted.elements.get('etPerms').value = 'read,write,manage,admin';
    promoted.elements.get('etPerms').dispatch('change');
    assert.ok(promoted.editBoxes.every(box => box.disabled), 'promotion must disable every scope control');
    await promoted.modal.onConfirm();
    const promotion = promoted.calls.at(-1);
    assert.equal(promotion.tool, 'admin_update_token');
    assert.equal(promotion.args.permissions, 'read,write,manage,admin');
    assert.equal(Object.hasOwn(promotion.args, 'space_ids_add'), false);
    assert.equal(Object.hasOwn(promotion.args, 'space_ids_remove'), false);

    const downgradedHash = 'sha256:' + '6'.repeat(64);
    const downgraded = accessHarness(identity, {
        admin_list_tokens: async () => ({
            status: 'ok',
            tokens: [{ name: 'former-admin', hash: downgradedHash, permissions: ['read', 'write', 'manage', 'admin'], space_ids: [] }],
            total: 1,
        }),
    });
    downgraded.render();
    await flush();
    downgraded.elements.get('etPerms').value = 'read,write,manage,admin';
    downgraded.actions['access-edit']({
        hash: downgradedHash,
    });
    await flush();
    assert.ok(downgraded.editBoxes.every(box => box.disabled), 'current admin starts with disabled scope controls');
    downgraded.elements.get('etPerms').value = 'read,write';
    downgraded.elements.get('etPerms').dispatch('change');
    assert.ok(downgraded.editBoxes.every(box => !box.disabled), 'downgrade must enable explicit new grants');
    downgraded.editBoxes[1].checked = true;
    await downgraded.modal.onConfirm();
    const downgrade = downgraded.calls.at(-1);
    assert.equal(downgrade.tool, 'admin_update_token');
    assert.equal(downgrade.args.permissions, 'read,write');
    assert.equal(downgrade.args.space_ids_add, 'beta');
    assert.equal(Object.hasOwn(downgrade.args, 'space_ids_remove'), false);
}

async function proveRowMenuTargetsExactTokenAndGuidesReplacement() {
    const firstHash = 'sha256:' + '7'.repeat(64);
    const selectedHash = 'sha256:' + '8'.repeat(64);
    const emptyPermissionsHash = 'sha256:' + '6'.repeat(64);
    const identity = {
        client_name: 'admin', auth_type: 'token',
        permissions: ['read', 'write', 'manage', 'admin'],
        token_hash: 'sha256:' + '4'.repeat(64),
    };
    const h = accessHarness(identity, {
        admin_list_tokens: async () => ({
            status: 'ok',
            tokens: [
                { name: 'duplicate-name', hash: firstHash, permissions: ['read'], space_ids: [] },
                { name: 'duplicate-name', hash: selectedHash, permissions: ['write', 'read'], space_ids: ['alpha'], expires_at: 'malformed' },
                { name: 'empty-profile', hash: emptyPermissionsHash, permissions: [], space_ids: [] },
                { name: 'internal-long', hash: 'sha256:' + '9'.repeat(64), permissions: ['read'], space_ids: [] },
                { name: 'revoked', hash: 'sha256:' + 'a'.repeat(64), permissions: ['read'], space_ids: [], revoked: true },
            ],
            total: 4,
        }),
    });
    h.render();
    await flush();
    const tableHtml = h.elements.get('accessTable').innerHTML;
    assert.match(tableHtml, /row-action-menu/);
    assert.doesNotMatch(h.content.innerHTML, /access-open-edit/);
    assert.match(tableHtml, /Edit token/);
    assert.match(tableHtml, /Create replacement/);
    assert.match(tableHtml, /Revoke token…/);
    assert.match(tableHtml, /Revoked permanently/);
    assert.doesNotMatch(tableHtml, /Reactivate token|Permanent soft revoke/);
    assert.match(tableHtml, /Delete permanently/);

    h.actions['access-edit']({ hash: selectedHash });
    await flush();
    assert.equal(h.modal.title, 'Edit token');
    assert.match(h.modal.body, /<option value="">— no change —<\/option>/);
    assert.doesNotMatch(h.modal.body, /<option[^>]+selected/);

    h.actions['access-replace']({ hash: selectedHash });
    assert.equal(h.modal.title, 'Replace token safely');
    assert.match(h.modal.body, /Create a new token with the same access profile/);
    assert.match(h.modal.body, /Creating a replacement does not revoke the old token/);
    assert.equal(await h.modal.onConfirm(), false);
    assert.equal(h.modal.title, 'Create replacement token');
    assert.match(h.modal.body, /value="duplicate-name-replacement"/);
    assert.match(h.modal.body, /value="write,read" selected/);
    assert.doesNotMatch(h.modal.body, /value="read" selected/);
    assert.match(h.modal.body, /stored permission profile/i);
    assert.match(h.modal.body, /value="alpha"/);
    assert.match(h.modal.body, /id="ctExpires"[^>]*value="1"/);

    h.actions['access-replace']({ hash: emptyPermissionsHash });
    assert.equal(await h.modal.onConfirm(), false);
    assert.equal(h.modal.title, 'Create replacement token');
    assert.match(h.modal.body, /<option value="" selected disabled>Stored permission profile unavailable/);
    assert.match(h.modal.body, /cannot be copied safely/i);
    assert.doesNotMatch(h.modal.body, /value="read,write" selected/);
    h.elements.get('ctPerms').value = '';
    const beforeUnavailableSubmit = h.calls.length;
    assert.equal(await h.modal.onConfirm(), false);
    assert.equal(h.calls.length, beforeUnavailableSubmit, 'unavailable permissions must fail before the network');
    assert.equal(h.elements.get('ctPermsErr').hidden, false);
    assert.match(h.elements.get('ctPermsErr').textContent, /Select a permission profile/);

    h.actions['access-replace']({ hash: 'sha256:' + 'a'.repeat(64) });
    assert.match(h.modal.body, /The old token remains revoked/);
    assert.doesNotMatch(h.modal.body, /old token stays active|revoke or delete/);
    assert.equal(await h.modal.onConfirm(), false);
    assert.match(h.modal.body, /The old token remains revoked/);
    assert.doesNotMatch(h.modal.body, /old token stays active/);

    h.actions['access-delete']({ hash: firstHash });
    assert.equal(h.modal.title, 'Delete token permanently');
    assert.match(h.modal.body, /immediately invalidates/);
    await h.actions['access-delete-do']({ hash: firstHash });
    assert.equal(h.calls.at(-1).tool, 'admin_delete_token');
    assert.equal(h.calls.at(-1).args.token_hash, firstHash);
}

async function proveManagerNeverCallsAdmin() {
    const identity = {
        client_name: 'manager', auth_type: 'token',
        permissions: ['read', 'write', 'manage'], token_hash: 'sha256:' + '1'.repeat(64),
    };
    const h = accessHarness(identity);
    h.render();
    assert.match(h.content.innerHTML, /Scoped delegation/);
    assert.equal(h.cache.tokens.length, 0);
    assert.equal(h.cache.spaces.length, 0);
    assert.deepEqual(h.calls, []);

    h.actions['access-create']();
    await h.modal.onConfirm();
    assert.equal(h.calls.at(-1).tool, 'token_create');
    assert.equal(Object.hasOwn(h.calls.at(-1).args, 'space_ids'), false);

    await h.actions['access-invite']();
    await flush();
    assert.equal(h.calls.at(-1).tool, 'space_list');
    assert.match(h.modal.body, /fresh-space/);
    assert.doesNotMatch(h.modal.body, /stale-admin-space/);
    const beforeInvalidHash = h.calls.length;
    h.elements.get('itHash').value = 'sha256:' + 'A'.repeat(64);
    await h.modal.onConfirm();
    assert.equal(h.calls.length, beforeInvalidHash, 'uppercase target hash must fail before the network');
    h.elements.get('itHash').value = 'a'.repeat(64);
    await h.modal.onConfirm();
    assert.equal(h.calls.length, beforeInvalidHash, 'bare target hash must fail before the network');
    h.elements.get('itHash').value = 'sha256:' + 'a'.repeat(64);
    await h.modal.onConfirm();
    assert.equal(h.calls.at(-1).tool, 'space_invite_token');

    for (const forged of [
        'access-edit', 'access-replace', 'access-revoke', 'access-delete', 'access-purge',
        'access-revoke-do', 'access-delete-do',
    ]) {
        h.actions[forged]({ hash: 'sha256:' + '2'.repeat(64), mode: 'all' });
    }
    assert.equal(h.calls.some(call => call.tool.startsWith('admin_')), false);
}

async function proveAdminAndBootstrapKeepLegacyCreate() {
    for (const identity of [
        { client_name: 'admin', auth_type: 'token', permissions: ['read', 'write', 'manage', 'admin'] },
        { client_name: 'bootstrap', auth_type: 'bootstrap', permissions: ['read', 'write', 'manage', 'admin'] },
    ]) {
        const h = accessHarness(identity);
        h.render();
        await flush();
        assert.equal(h.calls[0].tool, 'admin_list_tokens');
        h.actions['access-create']();
        assert.equal(h.elements.get('ctSpaces').disabled, false);
        h.elements.get('ctPerms').value = 'read,write,manage,admin';
        h.elements.get('ctPerms').dispatch('change');
        assert.equal(h.elements.get('ctSpaces').disabled, true);
        assert.match(h.elements.get('ctSpacesHint').textContent, /space_ids: \[\]/);
        h.elements.get('ctSpaces').value = 'must-not-be-sent';
        await h.modal.onConfirm();
        assert.equal(h.calls.at(-1).tool, 'admin_create_token');
        assert.equal(Object.hasOwn(h.calls.at(-1).args, 'space_ids'), false);
        assert.equal(h.calls.some(call => call.tool === 'token_create'), false);
        assert.match(h.modal.body, /lm_legacy_admin_secret/);
        assert.match(h.modal.body, /Admin access is global; the space allowlist was ignored/);
    }
}

async function provePartialCredentialIsNeverHidden() {
    const token = 'lm_partial_secret';
    const tokenHash = 'sha256:' + 'c'.repeat(64);
    const identity = {
        client_name: 'manager', auth_type: 'token',
        permissions: ['read', 'write', 'manage'], token_hash: 'sha256:' + '3'.repeat(64),
    };
    const h = accessHarness(identity, {
        token_create: async () => ({
            status: 'partial', recovery_required: true,
            name: 'manager-child', token, token_hash: tokenHash,
            permissions: ['read', 'write'],
            message: 'registry persistence outcome is ambiguous',
        }),
    });
    h.render();
    h.actions['access-create']();
    await h.modal.onConfirm();
    assert.match(h.modal.title, /Creation needs checking/);
    assert.match(h.modal.body, new RegExp(token));
    assert.match(h.modal.body, new RegExp(tokenHash));
    assert.match(h.modal.body, /Do not discard either value/);
    assert.match(h.modal.body, /Do not assume the token is active or absent/);
    await h.modal.onConfirm();
    assert.equal(h.calls.some(call => call.tool === 'space_list' || call.tool === 'space_invite_token'), false,
        'partial credential must never proceed to grants');
}

function spacesHarness(identity, overrides = {}) {
    let render;
    let modalCount = 0;
    let modal = null;
    const actions = {};
    const calls = [];
    const toasts = [];
    const elements = new Map([
        ['spacesToolbar', node()],
        ['spacesTableWrap', node()],
        ['spacesRefreshBtn', node()],
        ['csSpaceId', node('project-a')],
        ['csDescription', node('Exact description')],
        ['csOwner', node('owner@example.test')],
        ['csOwnerList', node()],
        ['csRules', node('# Exact rules')],
        ['csRulesCount', node()],
        ['csSpaceIdError', node()],
        ['csFormError', node()],
    ]);
    const confirmButton = node();
    confirmButton.textContent = 'Create';
    confirmButton.replaceWith = replacement => {
        confirmButton.isConnected = false;
        elements.set('modalConfirmBtn', replacement);
    };
    elements.set('modalConfirmBtn', confirmButton);
    const context = {
        console,
        cache: { spaces: [], tokens: [] },
        document: {
            getElementById(id) { return elements.get(id) || null; },
            querySelectorAll() { return []; },
        },
        AdminRouter: { epoch: 1, refresh() {} },
        AdminViews: { register(name, fn) { assert.equal(name, 'spaces'); render = fn; } },
        _ctx: () => ({ identity }),
        currentSessionGeneration: () => 1,
        sessionGenerationIsCurrent: generation => generation === 1,
        registerAction(name, fn) { actions[name] = fn; },
        callTool: async (tool, args) => {
            calls.push({ tool, args });
            if (overrides[tool]) return await overrides[tool](args);
            if (tool === 'space_list') return { status: 'ok', spaces: [], total: 0 };
            if (tool === 'bank_consolidation_queues') return { status: 'ok', lanes: [] };
            if (tool === 'admin_list_tokens') throw new Error('manager/writer must not probe admin');
            return { status: 'error' };
        },
        showModal(title, body, verb, onConfirm) {
            modalCount += 1;
            modal = { title, body, verb, onConfirm };
        },
        showToast(kind, message) { toasts.push({ kind, message }); },
        esc: escapeHtml,
        icon: () => '',
        truncateMiddle: value => String(value),
        fmtTimestamp: value => ({ text: String(value), title: String(value) }),
        statusDot: (_kind, label) => `DOT:${label}`,
        dataTable: (_headers, rows) => `TABLE:${rows}`,
        pageHeader: (title, actionsHtml = '') => `HEADER:${title}:${actionsHtml}`,
        stateEmpty: options => `EMPTY:${options.title}:${options.hint || ''}:${options.actionHtml || ''}`,
        stateError: options => `ERROR:${options.title || ''}`,
        stateLoading: message => `LOADING:${message}`,
        serverMessage: message => `SERVER:${message}`,
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(spacesPath, 'utf8'), context, { filename: spacesPath });
    const content = node();
    return {
        actions, calls, content, elements, toasts,
        get modal() { return modal; },
        get modalCount() { return modalCount; },
        render() { render(content, {}, { epoch: 1, identity }); },
    };
}

async function proveSpaceCreateGate() {
    const writer = spacesHarness({ auth_type: 'token', permissions: ['read', 'write'] });
    writer.render();
    assert.doesNotMatch(writer.content.innerHTML, /Create space/);
    await flush();
    assert.doesNotMatch(writer.elements.get('spacesTableWrap').innerHTML, /data-action="spaces-open-create"/);
    writer.actions['spaces-open-create']();
    assert.equal(writer.modalCount, 0);

    const manager = spacesHarness({ auth_type: 'token', permissions: ['read', 'write', 'manage'] });
    manager.render();
    assert.match(manager.content.innerHTML, /Create space/);
    await flush();
    assert.match(manager.elements.get('spacesTableWrap').innerHTML, /data-action="spaces-open-create"/);
    manager.actions['spaces-open-create']();
    assert.equal(manager.modalCount, 1);
    assert.equal(manager.calls.some(call => call.tool.startsWith('admin_')), false);
}

async function proveSpacePartialKeepsExactValuesAndNeverSucceeds() {
    const recoveryAction = 'Inspect <project-a/> exactly & do not delete.';
    const partial = {
        status: 'partial',
        space_id: 'project-a',
        recovery_required: true,
        message: 'commit marker not confirmed',
        recovery: { retry_safe: false, action: recoveryAction },
    };
    const manager = spacesHarness(
        { auth_type: 'token', permissions: ['read', 'write', 'manage'] },
        { space_create: async () => partial },
    );
    manager.render();
    await flush();
    manager.actions['spaces-open-create']();
    const before = {
        spaceId: manager.elements.get('csSpaceId').value,
        description: manager.elements.get('csDescription').value,
        owner: manager.elements.get('csOwner').value,
        rules: manager.elements.get('csRules').value,
    };
    const originalConfirm = manager.elements.get('modalConfirmBtn');
    const callsBeforeConfirm = manager.calls.length;

    const confirmed = await manager.modal.onConfirm();

    assert.equal(confirmed, false, 'partial must keep the create dialog open');
    assert.equal(manager.calls.length, callsBeforeConfirm + 1, 'unsafe partial must never auto-retry');
    const createCall = manager.calls.at(-1);
    assert.equal(createCall.tool, 'space_create');
    assert.equal(createCall.args.space_id, before.spaceId);
    assert.equal(createCall.args.description, before.description);
    assert.equal(createCall.args.owner, before.owner);
    assert.equal(createCall.args.rules, before.rules);
    assert.equal(manager.elements.get('csSpaceId').value, before.spaceId);
    assert.equal(manager.elements.get('csDescription').value, before.description);
    assert.equal(manager.elements.get('csOwner').value, before.owner);
    assert.equal(manager.elements.get('csRules').value, before.rules);
    const recovery = manager.elements.get('csFormError');
    assert.equal(recovery.hidden, false);
    assert.equal(recovery.attributes['data-recovery-required'], 'true');
    assert.match(recovery.innerHTML, /recovery\.retry_safe:<\/strong> <code>false<\/code>/);
    assert.match(recovery.innerHTML, /recovery\.action:<\/strong>/);
    assert.ok(recovery.innerHTML.includes(escapeHtml(recoveryAction)));
    assert.ok(recovery.innerHTML.includes('commit marker not confirmed'));
    assert.ok(recovery.innerHTML.includes('Admin recovery required'));
    const lockedConfirm = manager.elements.get('modalConfirmBtn');
    assert.notEqual(lockedConfirm, originalConfirm, 'unsafe retry must detach the shell-owned button');
    assert.equal(originalConfirm.isConnected, false);
    assert.equal(lockedConfirm.disabled, true);
    assert.equal(lockedConfirm.textContent, 'Admin recovery required');
    assert.equal(lockedConfirm.attributes['aria-disabled'], 'true');
    assert.equal(manager.toasts.some(toast => toast.kind === 'ok'), false);

    await flush();
    assert.equal(manager.calls.length, callsBeforeConfirm + 1, 'unsafe partial must stay idle');
}

async function proveRetrySafeSpacePartialAllowsOnlyManualIdenticalRetry() {
    const partial = {
        status: 'partial',
        space_id: 'project-a',
        recovery_required: true,
        message: 'commit marker not confirmed',
        recovery: { retry_safe: true, action: 'retry the identical request' },
    };
    const manager = spacesHarness(
        { auth_type: 'token', permissions: ['read', 'write', 'manage'] },
        { space_create: async () => partial },
    );
    manager.render();
    await flush();
    manager.actions['spaces-open-create']();
    const originalConfirm = manager.elements.get('modalConfirmBtn');
    const callsBeforeConfirm = manager.calls.length;

    assert.equal(await manager.modal.onConfirm(), false);
    assert.equal(manager.calls.length, callsBeforeConfirm + 1);
    const firstRetryArgs = manager.calls.at(-1).args;
    assert.equal(manager.elements.get('modalConfirmBtn'), originalConfirm);
    assert.equal(originalConfirm.disabled, false);
    assert.equal(originalConfirm.textContent, 'Create');
    assert.ok(manager.elements.get('csFormError').innerHTML.includes('Identical manual retry is permitted'));
    await flush();
    assert.equal(manager.calls.length, callsBeforeConfirm + 1, 'safe partial must not auto-retry');

    assert.equal(await manager.modal.onConfirm(), false, 'operator may explicitly retry the same values');
    assert.equal(manager.calls.length, callsBeforeConfirm + 2);
    const secondRetryArgs = manager.calls.at(-1).args;
    for (const field of ['space_id', 'description', 'owner', 'rules']) {
        assert.equal(secondRetryArgs[field], firstRetryArgs[field], `manual retry changed ${field}`);
    }
}

async function proveOnboardingGrantsAndClientConfiguration() {
    for (const admin of [false, true]) {
        const hash = 'sha256:' + 'c'.repeat(64);
        const secret = 'lm_research_fixture_never_in_config';
        const grants = [];
        const results = [
            { status: 'ok', added: true, mode: 'delta', space_ids_added: ['alpha'], space_ids_after: ['alpha'] },
            { status: 'error', message: 'Temporary failure' },
            { status: 'partial', recovery_required: true, message: 'Inspect exact target' },
            { status: 'ok', added: false, mode: 'delta', space_ids_noop: ['add:beta'], space_ids_after: ['alpha', 'beta'] },
        ];
        const create = async () => ({ status: 'created', token: secret, token_hash: hash,
            name: 'agent', permissions: ['read', 'write'], space_ids: [] });
        const grant = async args => {
            grants.push(JSON.parse(JSON.stringify(args)));
            const response = results.shift();
            if (admin && response.status === 'error') throw new Error('Lost response');
            return response;
        };
        const h = accessHarness({ client_name: 'owner', permissions: admin ? ['admin'] : ['read', 'write', 'manage'] }, {
            admin_create_token: create, token_create: create,
            space_list: async () => ({ status: 'ok', spaces: ['alpha', 'beta', 'gamma'].map(space_id => ({ space_id })) }),
            admin_update_token: grant, space_invite_token: grant,
        });
        assert.equal(typeof h.context.window.openAccessOnboarding, 'function', 'shared entry point must exist');
        h.context.window.openAccessOnboarding({ spaceId: 'beta', message: 'Space created' });
        assert.equal(h.calls.length, 0, 'opening handoff must never create a token automatically');
        await h.modal.onConfirm();
        assert.equal(h.calls.length, 1, 'create only once before acknowledgement');
        assert.match(h.modal.body, /Copy token/);
        await h.modal.onConfirm();
        assert.equal(h.elements.get('ctSecret').textContent, '', 'plaintext cleared before grants');
        assert.equal(h.elements.get('ctTokenHash').textContent, '', 'secret screen hash cleared');
        assert.equal(h.modal.title, 'Grant space access');
        assert.match(h.modal.body, new RegExp(hash), 'non-secret full Token ID remains visible during grants');
        await h.elements.get('aoCopyHashBtn').dispatch('click');
        await flush();
        assert.equal(h.copied.at(-1), hash, 'operator can copy the Token ID after acknowledging the plaintext');
        assert.equal(h.grantBoxes.find(box => box.value === 'beta').checked, true);
        for (const box of h.grantBoxes) box.checked = true;
        await h.elements.get('aoGrant').dispatch('click');
        assert.equal(grants.length, 3);
        assert.match(h.elements.get('aoResults').innerHTML, /Granted/);
        assert.match(h.elements.get('aoResults').innerHTML, /Needs checking/);
        assert.match(h.elements.get('aoResults').innerHTML,
            admin ? /Not confirmed — retry available/ : /Refused by server — retry available/,
            'explicit server refusal and lost transport response have distinct labels');
        await h.elements.get('aoRetry').dispatch('click');
        assert.equal(grants.length, 4, 'retry excludes success and recovery-required');
        assert.deepEqual(grants[3], admin ? { token_hash: hash, space_ids_add: 'beta' } : { token_hash: hash, space_id: 'beta' });
        assert.ok(grants.every(args => args.token_hash === hash), 'no prefix or name as identity');
        assert.ok(grants.every(args => !('permissions' in args || 'space_ids' in args || 'space_ids_remove' in args)));
        assert.equal(h.calls.filter(call => call.tool.endsWith('create_token') || call.tool === 'token_create').length, 1);
        if (!admin) assert.equal(h.calls.some(call => call.tool.startsWith('admin_')), false);
        await h.modal.onConfirm();
        assert.equal(h.modal.title, 'Configure client');
        assert.equal(h.elements.get('aoUrl').value, '', 'external URL is never inferred');
        h.elements.get('aoUrl').value = 'https://external.example/proxy/hivemind/mcp';
        h.elements.get('aoClient').value = 'codex';
        await h.elements.get('aoCopyConfig').dispatch('click');
        assert.match(h.copied.at(-1), /bearer_token_env_var = "HIVEMIND_TOKEN"/);
        assert.match(h.copied.at(-1), /https:\/\/external.example\/proxy\/hivemind\/mcp/);
        assert.ok(!h.copied.at(-1).includes(secret));
        h.elements.get('aoClient').value = 'claude';
        await h.elements.get('aoCopyConfig').dispatch('click');
        assert.equal(JSON.parse(h.copied.at(-1)).mcpServers.hivemind.headers.Authorization, 'Bearer ${HIVEMIND_TOKEN}');
        const copies = h.copied.length;
        h.elements.get('aoUrl').value = 'https://user:password@external.example/mcp';
        await h.elements.get('aoCopyConfig').dispatch('click');
        assert.equal(h.copied.length, copies, 'credentials in URL must not reach config');
        assert.match(h.elements.get('aoConfigError').textContent, /HTTP|credentials/);
        h.setIdentity({ client_name: 'another', permissions: ['admin'] });
        h.elements.get('aoUrl').value = 'https://external.example/mcp';
        await h.elements.get('aoCopyConfig').dispatch('click');
        assert.equal(h.copied.length, copies, 'stale-session config cannot be copied');
    }
}

async function proveOnboardingOwnershipAndSingleFlight() {
    const hash = 'sha256:' + 'd'.repeat(64);
    for (const boundary of ['session', 'close', 'route', 'modal']) {
        let finish;
        const gate = new Promise(resolve => { finish = resolve; });
        const h = accessHarness({ permissions: ['manage'] }, {
            token_create: async () => ({ status: 'created', token: 'lm_once', token_hash: hash, permissions: ['read'] }),
            space_list: async () => ({ status: 'ok', spaces: [{ space_id: 'alpha' }, { space_id: 'beta' }] }),
            space_invite_token: async () => await gate,
        });
        h.context.window.openAccessOnboarding({});
        await h.modal.onConfirm();
        await h.modal.onConfirm();
        h.grantBoxes.forEach(box => { box.checked = true; });
        // A forged checkbox from outside the fresh list must not enter the batch.
        h.grantBoxes.push(Object.assign(node('forged'), { checked: true }));
        const request = h.elements.get('aoGrant').dispatch('click');
        await h.elements.get('aoGrant').dispatch('click');
        assert.equal(h.calls.filter(call => call.tool === 'space_invite_token').length, 1, 'single flight');
        assert.equal(await h.modal.onConfirm(), false, 'cannot leave for config while submitting');
        assert.equal(h.modal.title, 'Grant space access');
        if (boundary === 'session') h.setIdentity({ client_name: 'other', permissions: ['manage'] });
        if (boundary === 'close') { h.context.window.clearAccessHandoff(); h.elements.get('adminModal').style.display = 'none'; }
        if (boundary === 'route') h.context.AdminRouter.epoch += 1;
        if (boundary === 'modal') h.elements.set('aoResults', node());
        const before = h.elements.get('aoResults').innerHTML;
        finish({ status: 'ok', added: true });
        await request;
        assert.equal(h.calls.filter(call => call.tool === 'space_invite_token').length, 1, `${boundary}: no next grant`);
        assert.equal(h.elements.get('aoResults').innerHTML, before, `${boundary}: no stale painting`);
    }
}

async function proveOnboardingInitialScopesAndSpaceEntry() {
    const hash = 'sha256:' + 'e'.repeat(64);
    const h = accessHarness({ permissions: ['manage'] }, {
        space_create: async args => ({ status: 'created', space_id: args.space_id, token_message: 'Owner <saved> & ready' }),
        token_create: async () => ({ status: 'created', token: 'lm_once', token_hash: hash, permissions: ['read'], space_ids: ['alpha'] }),
        space_list: async () => ({ status: 'ok', spaces: [{ space_id: 'alpha' }, { space_id: 'beta' }] }),
    });
    for (const [id, value] of Object.entries({ csSpaceId: 'beta', csDescription: '', csOwner: '', csRules: '',
        csRulesCount: '', csSpaceIdError: '', csFormError: '' })) h.elements.set(id, node(value));
    h.actions['spaces-open-create']();
    await h.modal.onConfirm();
    assert.equal(h.modal.title, 'Create token', 'Space-created opens the actual shared form');
    assert.match(h.modal.body, /Owner &lt;saved&gt; &amp; ready/);
    assert.deepEqual(h.calls.map(call => call.tool), ['space_create'], 'no implicit token creation');
    await h.modal.onConfirm();
    await h.modal.onConfirm();
    assert.equal(h.grantBoxes.find(box => box.value === 'alpha').disabled, true, 'initial grant retained');
    assert.equal(h.grantBoxes.find(box => box.value === 'beta').checked, true, 'new space preselected');
    h.grantBoxes.push(Object.assign(node('forged'), { checked: true }));
    await h.elements.get('aoGrant').dispatch('click');
    const grants = h.calls.filter(call => call.tool === 'space_invite_token');
    assert.equal(grants.length, 1);
    assert.equal(grants[0].args.space_id, 'beta');

    const admin = accessHarness({ permissions: ['admin'] }, {
        admin_create_token: async () => ({ status: 'created', token: 'lm_admin', token_hash: hash, permissions: ['admin'], space_ids: [] }),
    });
    admin.actions['access-create']();
    await admin.modal.onConfirm();
    await admin.modal.onConfirm();
    assert.equal(admin.modal.title, 'Configure client');
    assert.match(admin.modal.body, /global access/);
    assert.equal(admin.calls.length, 1, 'global admin target skips spaces and grants');
}

await proveOnboardingGrantsAndClientConfiguration();
await proveOnboardingOwnershipAndSingleFlight();
await proveOnboardingInitialScopesAndSpaceEntry();
await proveManagerNeverCallsAdmin();
await proveAdminAndBootstrapKeepLegacyCreate();
await proveAdminEditPromotionAndDowngradeScopeTransitions();
await proveRowMenuTargetsExactTokenAndGuidesReplacement();
await provePartialCredentialIsNeverHidden();
await proveSpaceCreateGate();
await proveSpacePartialKeepsExactValuesAndNeverSucceeds();
await proveRetrySafeSpacePartialAllowsOnlyManualIdenticalRetry();
console.log('admin access roles runtime: ok');
