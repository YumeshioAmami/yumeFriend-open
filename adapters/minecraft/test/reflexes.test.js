'use strict';

const test = require('node:test');
const assert = require('node:assert');

const {
  isHostile,
  shouldEat,
  isCloseEnough,
  hasMoved,
  normalizeReflexConfig,
  orderedModes,
  DEFAULT_MODES,
} = require('../lib/reflexes');

test('isHostile recognizes namespaced and bare mob names', () => {
  assert.equal(isHostile('zombie'), true);
  assert.equal(isHostile('minecraft:creeper'), true);
  assert.equal(isHostile('cow'), false);
  assert.equal(isHostile(''), false);
});

test('shouldEat only when hurt and full enough', () => {
  assert.equal(shouldEat(10, 18), true);
  assert.equal(shouldEat(10, 12), false);
  assert.equal(shouldEat(20, 20), false);
});

test('isCloseEnough uses the default 3.5 block limit', () => {
  assert.equal(isCloseEnough(3.5), true);
  assert.equal(isCloseEnough(3.6), false);
});

test('hasMoved tolerates tiny jitter', () => {
  assert.equal(hasMoved({ x: 0, y: 0, z: 0 }, { x: 0.1, y: 0, z: 0 }), false);
  assert.equal(hasMoved({ x: 0, y: 0, z: 0 }, { x: 1, y: 0, z: 0 }), true);
  assert.equal(hasMoved(null, { x: 1, y: 0, z: 0 }), true);
});

test('reflex config normalizes and orders by priority', () => {
  const config = normalizeReflexConfig({
    modes: {
      self_preservation: { enabled: true, priority: 200 },
      unstuck: { enabled: true, priority: 10 },
    },
  });
  assert.equal(config.modes.self_preservation.priority, 200);
  assert.equal(config.modes.unstuck, undefined);
  assert.deepEqual(
    orderedModes(config).map(([name]) => name),
    ['self_preservation']
  );
});

test('decision reflexes no longer exist', () => {
  assert.equal(DEFAULT_MODES.item_collecting, undefined);
  assert.equal(DEFAULT_MODES.self_defense, undefined);
  assert.equal(DEFAULT_MODES.auto_eat, undefined);
  assert.equal(DEFAULT_MODES.torch_placing, undefined);
  assert.equal(DEFAULT_MODES.unstuck, undefined);
  assert.equal(
    orderedModes(normalizeReflexConfig({})).some(([name]) =>
      ['item_collecting', 'self_defense', 'auto_eat', 'torch_placing'].includes(name)
    ),
    false
  );
});
