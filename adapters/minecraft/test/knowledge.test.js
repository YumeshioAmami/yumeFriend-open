'use strict';

const test = require('node:test');
const assert = require('node:assert');
const minecraftData = require('minecraft-data');

const { recipeFor, toolTierFor } = require('../lib/knowledge');

const mcData = minecraftData('1.20.1');

test('recipeFor reads real minecraft-data recipes', () => {
  const result = recipeFor(mcData, 'oak_planks');
  assert.equal(result.ok, true);
  assert.equal(result.item, 'oak_planks');
  assert.ok(result.recipes.length >= 1);
  assert.ok(result.recipes[0].ingredients.some((item) => item.item.includes('oak_log')));
  assert.equal(result.source, 'minecraft-data');
});

test('toolTierFor reports the real minimum pickaxe tier', () => {
  const stone = toolTierFor(mcData, 'stone');
  assert.equal(stone.ok, true);
  assert.equal(stone.minimum_tool, 'wooden_pickaxe');
  assert.equal(stone.minimum_tier, 1);

  const diamond = toolTierFor(mcData, 'diamond_ore');
  assert.equal(diamond.ok, true);
  assert.equal(diamond.minimum_tool, 'iron_pickaxe');
  assert.equal(diamond.minimum_tier, 4);
});

test('unknown facts fail honestly', () => {
  assert.equal(recipeFor(mcData, 'not_a_real_item').ok, false);
  assert.equal(toolTierFor(mcData, 'not_a_real_block').ok, false);
});
