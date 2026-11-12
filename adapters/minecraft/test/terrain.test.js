'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { bestTool } = require('../lib/terrain');

test('bestTool prefers stone over wood and diamond over stone', () => {
  const wooden = {
    name: 'wooden_pickaxe',
    maxDurability: 59,
    durabilityUsed: 0,
  };
  const stone = {
    name: 'stone_pickaxe',
    maxDurability: 131,
    durabilityUsed: 20,
  };
  const diamond = {
    name: 'diamond_pickaxe',
    maxDurability: 1561,
    durabilityUsed: 800,
  };
  assert.equal(bestTool([wooden, stone], 'pickaxe').name, 'stone_pickaxe');
  assert.equal(
    bestTool([stone, diamond, wooden], 'pickaxe').name,
    'diamond_pickaxe'
  );
});

test('bestTool matches shovels for dirt work', () => {
  const shovel = {
    name: 'iron_shovel',
    maxDurability: 250,
    durabilityUsed: 0,
  };
  assert.equal(bestTool([shovel], 'shovel').name, 'iron_shovel');
  assert.equal(bestTool([], 'shovel'), null);
});
