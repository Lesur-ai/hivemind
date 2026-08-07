import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const viewPath = process.argv[2];
assert.ok(viewPath, 'spaces view path is required');

function element(value = '') {
    const attributes = {};
    return {
        value,
        hidden: true,
        textContent: '',
        innerHTML: '',
        attributes,
        setAttribute(name, next) { attributes[name] = String(next); },
        removeAttribute(name) { delete attributes[name]; },
    };
}

const nodes = {
    csSpaceId: element('reused-space'),
    csDescription: element('restored description'),
    csOwner: element('owner@example.test'),
    csRules: element('# Rules'),
    csSpaceIdError: element(),
    csFormError: element(),
};
const calls = [];
const action = 'Inspect then retry space_delete(recover_access_grants=True).';
const partial = {
    status: 'partial',
    recovery_required: true,
    message: 'A previous deletion left access grants behind.',
    recovery: { retry_safe: true, action },
};
const identity = {
    auth_type: 'token',
    permissions: ['read', 'write', 'manage'],
};

const context = {
    console,
    document: {
        getElementById(id) { return nodes[id] || null; },
        querySelector() { return null; },
    },
    _ctx: () => ({ identity }),
    AdminRouter: { epoch: 17, refresh() {} },
    AdminViews: { register() {} },
    registerAction() {},
    callTool: async (tool, args) => {
        calls.push({ tool, args });
        return partial;
    },
    esc: value => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;'),
    icon: () => '',
};

vm.createContext(context);
const original = fs.readFileSync(viewPath, 'utf8');
const instrumented = original.replace(
    "AdminViews.register('spaces', render);",
    'globalThis.__submitCreateSpace = _submitCreateSpace;',
);
vm.runInContext(instrumented, context, { filename: viewPath });
assert.equal(typeof context.__submitCreateSpace, 'function');

const outcome = await context.__submitCreateSpace();
assert.equal(outcome, false);
assert.equal(calls.length, 1);
assert.equal(calls[0].tool, 'space_create');
assert.equal(JSON.stringify(calls[0].args), JSON.stringify({
    space_id: 'reused-space',
    description: 'restored description',
    owner: 'owner@example.test',
    rules: '# Rules',
}));
assert.equal(nodes.csFormError.hidden, false);
assert.equal(nodes.csFormError.attributes['data-recovery-required'], 'true');
assert.ok(nodes.csFormError.innerHTML.includes('Grant-recovery retry is MCP/CLI-only'));
assert.ok(nodes.csFormError.innerHTML.includes('This console never sends recover_access_grants'));
assert.ok(nodes.csFormError.innerHTML.includes(action));

console.log('admin space create recovery runtime: ok');
