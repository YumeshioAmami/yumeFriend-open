'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { resolveVersionName, protocolsFor, supportedVersions } = require('../lib/version');

test('protocol number maps back to the version name (D252)', () => {
  const protocols = protocolsFor('1.20.4');
  assert.ok(protocols.length >= 1, '1.20.4 should have a protocol number');
  assert.equal(resolveVersionName(protocols[0], 'fallback'), '1.20.4');
});

test('unknown protocol falls back to the server-provided name', () => {
  assert.equal(resolveVersionName(-1, 'surprise-version'), 'surprise-version');
});

test('supportedVersions only lists versions that have local data', () => {
  const mineflayer = require('mineflayer');
  const ours = supportedVersions([...mineflayer.testedVersions]);
  assert.ok(ours.includes('1.20.1'));
  assert.ok(ours.length >= 1);
  assert.ok(ours.every((version) => mineflayer.testedVersions.includes(version)));
});
