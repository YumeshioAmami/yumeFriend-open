const assert = require('node:assert/strict');
const test = require('node:test');

const { foodInInventory } = require('../lib/inventory');

const data = {
  foodsByName: { bread: { foodPoints: 5 } },
  itemsByName: { dirt: { name: 'dirt' } },
};

function botWith(items) {
  return { inventory: { items: () => items } };
}

test('foodInInventory reads foodPoints from foodsByName', () => {
  const found = foodInInventory(
    botWith([{ name: 'dirt', count: 4 }, { name: 'bread', count: 3 }]),
    data
  );
  assert.equal(found.name, 'bread');
});

test('foodInInventory ignores non-food items', () => {
  assert.equal(
    foodInInventory(botWith([{ name: 'dirt', count: 4 }]), data),
    undefined
  );
});
