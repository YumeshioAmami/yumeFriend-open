'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { pickBlockName, pickAttackTarget } = require('../lib/args');

test('pickBlockName accepts common aliases the model may use', () => {
  assert.equal(pickBlockName({ block: 'dirt' }), 'dirt');
  assert.equal(pickBlockName({ target: 'iron_ore' }), 'iron_ore');
  assert.equal(pickBlockName({ name: 'sand' }), 'sand');
  assert.equal(pickBlockName({}), '');
  assert.equal(pickBlockName(null), '');
});

test('pickAttackTarget accepts target / name / mob', () => {
  assert.equal(pickAttackTarget({ target: 'zombie' }), 'zombie');
  assert.equal(pickAttackTarget({ name: 'creeper' }), 'creeper');
  assert.equal(pickAttackTarget({ mob: 'skeleton' }), 'skeleton');
  assert.equal(pickAttackTarget({}), '');
});
