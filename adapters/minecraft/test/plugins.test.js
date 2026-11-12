'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const minecraftData = require('minecraft-data');

const {
  loadPlugins,
  describePlugins,
  callPlugin,
} = require('../plugins/loader');

const mcData = minecraftData('1.20.1');
const plugins = loadPlugins({
  root: path.join(__dirname, '..', 'plugins'),
  mcData,
});

function fakeBot() {
  const calls = { toss: [], activated: 0, deactivated: 0 };
  const inventory = [
    { name: 'torch', type: 50, count: 8 },
    { name: 'apple', type: 260, count: 2 },
  ];
  const bot = {
    inventory: {
      items: () => inventory,
      slots: inventory,
    },
    heldItem: inventory[0],
    activateItem: () => {
      calls.activated += 1;
    },
    deactivateItem: () => {
      calls.deactivated += 1;
    },
    toss: async (...args) => {
      calls.toss.push(args);
    },
  };
  return { bot, calls };
}

test('loader lists available example and safely skips missing mod', () => {
  const described = describePlugins(plugins);
  const inventory = described.find((item) => item.id === 'inventory_bridge');
  const missing = described.find((item) => item.id === 'example_mod_bridge');
  assert.equal(inventory.available, true);
  assert.deepEqual(
    inventory.actions.map((item) => item.effect),
    ['read', 'write', 'interact']
  );
  assert.equal(missing.available, false);
  assert.match(missing.reason, /没有检测到模组/);
});

test('example plugin reads inventory, writes/drop and interacts', async () => {
  const { bot, calls } = fakeBot();
  const summary = await callPlugin(
    plugins,
    'inventory_bridge',
    'inventory_summary',
    {},
    { bot, mcData }
  );
  assert.equal(summary.ok, true);
  assert.equal(summary.result.items.length, 2);

  const dropped = await callPlugin(
    plugins,
    'inventory_bridge',
    'drop_item',
    { item: 'apple', count: 1 },
    { bot, mcData }
  );
  assert.equal(dropped.ok, true);
  assert.deepEqual(calls.toss[0], [260, null, 1]);

  const used = await callPlugin(
    plugins,
    'inventory_bridge',
    'use_held_item',
    {},
    { bot, mcData }
  );
  assert.equal(used.ok, true);
  assert.equal(calls.activated, 1);
  assert.equal(calls.deactivated, 1);
});

test('calling a skipped plugin returns a clear condition error', async () => {
  const { bot } = fakeBot();
  const result = await callPlugin(
    plugins,
    'example_mod_bridge',
    'mod_scan',
    {},
    { bot, mcData }
  );
  assert.equal(result.ok, false);
  assert.equal(result.skipped, true);
  assert.match(result.error, /条件不满足/);
});
