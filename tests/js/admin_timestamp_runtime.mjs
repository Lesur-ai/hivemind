import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const appPath = process.argv[2];
assert.ok(appPath, 'admin-app.js path is required');
assert.equal(process.env.TZ, 'Europe/Paris', 'test requires TZ=Europe/Paris');

const source = fs.readFileSync(appPath, 'utf8');
const start = source.indexOf('function _pad2');
const end = source.indexOf('function truncateMiddle');
assert.ok(start >= 0 && end > start, 'timestamp helpers not found');

const context = { Date, Intl };
vm.createContext(context);
vm.runInContext(`${source.slice(start, end)}\nthis.fmtTimestamp = fmtTimestamp;`, context);

assert.deepEqual(
    JSON.parse(JSON.stringify(context.fmtTimestamp('2026-01-15T12:00:00Z'))),
    {
        text: '2026-01-15 13:00',
        title: '2026-01-15T12:00:00Z · Europe/Paris (UTC+01:00)',
        zone: 'UTC+01:00',
    },
);
assert.equal(context.fmtTimestamp('2026-07-15T12:00:00+00:00').text, '2026-07-15 14:00');
assert.equal(context.fmtTimestamp('2026-07-15T12:00:00+00:00').zone, 'UTC+02:00');
assert.equal(context.fmtTimestamp('2026-01-15T23:30:00').text, '2026-01-16 00:30');
assert.equal(context.fmtTimestamp('2026-01-15T12:00:00-05:00').text, '2026-01-15 18:00');
assert.equal(context.fmtTimestamp('2026-01-15T12-00-00').text, '2026-01-15 13:00');
assert.equal(context.fmtTimestamp(`2026-01-15T12-00-00-${'a'.repeat(32)}`).text, '2026-01-15 13:00');
assert.equal(context.fmtTimestamp('20260115T120000').text, '2026-01-15 13:00');
assert.deepEqual(
    JSON.parse(JSON.stringify(context.fmtTimestamp('2026-01-15'))),
    { text: '2026-01-15', title: '2026-01-15', zone: '' },
);
assert.deepEqual(
    JSON.parse(JSON.stringify(context.fmtTimestamp('not-a-timestamp'))),
    { text: 'not-a-timestamp', title: 'not-a-timestamp', zone: '' },
);

console.log('admin timestamp runtime assertions passed');
