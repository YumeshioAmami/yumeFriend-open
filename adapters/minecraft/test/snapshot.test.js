'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const Vec3 = require('vec3');

const { state, snapshot } = require('../index.js');

function fakeBot() {
  const position = new Vec3(1.5, 64, 2.5);
  const dirt = {
    slot: 36,
    name: 'dirt',
    count: 12,
    maxDurability: null,
    durabilityUsed: null,
    stackable: 64,
  };
  const pickaxe = {
    slot: 37,
    name: 'diamond_pickaxe',
    count: 1,
    maxDurability: 1561,
    durabilityUsed: 100,
    stackable: 1,
  };
  return {
    version: '1.20.4',
    username: 'Xiyin_',
    health: 20,
    food: 18,
    oxygenLevel: 20,
    entity: {
      position,
      yaw: 0,
      pitch: 0,
      isInWater: false,
      isInLava: false,
      isInFire: false,
      fallDistance: 0,
    },
    inventory: {
      items: () => [dirt, pickaxe],
      emptySlotCount: () => 20,
      slots: { 45: null },
    },
    heldItem: dirt,
    players: {},
    entities: {},
    game: { gameMode: 'survival', dimension: 'minecraft:overworld' },
    time: { timeOfDay: 1000, isDay: true },
    isRaining: false,
    thunderState: 0,
    blockAt: (point) => {
      const x = Math.floor(point.x);
      const y = Math.floor(point.y);
      const z = Math.floor(point.z);
      if (x === 5 && z === 2) {
        if (y === 62) return { name: 'water', boundingBox: 'empty', position: point };
        if (y === 63) return null;
      }
      if (x === -4 && y === 63 && z === 0) {
        return { name: 'oak_log', boundingBox: 'block', position: point };
      }
      if (y === 63 && x >= -6 && x <= 7 && z >= -2 && z <= 3) {
        return { name: 'grass_block', boundingBox: 'block', position: point };
      }
      return null;
    },
    blockAtEntityCursor: () => null,
  };
}

test('snapshot exposes the full inventory without throwing (D330)', () => {
  state.bot = fakeBot();
  state.status = 'running';
  state.followTarget = null;
  state.mode = 'follow';
  state.tasks = new Map();
  try {
    const view = snapshot();
    assert.equal(view.inventory_details.items.length, 2);
    assert.equal(view.inventory_by_item.find((item) => item.item === 'dirt').count, 12);
    assert.equal(view.inventory_tools[0].name, 'diamond_pickaxe');
    assert.equal(view.inventory_tools[0].durability, 1461);
    assert.equal(view.equipment.held.name, 'dirt');
    assert.equal(view.held.item, 'dirt');
    assert.equal(view.inventory_empty_slots, 20);
    assert.equal(view.inventory_details.full, false);
    assert.equal(view.world.near.rows.length, 17);
    assert.equal(view.world.near.nearest_water.direction, '东');
    assert.equal(view.world.near.nearest_tree.x, -4);
    assert.equal(view.tool_guidance.pickaxe, 'diamond_pickaxe');
    assert.equal(view.tool_guidance.shovel, null);
  } finally {
    state.bot = null;
  }
});
