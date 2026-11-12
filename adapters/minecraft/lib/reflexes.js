'use strict';

// P2 反射层：只做"身体本能"的判断，不涉及性格/价值观（宪法§1）。

const HOSTILE_NAMES = new Set([
  'zombie',
  'zombie_villager',
  'husk',
  'drowned',
  'skeleton',
  'stray',
  'bogged',
  'wither_skeleton',
  'creeper',
  'spider',
  'cave_spider',
  'silverfish',
  'endermite',
  'slime',
  'magma_cube',
  'witch',
  'pillager',
  'vindicator',
  'evoker',
  'ravager',
  'illusioner',
  'vex',
  'blaze',
  'ghast',
  'guardian',
  'elder_guardian',
  'phantom',
  'shulker',
  'warden',
  'breeze',
  'piglin',
  'piglin_brute',
  'hoglin',
  'zoglin',
  'wither',
  'ender_dragon',
]);

const DEFAULT_MODES = {
  self_preservation: { enabled: true, priority: 100, interrupts: true, cooldown: 0.5 },
};

const MODE_ORDER = Object.keys(DEFAULT_MODES);

function normalizeReflexConfig(raw) {
  const source = raw && typeof raw === 'object' && raw.modes ? raw.modes : {};
  const modes = {};
  for (const name of MODE_ORDER) {
    const defaults = DEFAULT_MODES[name];
    const item = source[name] && typeof source[name] === 'object' ? source[name] : {};
    const priority = Number(item.priority ?? defaults.priority);
    const cooldown = Number(item.cooldown ?? defaults.cooldown);
    modes[name] = {
      enabled: item.enabled === undefined ? defaults.enabled : Boolean(item.enabled),
      priority: Number.isFinite(priority) ? Math.max(0, Math.min(999, priority)) : defaults.priority,
      interrupts: item.interrupts === undefined ? defaults.interrupts : Boolean(item.interrupts),
      cooldown: Number.isFinite(cooldown) ? Math.max(0, Math.min(300, cooldown)) : defaults.cooldown,
    };
  }
  return { modes };
}

function orderedModes(config) {
  const normalized = normalizeReflexConfig(config);
  return Object.entries(normalized.modes)
    .filter(([, values]) => values.enabled)
    .sort((left, right) => right[1].priority - left[1].priority || left[0].localeCompare(right[0]));
}

function bareName(name) {
  return String(name || '').replace(/^minecraft:/, '');
}

function isHostile(name) {
  return HOSTILE_NAMES.has(bareName(name));
}

function shouldEat(health, food) {
  return Number(health) < 12 && Number(food) >= 18;
}

function isCloseEnough(distance, limit = 3.5) {
  return Number(distance) <= limit;
}

function hasMoved(before, after, threshold = 0.4) {
  if (!before || !after) return true;
  const dx = Number(after.x) - Number(before.x);
  const dy = Number(after.y) - Number(before.y);
  const dz = Number(after.z) - Number(before.z);
  return Math.sqrt(dx * dx + dy * dy + dz * dz) >= threshold;
}

module.exports = {
  HOSTILE_NAMES,
  DEFAULT_MODES,
  normalizeReflexConfig,
  orderedModes,
  isHostile,
  shouldEat,
  isCloseEnough,
  hasMoved,
};
