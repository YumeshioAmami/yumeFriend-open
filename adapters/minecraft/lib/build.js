'use strict';

/**
 * 简单的确定性建造器（D263）。
 *
 * 为什么放在适配器里：主代码不认识 Minecraft 的坐标和方块规则；适配器负责
 * “把房子真的盖出来”。她只需要说 `build({"structure":"shelter"})`。
 */

const Vec3 = require('vec3');
const { goals, Movements } = require('mineflayer-pathfinder');
const { pickBlockName } = require('./args');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function ensureMovements(bot) {
  if (bot.pathfinder.movements) return;
  const movements = new Movements(bot);
  movements.canDig = true;
  bot.pathfinder.setMovements(movements);
}

function clampInt(value, min, max, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(min, Math.min(max, Math.floor(number)));
}

function isAir(block) {
  return !block || block.boundingBox === 'empty';
}

function isLiquid(block) {
  return Boolean(block && /water|lava/.test(block.name || ''));
}

/**
 * 5×5 空心小屋：墙（留两格高的门洞）+ 平屋顶。
 * 返回**放置顺序**：墙从下到上，屋顶从外圈到内圈。
 */
function shelterLayout(origin, width = 5, depth = 5, height = 3) {
  const w = clampInt(width, 3, 7, 5);
  const d = clampInt(depth, 3, 7, 5);
  const h = clampInt(height, 2, 4, 3);
  const doorX = Math.floor(w / 2);
  const blocks = [];

  for (let y = 1; y <= h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      for (let z = 0; z < d; z += 1) {
        const perimeter = x === 0 || x === w - 1 || z === 0 || z === d - 1;
        if (!perimeter) continue;
        // 正面（z=0）中间留两格高的门洞
        if (z === 0 && x === doorX && y <= 2) continue;
        blocks.push({
          x: origin.x + x,
          y: origin.y + y,
          z: origin.z + z,
          kind: 'wall',
        });
      }
    }
  }

  const roof = [];
  for (let x = 0; x < w; x += 1) {
    for (let z = 0; z < d; z += 1) {
      roof.push({
        x: origin.x + x,
        y: origin.y + h + 1,
        z: origin.z + z,
        kind: 'roof',
        ring: Math.min(x, z, w - 1 - x, d - 1 - z),
      });
    }
  }
  roof.sort((a, b) => a.ring - b.ring || a.z - b.z || a.x - b.x);
  for (const block of roof) {
    const { ring: _ring, ...rest } = block;
    blocks.push(rest);
  }
  return blocks;
}

function blockAt(bot, position) {
  try {
    return bot.blockAt(position);
  } catch (_) {
    return null;
  }
}

function siteIsFlat(bot, origin, width, depth, height) {
  for (let x = 0; x < width; x += 1) {
    for (let z = 0; z < depth; z += 1) {
      const ground = blockAt(bot, new Vec3(origin.x + x, origin.y - 1, origin.z + z));
      if (!ground || isAir(ground) || isLiquid(ground)) return false;
      for (let y = 0; y <= height + 1; y += 1) {
        const body = blockAt(bot, new Vec3(origin.x + x, origin.y + y, origin.z + z));
        if (!isAir(body)) return false;
      }
    }
  }
  return true;
}

/** 在玩家附近找几块同高度的平地（给 prompt 的建房候选）。 */
function findFlatSites(bot, width, depth, height, maxRadius = 16, limit = 3, step = 2) {
  const base = bot.entity.position.floored();
  const candidates = [];
  for (let dx = -maxRadius; dx <= maxRadius; dx += step) {
    for (let dz = -maxRadius; dz <= maxRadius; dz += step) {
      const distance = Math.hypot(dx, dz);
      if (distance < 2 || distance > maxRadius) continue;
      candidates.push({ dx, dz, distance });
    }
  }
  candidates.sort((a, b) => a.distance - b.distance);

  const sites = [];
  for (const candidate of candidates) {
    const centerX = base.x + candidate.dx;
    const centerZ = base.z + candidate.dz;
    const origin = new Vec3(
      centerX - Math.floor(width / 2),
      base.y,
      centerZ - Math.floor(depth / 2)
    );
    if (!siteIsFlat(bot, origin, width, depth, height)) continue;
    const ground = blockAt(
      bot,
      new Vec3(centerX, origin.y - 1, centerZ)
    );
    sites.push({
      x: origin.x,
      y: origin.y,
      z: origin.z,
      center_x: centerX,
      center_y: origin.y,
      center_z: centerZ,
      distance: Number(candidate.distance.toFixed(1)),
      ground: ground ? ground.name : null,
    });
    if (sites.length >= limit) break;
  }
  return sites;
}

/** 在玩家附近找一块同高度的平地；找不到就返回 null。 */
function findFlatSite(bot, width, depth, height, maxRadius = 24) {
  const sites = findFlatSites(bot, width, depth, height, maxRadius, 1);
  if (!sites.length) return null;
  return new Vec3(sites[0].x, sites[0].y, sites[0].z);
}

function pickMaterial(bot, requested) {
  const wanted = pickBlockName({ block: requested });
  const candidates = [
    wanted,
    'oak_planks',
    'spruce_planks',
    'birch_planks',
    'cobblestone',
    'stone',
    'dirt',
    'sand',
  ].filter(Boolean);
  const inventory = bot.inventory ? bot.inventory.items() : [];
  for (const name of candidates) {
    const item = inventory.find((entry) => entry.name === name);
    if (item) return item;
  }
  return null;
}

function referenceFor(bot, target) {
  const directions = [
    [new Vec3(0, -1, 0), new Vec3(0, 1, 0)],
    [new Vec3(0, 1, 0), new Vec3(0, -1, 0)],
    [new Vec3(1, 0, 0), new Vec3(-1, 0, 0)],
    [new Vec3(-1, 0, 0), new Vec3(1, 0, 0)],
    [new Vec3(0, 0, 1), new Vec3(0, 0, -1)],
    [new Vec3(0, 0, -1), new Vec3(0, 0, 1)],
  ];
  for (const [offset, face] of directions) {
    const block = blockAt(bot, target.plus(offset));
    if (block && !isAir(block)) return { reference: block, face };
  }
  return null;
}

async function placeOne(bot, target, material, task) {
  if (task.cancelled) return false;
  const existing = blockAt(bot, target);
  if (!isAir(existing)) return false;

  const found = referenceFor(bot, target);
  if (!found) return false;

  // 站在屋内时通常够得到；万一太远（7×7 的角）就走近一点
  const center = target.offset(0.5, 0.5, 0.5);
  if (bot.entity.position.distanceTo(center) > 4.2) {
    try {
      ensureMovements(bot);
      await bot.pathfinder.goto(new goals.GoalNear(target.x, target.y, target.z, 3));
    } catch (_) {
      /* 走不过去就试试原地放 */
    }
  }

  const held = bot.heldItem;
  if (!held || held.name !== material.name) {
    await bot.equip(material, 'hand');
  }
  await bot.lookAt(center, true);
  await bot.placeBlock(found.reference, found.face);
  await sleep(120);
  return true;
}

async function buildTask(bot, args, task) {
  const structure = String((args && args.structure) || 'shelter').toLowerCase();
  if (!['shelter', 'house', 'hut'].includes(structure)) {
    throw new Error('现在只支持 structure=shelter（空心小屋）');
  }
  const width = clampInt(args && args.width, 3, 7, 5);
  const depth = clampInt(args && args.depth, 3, 7, 5);
  const height = clampInt(args && args.height, 2, 4, 3);

  let origin = null;
  const hasCoordinates = [args && args.x, args && args.y, args && args.z].every(
    (value) => Number.isFinite(Number(value))
  );
  if (hasCoordinates) {
    origin = new Vec3(Number(args.x), Number(args.y), Number(args.z));
  } else {
    origin = findFlatSite(bot, width, depth, height, 24);
  }
  if (!origin) {
    throw new Error(
      `附近 ${8} 格内找不到 ${width}×${depth} 的平地——先走到平一点的地方，` +
        '或者用 place 自己铺一块地基'
    );
  }

  const material = pickMaterial(bot, args && (args.block || args.item));
  if (!material) {
    throw new Error('背包里没有可以盖房子的方块（木板 / 圆石 / 泥土）');
  }
  const available = bot.inventory
    .items()
    .filter((item) => item.name === material.name)
    .reduce((total, item) => total + item.count, 0);
  const layout = shelterLayout(origin, width, depth, height);
  if (available <= 0) throw new Error(`背包里没有 ${material.name}`);

  // 站到屋子中间（那里必须是空气）；之后墙和屋顶都在伸手范围内
  const center = new Vec3(
    origin.x + Math.floor(width / 2),
    origin.y,
    origin.z + Math.floor(depth / 2)
  );
  let reachedCenter = false;
  try {
    ensureMovements(bot);
    await bot.pathfinder.goto(
      new goals.GoalNear(center.x, center.y, center.z, 1)
    );
    reachedCenter = true;
  } catch (_) {
    /* 下面给一句人话，不硬盖 */
  }
  if (!reachedCenter) {
    throw new Error('找到一块平地，但走不到中间——先把挡路的方块清一下');
  }

  let placed = 0;
  let skipped = 0;
  let outOfMaterial = false;
  for (const entry of layout) {
    if (task.cancelled) break;
    if (placed >= available) {
      outOfMaterial = true;
      break;
    }
    const target = new Vec3(entry.x, entry.y, entry.z);
    try {
      const ok = await placeOne(bot, target, material, task);
      if (ok) placed += 1;
      else skipped += 1;
    } catch (_) {
      skipped += 1;
      await sleep(100);
    }
  }

  return {
    structure: 'shelter',
    origin: { x: origin.x, y: origin.y, z: origin.z },
    width,
    depth,
    height,
    block: material.name,
    placed,
    skipped,
    missing: Math.max(0, layout.length - placed - skipped),
    out_of_material: outOfMaterial,
    cancelled: Boolean(task.cancelled),
    note:
      placed > 0
        ? '小屋的墙和屋顶已经按顺序放好；门洞留在正面中间'
        : '一块也没放成——先确认脚下是平地、背包里有方块',
  };
}

module.exports = {
  shelterLayout,
  findFlatSite,
  findFlatSites,
  buildTask,
};
