'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { detectIncident } = require('../index.js');

function hazardBot(overrides = {}, oxygen = 300) {
  return {
    health: 20,
    oxygenLevel: oxygen,
    entity: {
      isInWater: false,
      isInLava: false,
      isInFire: false,
      ...overrides,
    },
  };
}

test('incident detector reports drowning before oxygen is empty', () => {
  const incident = detectIncident(hazardBot({ isInWater: true }, 120));
  assert.equal(incident.kind, 'water');
});

test('incident detector ignores harmless surface swimming', () => {
  assert.equal(
    detectIncident(hazardBot({ isInWater: true, oxygenLevel: 300 })),
    null
  );
});

test('incident detector prioritizes lava and low health', () => {
  assert.equal(
    detectIncident(hazardBot({ isInLava: true, isInWater: false })).kind,
    'lava'
  );
  const low = hazardBot();
  low.health = 4;
  assert.equal(detectIncident(low).kind, 'low_health');
});
