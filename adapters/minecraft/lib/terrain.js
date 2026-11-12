'use strict';

/**
 * 紧凑地形感知（D332）。
 *
 * 不把每个方块坐标塞给模型，而是给：
 *   · 一张俯视字符图（北在上）
 *   · 高度范围
 *   · 最近的水 / 岩浆 / 树 / 石头
 *
 * 地图是**采样**出来的，尺寸固定，所以 token 不会随周围地形面积爆炸。
 */

const Vec3 = require('vec3');

const WATER_RE = /water/;
const LAVA_RE = /lava/;
const TREE_RE = /(_log$|_wood$|_leaves$|_stem$|bamboo)/;
const STONE_RE = /stone|cobblestone|deepslate|_ore|obsidian|netherrack|terracotta|brick/;
const TOOL_TIER = {
  netherite: 6,
  diamond: 5,
  iron: 4,
  stone: 3,
  golden: 2,
  wooden: 1,
};

function blockAtSafe(bot, x, y, z) {
  try {
    return bot.blockAt(new Vec3(x, y, z));
  } catch (_) {
    return null;
  }
}

function surfaceAt(bot, x, z, centerY, verticalSpan = 12) {
  for (let y = centerY + verticalSpan; y >= centerY - verticalSpan; y -= 1) {
    const block = blockAtSafe(bot, x, y, z);
    if (!block) continue;
    const name = String(block.name || 'air');
    if (name === 'air' || name === 'cave_air' || name === 'void_air') continue;
    if (WATER_RE.test(name) || LAVA_RE.test(name) || block.boundingBox !== 'empty') {
      return { y, name };
    }
  }
  return { y: null, name: null };
}

function cellCode(surface, playerY) {
  const name = surface.name;
  if (!name) return '?';
  if (WATER_RE.test(name)) return '~';
  if (LAVA_RE.test(name)) return '!';
  if (TREE_RE.test(name)) return 'T';
  if (STONE_RE.test(name)) return '#';
  if (surface.y !== null && surface.y > playerY + 1) return '^';
  if (surface.y !== null && surface.y < playerY - 1) return 'v';
  return '.';
}

function directionLabel(dx, dz) {
  const ns = dz > 0.5 ? '南' : dz < -0.5 ? '北' : '';
  const ew = dx > 0.5 ? '东' : dx < -0.5 ? '西' : '';
  return `${ns}${ew}` || '同格附近';
}

function nearestFeature(cells, predicate) {
  let best = null;
  for (const cell of cells) {
    if (!predicate(cell)) continue;
    if (best === null || cell.distance < best.distance) best = cell;
  }
  if (!best) return null;
  return {
    x: best.x,
    y: best.y,
    z: best.z,
    name: best.name,
    distance: Number(best.distance.toFixed(1)),
    direction: directionLabel(best.dx, best.dz),
  };
}

function toolTierRank(name) {
  const base = String(name || '')
    .replace(/^minecraft:/, '')
    .replace(/_(pickaxe|axe|shovel|hoe|sword)$/, '');
  return TOOL_TIER[base] || 0;
}

function durabilityLeft(item) {
  const max = Number(item && item.maxDurability);
  const used = Number(item && item.durabilityUsed);
  if (!Number.isFinite(max) || max <= 0 || !Number.isFinite(used)) return 0;
  return Math.max(0, max - used);
}

function bestTool(items, kind) {
  const candidates = (items || []).filter(
    (item) => item && typeof item.name === 'string' && item.name.includes(kind)
  );
  candidates.sort(
    (left, right) =>
      toolTierRank(right.name) - toolTierRank(left.name) ||
      durabilityLeft(right) - durabilityLeft(left)
  );
  return candidates[0] || null;
}

const MAP_CACHE = new Map();

function buildTerrainMap(
  bot,
  { radius = 8, step = 1, verticalSpan = 12, cacheMs = 8000 } = {}
) {
  const position = bot && bot.entity && bot.entity.position;
  if (!position) return null;
  const radiusInt = Math.max(1, Math.min(128, Math.floor(Number(radius) || 8)));
  const stepInt = Math.max(1, Math.min(16, Math.floor(Number(step) || 1)));
  const half = Math.max(1, Math.min(32, Math.floor(radiusInt / stepInt)));
  const baseX = Math.floor(position.x);
  const baseY = Math.floor(position.y);
  const baseZ = Math.floor(position.z);
  const cacheKey = `${baseX}:${baseY}:${baseZ}:${half}:${stepInt}`;
  const cached = MAP_CACHE.get(cacheKey);
  if (cached && Date.now() - cached.at < cacheMs) return cached.map;

  const originX = baseX - half * stepInt;
  const originZ = baseZ - half * stepInt;
  const rows = [];
  const cells = [];
  for (let row = 0; row < half * 2 + 1; row += 1) {
    let line = '';
    for (let col = 0; col < half * 2 + 1; col += 1) {
      const x = originX + col * stepInt;
      const z = originZ + row * stepInt;
      const surface = surfaceAt(bot, x, z, baseY, verticalSpan);
      const code = cellCode(surface, baseY);
      line += code;
      const dx = x - position.x;
      const dz = z - position.z;
      cells.push({
        x,
        z,
        y: surface.y,
        name: surface.name,
        code,
        dx,
        dz,
        distance: Math.hypot(dx, dz),
      });
    }
    rows.push(line);
  }

  const heights = cells
    .filter((cell) => cell.y !== null)
    .map((cell) => Number(cell.y));
  const map = {
    radius: half * stepInt,
    step: stepInt,
    origin: { x: originX, z: originZ },
    rows,
    height_min: heights.length ? Math.min(...heights) : null,
    height_max: heights.length ? Math.max(...heights) : null,
    water_cells: cells.filter((cell) => WATER_RE.test(cell.name || '')).length,
    tree_cells: cells.filter((cell) => TREE_RE.test(cell.name || '')).length,
    stone_cells: cells.filter((cell) => STONE_RE.test(cell.name || '')).length,
    nearest_water: nearestFeature(cells, (cell) => WATER_RE.test(cell.name || '')),
    nearest_lava: nearestFeature(cells, (cell) => LAVA_RE.test(cell.name || '')),
    nearest_tree: nearestFeature(cells, (cell) => TREE_RE.test(cell.name || '')),
    nearest_stone: nearestFeature(cells, (cell) => STONE_RE.test(cell.name || '')),
  };
  if (MAP_CACHE.size > 32) MAP_CACHE.clear();
  MAP_CACHE.set(cacheKey, { at: Date.now(), map });
  return map;
}

module.exports = {
  buildTerrainMap,
  bestTool,
  directionLabel,
  toolTierRank,
  WATER_RE,
  LAVA_RE,
  TREE_RE,
  STONE_RE,
};
