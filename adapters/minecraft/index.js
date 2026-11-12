#!/usr/bin/env node
'use strict';

/**
 * yumeFriend Minecraft 适配器（契约 v1 / D250-D252）。
 *
 * 它只做两件事：**上报事实** + **执行指令**。
 * 不认识 LLM、不碰数据库、不写人格/记忆。
 *
 * 启动（由主代码的 AdapterProcess 给端口和 token）：
 *   node index.js --port 12345 --token <token>
 * 可选默认值：
 *   --mc-host 127.0.0.1 --mc-port 25565 --username Xiyin_ --heartbeat 5
 */

const http = require('http');
const { URL } = require('url');
const { WebSocketServer, WebSocket } = require('ws');
const mineflayer = require('mineflayer');
const mcProtocol = require('minecraft-protocol');
const minecraftData = require('minecraft-data');
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder');
const Vec3 = require('vec3');
const { resolveVersionName, supportedVersions } = require('./lib/version');
const {
  isHostile,
  isCloseEnough,
  hasMoved,
  normalizeReflexConfig,
  orderedModes,
} = require('./lib/reflexes');
const { pickBlockName, pickAttackTarget } = require('./lib/args');
const { findFlatSites } = require('./lib/build');
const { foodInInventory } = require('./lib/inventory');
const { recipeFor, toolTierFor } = require('./lib/knowledge');
const { buildTerrainMap, bestTool } = require('./lib/terrain');
const {
  loadPlugins,
  describePlugins,
  callPlugin,
} = require('./plugins/loader');

const PROTOCOL_VERSION = 1;
const ADAPTER_VERSION = '0.1.0';

// D252：**不手写版本表** —— mineflayer CI 测过 + 本地真的有数据（P4 裁剪后自动只剩保留版本）
const SUPPORTED_VERSIONS = supportedVersions([...mineflayer.testedVersions]);

const ACTIONS = {
  follow: {
    params: { player: 'string' },
    description: '跟着某个玩家（不填 = 离你最近的那个）',
  },
  mode: {
    params: { mode: 'string', player: 'string' },
    description: '常态模式：follow / chat / free（由主代码调用，不是普通任务）',
  },
  goto: {
    params: { x: 'number', y: 'number', z: 'number' },
    description: '走到坐标',
  },
  say: {
    params: { text: 'string' },
    description: '在游戏里说一句',
  },
  command: {
    params: { commands: 'array' },
    description: '内部动作：执行一条或多条服务器命令（只能由主代码的 game_cheat 调用）',
  },
  inspect_blocks: {
    params: { positions: 'array' },
    description: '内部动作：批量读取指定坐标的方块（蓝图校验用，不是普通任务）',
  },
  survey_area: {
    params: { radius: 'number', step: 'number' },
    description: '内部动作：读取周围地形的紧凑字符图和地标',
  },
  knowledge_query: {
    params: { query: 'string', name: 'string' },
    description: '内部动作：从 minecraft-data 查配方 / 工具等级事实',
  },
  configure_reflex: {
    params: { config: 'object' },
    description: '内部动作：实时更新反射 mode 配置',
  },
  plugin_list: {
    params: {},
    description: '内部动作：列出模组插件与可用性',
  },
  plugin_call: {
    params: { plugin: 'string', action: 'string', args: 'object' },
    description: '内部动作：调用模组插件动作（read / write / interact）',
  },
  stop: {
    params: {},
    description: '停下当前动作',
  },
  sequence: {
    params: { steps: 'array' },
    description: '按顺序连续做多步（mine → craft → place …），只在整个 sequence 结束时报一次',
  },
  rest: {
    params: { seconds: 'number' },
    description: '坐下/蹲下休息，倒计时结束自动回来（有秒数）',
  },
  wait: {
    params: { seconds: 'number' },
    description: '原地待命，倒计时结束自动回来（有秒数）',
  },
  patrol: {
    params: { seconds: 'number' },
    description: '系统自动待机巡逻（不要主动调用）',
  },
  mine: {
    params: { block: 'string', count: 'number' },
    description: '挖最近的某种方块（默认 1 个）',
  },
  place: {
    params: { block: 'string', x: 'number', y: 'number', z: 'number' },
    description: '在坐标放一个方块（从背包里拿）',
  },
  dig_at: {
    params: { x: 'number', y: 'number', z: 'number' },
    description: '精确挖掉指定坐标的方块（蓝图原位修复用）',
  },
  fly: {
    params: { enabled: 'boolean', x: 'number', y: 'number', z: 'number' },
    description: '创造模式飞行：开关飞行；给坐标时飞到该点',
  },
  fish: {
    params: { timeout: 'number' },
    description: '用鱼竿在附近水域钓鱼',
  },
  shoot: {
    params: {
      target: 'string',
      x: 'number',
      y: 'number',
      z: 'number',
      charge_ms: 'number',
    },
    description: '用弓/弩射击目标实体或坐标',
  },
  boat: {
    params: { x: 'number', y: 'number', z: 'number' },
    description: '在附近水面放船并上船',
  },
  mount: {
    params: { target: 'string' },
    description: '骑上附近的船 / 矿车 / 动物',
  },
  dismount: {
    params: {},
    description: '从当前载具下来',
  },
  use_item: {
    params: { hold_ms: 'number' },
    description: '使用手上的物品（右键；例如打火石、骨粉、桶）',
  },
  pillar: {
    params: { block: 'string', count: 'number' },
    description: '站在原地往上垫方块（爬高 / 出坑）',
  },
  craft: {
    params: { item: 'string', count: 'number' },
    description: '合成物品（木头→木板/木棍/工作台/镐子…）',
  },
  smelt: {
    params: { item: 'string', count: 'number' },
    description: '用熔炉烧炼（需要燃料）',
  },
  equip: {
    params: { item: 'string' },
    description: '把背包里的物品拿到手上（换工具 / 食物）',
  },
  attack: {
    params: { target: 'string', count: 'number' },
    description: '打附近的敌对生物（默认 1 只）',
  },
  eat: {
    params: {},
    description: '吃东西（血少 / 饿的时候）',
  },
  drop: {
    params: { item: 'string', count: 'number' },
    description: '把背包里的物品丢出去',
  },
  bring: {
    params: { item: 'string', count: 'number', player: 'string' },
    description: '走到某个玩家身边，把物品交给他',
  },
  escape: {
    params: {},
    description: '被卡住 / 困在洞里时，挖一条路脱困',
  },
};

const CAPABILITIES = {
  game: 'minecraft',
  display_name: 'Minecraft',
  protocol_version: PROTOCOL_VERSION,
  adapter_version: ADAPTER_VERSION,
  supported_versions: SUPPORTED_VERSIONS,
  actions: ACTIONS,
  limits: { max_tasks: 4, tick_hz: 20 },
};

// ---------- 命令行 ----------

function parseArgs(argv) {
  const out = {
    port: 0,
    token: '',
    mcHost: '127.0.0.1',
    mcPort: 25565,
    username: 'Xiyin_',
    heartbeat: 5,
    reflex: true,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (key === '--port') out.port = Number(value);
    else if (key === '--token') out.token = String(value || '');
    else if (key === '--mc-host') out.mcHost = String(value || out.mcHost);
    else if (key === '--mc-port') out.mcPort = Number(value);
    else if (key === '--username') out.username = String(value || out.username);
    else if (key === '--heartbeat') out.heartbeat = Number(value);
    else if (key === '--no-reflex') {
      out.reflex = false;
      continue;
    }
    else continue;
    i += 1;
  }
  if ((!out.port || !out.token) && require.main === module) {
    console.error('用法：node index.js --port <端口> --token <token>');
    process.exit(2);
  }
  return out;
}

const cli = parseArgs(process.argv.slice(2));
const startedAt = Date.now();

// P4.1：父进程（yumeFriend.exe）被强杀时，别把自己留成孤儿（每 5 秒看一眼）
const PARENT_PID = Number(process.env.YF_PARENT_PID || 0);
if (PARENT_PID > 0) {
  setInterval(() => {
    try {
      process.kill(PARENT_PID, 0);
    } catch (_) {
      process.exit(0);
    }
  }, 5000).unref();
}

// ---------- 会话状态 ----------

const state = {
  bot: null,
  sessionId: null,
  status: 'idle', // idle / starting / running / stopping / ended / crashed
  tasks: new Map(),
  followTarget: null,
  mode: 'free',
  sockets: new Set(),
  buffered: [],
  heartbeatTimer: null,
  counter: 0,
  lastHealth: 20,
  config: {},
  stopping: false,
  reflexTimer: null,
  reflexBusy: false,
  nextDefendAt: 0,
  lastPosition: null,
  lastMovedAt: Date.now(),
  stuckTries: 0,
  escaping: false,
  collectingTaskIds: new Set(),
  collectedResults: new Map(),
  reflexConfig: normalizeReflexConfig({}),
  reflexLastRun: {},
  activeMoveTaskId: null,
  activeMoveTaskAction: null,
  incident: null,
  incidentCounter: 0,
  plugins: [],
  proximityTimer: null,
  nearPlayers: new Map(),
  nearHostiles: new Map(),
  inventoryTimer: null,
  inventoryCounts: new Map(),
  inventoryVersion: 0,
};

function nowIso() {
  return new Date().toISOString();
}

function emit(payload) {
  const event = {
    event_id: `${payload.type || 'event'}-${Math.random().toString(16).slice(2, 10)}`,
    ts: nowIso(),
    ...payload,
  };
  const line = JSON.stringify(event);
  let sent = false;
  for (const socket of state.sockets) {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(line);
      sent = true;
    }
  }
  if (!sent) {
    state.buffered.push(event);
    if (state.buffered.length > 200) state.buffered.shift();
  }
}

function httpError(status, code, message) {
  const error = new Error(message);
  error.status = status;
  error.code = code;
  return error;
}

// ---------- 版本预检（D252） ----------

async function pingServer(host, port) {
  const result = await mcProtocol.ping({ host, port, closeTimeout: 5000 });
  const version = (result && result.version) || {};
  return {
    name: version.name,
    protocol: version.protocol,
    resolved: resolveVersionName(version.protocol, version.name),
  };
}

// ---------- 机器人 ----------

function nearestPlayer(bot) {
  const entity = bot.nearestEntity(
    (item) => item.type === 'player' && item.username !== bot.username
  );
  return entity ? { username: entity.username, entity } : null;
}

function resolveFollowTarget(bot, name) {
  if (name) {
    const player = bot.players && bot.players[name];
    if (player && player.entity) return { username: name, entity: player.entity };
    return null;
  }
  return nearestPlayer(bot);
}

function makeMovements(bot) {
  const movements = new Movements(bot);
  // P4.3：路被挡住时允许挖路（卡在洞里能自己出来，而不是原地跳）
  movements.canDig = true;
  // 低处/洞里：允许开门、垫一格、跳远；pathfinder 能选的路更多。
  movements.canOpenDoors = true;
  movements.allow1by1towers = true;
  movements.allowParkour = true;
  if (bot.pathfinder) {
    // 地形复杂时别 5 秒就放弃（默认太短 → "Took to long to decide path to goal!"）
    bot.pathfinder.thinkTimeout = 6000;
    bot.pathfinder.tickTimeout = 100;
  }
  return movements;
}

// D276：常态模式不是任务 —— 直接改寻路目标，不发 task_done。
function applyMode(bot, args) {
  const mode = String((args && args.mode) || 'free').toLowerCase();
  if (!['follow', 'chat', 'free', 'none'].includes(mode)) {
    throw new Error('mode 只能是 follow / chat / free');
  }
  if (mode === 'free' || mode === 'none') {
    try {
      bot.pathfinder.setGoal(null);
    } catch (_) {
      /* 没加载寻路也没关系 */
    }
    state.followTarget = null;
    state.mode = mode === 'none' ? 'none' : 'free';
    return { mode: state.mode };
  }
  const target = resolveFollowTarget(bot, args && args.player);
  state.mode = mode;
  if (!target) {
    state.followTarget = null;
    try {
      bot.pathfinder.setGoal(null);
    } catch (_) {
      /* ignore */
    }
    return { mode, following: null };
  }
  const distance = mode === 'chat' ? 5 : 2;
  state.followTarget = target.username;
  bot.pathfinder.setMovements(makeMovements(bot));
  bot.pathfinder.setGoal(new goals.GoalFollow(target.entity, distance), true);
  return { mode, following: target.username, distance };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function withTimeout(promise, ms, message) {
  let timer = null;
  return Promise.race([
    promise.finally(() => {
      if (timer) clearTimeout(timer);
    }),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), ms);
    }),
  ]);
}

async function gotoWithTimeout(bot, goal, seconds, label = '寻路') {
  try {
    await withTimeout(
      bot.pathfinder.goto(goal),
      seconds * 1000,
      `${label}超时（${seconds}s）`
    );
  } catch (error) {
    try {
      bot.pathfinder.setGoal(null);
    } catch (_) {
      /* ignore */
    }
    throw error;
  }
}

function clampSeconds(value, fallback, maxSeconds) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(1, Math.min(number, maxSeconds));
}

const DATA_CACHE = new Map();
function dataFor(bot) {
  if (!DATA_CACHE.has(bot.version)) {
    DATA_CACHE.set(bot.version, minecraftData(bot.version));
  }
  return DATA_CACHE.get(bot.version);
}

function blockInfo(bot, name) {
  return dataFor(bot).blocksByName[name];
}

function findHostile(bot, name) {
  return bot.nearestEntity(
    (entity) =>
      entity.type === 'mob' && (name ? entity.name === name : isHostile(entity.name))
  );
}

function blockView(block) {
  return block
    ? {
        name: block.name,
        x: block.position.x,
        y: block.position.y,
        z: block.position.z,
      }
    : null;
}

function nearbyBlocks(bot, radius, step = 1) {
  const position = bot.entity && bot.entity.position;
  if (!position) return [];
  const base = position.floored();
  const counts = new Map();
  for (let dx = -radius; dx <= radius; dx += step) {
    for (let dz = -radius; dz <= radius; dz += step) {
      for (let dy = -4; dy <= 4; dy += 1) {
        const block = bot.blockAt(new Vec3(base.x + dx, base.y + dy, base.z + dz));
        if (!block || block.boundingBox === 'empty') continue;
        counts.set(block.name, (counts.get(block.name) || 0) + 1);
      }
    }
  }
  return Array.from(counts.entries())
    .sort((left, right) => right[1] - left[1])
    .slice(0, 18)
    .map(([name, count]) => ({ name, count }));
}

function mergeBlockCounts(near, far) {
  const counts = new Map();
  for (const item of [...near, ...far]) {
    counts.set(item.name, (counts.get(item.name) || 0) + item.count);
  }
  return Array.from(counts.entries())
    .sort((left, right) => right[1] - left[1])
    .slice(0, 18)
    .map(([name, count]) => ({ name, count }));
}

function directionLabel(dx, dz) {
  const ns = dz > 0.5 ? '南' : dz < -0.5 ? '北' : '';
  const ew = dx > 0.5 ? '东' : dx < -0.5 ? '西' : '';
  return `${ns}${ew}` || '同格附近';
}

function verticalLabel(dy) {
  if (dy > 1) return '上方';
  if (dy < -1) return '下方';
  return '同高';
}

function entityView(bot, entity) {
  const position = bot.entity && bot.entity.position;
  const dx = position ? entity.position.x - position.x : 0;
  const dz = position ? entity.position.z - position.z : 0;
  const dy = position ? entity.position.y - position.y : 0;
  return {
    name: entity.username || entity.name || entity.type,
    type: entity.type,
    distance: position
      ? Number(entity.position.distanceTo(position).toFixed(1))
      : null,
    direction: directionLabel(dx, dz),
    vertical: verticalLabel(dy),
    hostile: entity.type === 'mob' && isHostile(entity.name),
    x: Number(entity.position.x.toFixed(1)),
    y: Number(entity.position.y.toFixed(1)),
    z: Number(entity.position.z.toFixed(1)),
  };
}

function itemToolKind(name) {
  if (/_pickaxe$/.test(name)) return 'pickaxe';
  if (/_axe$/.test(name)) return 'axe';
  if (/_shovel$/.test(name)) return 'shovel';
  if (/_hoe$/.test(name)) return 'hoe';
  if (name === 'shears') return 'shears';
  if (name === 'fishing_rod') return 'fishing_rod';
  if (name === 'bow' || name === 'crossbow') return 'ranged';
  if (name === 'flint_and_steel') return 'igniter';
  return null;
}

function itemView(bot, item) {
  const data = dataFor(bot);
  const food = data.foodsByName[item.name];
  const maxDurability =
    Number.isFinite(Number(item.maxDurability)) && Number(item.maxDurability) > 0
      ? Number(item.maxDurability)
      : null;
  const durability =
    maxDurability !== null && Number.isFinite(Number(item.durabilityUsed))
      ? Math.max(0, maxDurability - Number(item.durabilityUsed))
      : null;
  return {
    slot: item.slot,
    name: item.name,
    count: item.count,
    tool: itemToolKind(item.name),
    durability,
    max_durability: maxDurability,
    food: Boolean(food && Number(food.foodPoints) > 0),
    arrow: /arrow$/.test(item.name),
    boat: /_boat$/.test(item.name),
    stackable: item.stackable,
  };
}

function inventoryDetails(bot) {
  const items = bot.inventory ? bot.inventory.items().map((item) => itemView(bot, item)) : [];
  const byName = new Map();
  for (const item of items) {
    const current = byName.get(item.name) || {
      item: item.name,
      count: 0,
      food: false,
      tool: item.tool,
      max_durability: item.max_durability,
    };
    current.count += Number(item.count || 0);
    current.food = current.food || item.food;
    current.tool = current.tool || item.tool;
    byName.set(item.name, current);
  }
  const emptySlots =
    bot.inventory && typeof bot.inventory.emptySlotCount === 'function'
      ? bot.inventory.emptySlotCount()
      : null;
  const equipment = {
    held: bot.heldItem ? itemView(bot, bot.heldItem) : null,
    offhand:
      bot.inventory && bot.inventory.slots && bot.inventory.slots[45]
        ? itemView(bot, bot.inventory.slots[45])
        : null,
    armor: [5, 6, 7, 8]
      .map((slot) => bot.inventory && bot.inventory.slots && bot.inventory.slots[slot])
      .filter(Boolean)
      .map((item) => itemView(bot, item)),
  };
  return {
    version: state.inventoryVersion,
    items,
    by_item: Array.from(byName.values()).sort((left, right) =>
      left.item.localeCompare(right.item)
    ),
    empty_slots: emptySlots,
    full: emptySlots === 0,
    tools: items.filter((item) => item.tool),
    ammo: items.filter((item) => item.arrow),
    boats: items.filter((item) => item.boat),
    foods: items.filter((item) => item.food),
    equipment,
  };
}

function inventoryCounts(bot) {
  const counts = new Map();
  for (const item of bot.inventory ? bot.inventory.items() : []) {
    counts.set(item.name, (counts.get(item.name) || 0) + Number(item.count || 0));
  }
  return counts;
}

function startInventoryTracking() {
  stopInventoryTracking();
  state.inventoryCounts = inventoryCounts(state.bot);
  state.inventoryVersion = 1;
  state.inventoryTimer = setInterval(() => {
    const bot = state.bot;
    if (!bot || state.status !== 'running' || !bot.inventory) return;
    const next = inventoryCounts(bot);
    const added = [];
    const removed = [];
    for (const [name, count] of next.entries()) {
      const before = state.inventoryCounts.get(name) || 0;
      if (count > before) added.push({ item: name, count: count - before });
      if (count < before) removed.push({ item: name, count: before - count });
    }
    for (const [name, count] of state.inventoryCounts.entries()) {
      if (!next.has(name)) removed.push({ item: name, count });
    }
    if (added.length || removed.length) {
      state.inventoryCounts = next;
      state.inventoryVersion += 1;
      emit({ type: 'inventory_change', added, removed });
    }
  }, 500);
}

function stopInventoryTracking() {
  if (state.inventoryTimer) {
    clearInterval(state.inventoryTimer);
    state.inventoryTimer = null;
  }
  state.inventoryCounts = new Map();
}

function nearbyEntities(bot, radius, limit = 12) {
  const position = bot.entity && bot.entity.position;
  if (!position) return [];
  return Object.values(bot.entities || {})
    .filter(
      (entity) =>
        entity &&
        entity !== bot.entity &&
        entity.position &&
        entity.position.distanceTo(position) <= radius &&
        (entity.type === 'player' || entity.type === 'mob')
    )
    .map((entity) => entityView(bot, entity))
    .sort((left, right) => left.distance - right.distance)
    .slice(0, limit);
}

function nearbyDroppedItems(bot, radius = 12) {
  const position = bot.entity && bot.entity.position;
  if (!position) return [];
  return Object.values(bot.entities || {})
    .filter(
      (entity) =>
        entity &&
        (entity.name === 'item' || entity.name === 'Item') &&
        entity.position &&
        entity.position.distanceTo(position) <= radius
    )
    .map((entity) => {
      let dropped = null;
      try {
        dropped = entity.getDroppedItem();
      } catch (_) {
        dropped = null;
      }
      return {
        name: (dropped && (dropped.name || dropped.displayName)) || 'item',
        count: dropped ? Number(dropped.count || 1) : 1,
        distance: Number(entity.position.distanceTo(position).toFixed(1)),
        x: Number(entity.position.x.toFixed(1)),
        y: Number(entity.position.y.toFixed(1)),
        z: Number(entity.position.z.toFixed(1)),
      };
    })
    .sort((left, right) => left.distance - right.distance)
    .slice(0, 12);
}

function localGrid(bot, radius = 2) {
  const position = bot.entity && bot.entity.position;
  if (!position) return [];
  const base = position.floored();
  const cells = [];
  for (let dx = -radius; dx <= radius; dx += 1) {
    for (let dz = -radius; dz <= radius; dz += 1) {
      const x = base.x + dx;
      const z = base.z + dz;
      let ground = null;
      for (let y = base.y + 2; y >= base.y - 5; y -= 1) {
        const block = bot.blockAt(new Vec3(x, y, z));
        if (block && block.boundingBox !== 'empty') {
          ground = { name: block.name, y };
          break;
        }
      }
      const feet = bot.blockAt(new Vec3(x, base.y, z));
      const head = bot.blockAt(new Vec3(x, base.y + 1, z));
      cells.push({
        x,
        z,
        ground,
        feet: feet && feet.boundingBox !== 'empty' ? feet.name : null,
        head: head && head.boundingBox !== 'empty' ? head.name : null,
      });
    }
  }
  return cells;
}

function terrainSense(bot) {
  const position = bot.entity && bot.entity.position;
  if (!position) return null;
  const base = position.floored();
  const directions = [
    { direction: '东', dx: 1, dz: 0 },
    { direction: '东南', dx: 1, dz: 1 },
    { direction: '南', dx: 0, dz: 1 },
    { direction: '西南', dx: -1, dz: 1 },
    { direction: '西', dx: -1, dz: 0 },
    { direction: '西北', dx: -1, dz: -1 },
    { direction: '北', dx: 0, dz: -1 },
    { direction: '东北', dx: 1, dz: -1 },
  ];
  const edges = [];
  let flat = true;
  for (const item of directions) {
    const samples = [];
    for (const distance of [4, 8, 16, 32, 64]) {
      const x = base.x + item.dx * distance;
      const z = base.z + item.dz * distance;
      let groundY = null;
      for (let y = base.y + 2; y >= base.y - 4; y -= 1) {
        const block = bot.blockAt(new Vec3(x, y, z));
        if (block && block.boundingBox !== 'empty') {
          groundY = block.position.y;
          break;
        }
      }
      let headroom = 0;
      for (let height = 0; height < 5; height += 1) {
        const block = bot.blockAt(new Vec3(x, base.y + height, z));
        if (block && block.boundingBox !== 'empty') break;
        headroom += 1;
      }
      const dy = groundY === null ? null : groundY - (base.y - 1);
      // 远处区块没加载时 dy=null，不代表地形有问题，别据此判"不平"
      if (dy !== null && (Math.abs(dy) >= 2 || headroom <= 1)) flat = false;
      samples.push({ distance, dy, headroom });
    }
    const worstDrop = samples.reduce(
      (worst, sample) =>
        Math.abs(sample.dy || 0) > Math.abs(worst.dy || 0) ? sample : worst,
      samples[0]
    );
    const leastHeadroom = samples.reduce(
      (worst, sample) => (sample.headroom < worst.headroom ? sample : worst),
      samples[0]
    );
    edges.push({
      direction: item.direction,
      distance: worstDrop.distance,
      dy: worstDrop.dy,
      headroom: leastHeadroom.headroom,
      samples,
    });
  }
  return { flat, edges };
}

function environmentInfo(bot) {
  const time = bot.time || {};
  let biome = null;
  try {
    const raw =
      bot.world && typeof bot.world.getBiome === 'function'
        ? bot.world.getBiome(bot.entity.position)
        : null;
    if (typeof raw === 'number') {
      const entry = dataFor(bot).biomes && dataFor(bot).biomes[raw];
      biome = entry ? entry.name : null;
    } else if (raw && typeof raw === 'object') {
      biome = raw.name || null;
    } else if (typeof raw === 'string') {
      biome = raw;
    }
  } catch (_) {
    /* 某些版本没有 biome API，拿不到就不写 */
  }
  return {
    dimension: (bot.game && bot.game.dimension) || null,
    time_of_day: Number.isFinite(time.timeOfDay) ? time.timeOfDay : null,
    is_day: typeof time.isDay === 'boolean' ? time.isDay : null,
    weather: bot.thunderState > 0 ? '雷雨' : bot.isRaining ? '下雨' : '晴',
    biome,
  };
}

let FLAT_SITE_CACHE = { key: '', at: 0, sites: [] };
function flatSites(bot) {
  const position = bot.entity && bot.entity.position;
  if (!position) return [];
  const key = `${Math.floor(position.x / 16)}:${Math.floor(position.y)}:${Math.floor(
    position.z / 16
  )}`;
  if (FLAT_SITE_CACHE.key === key && Date.now() - FLAT_SITE_CACHE.at < 15000) {
    return FLAT_SITE_CACHE.sites;
  }
  const sites = findFlatSites(bot, 5, 5, 3, 48, 3, 6);
  FLAT_SITE_CACHE = { key, at: Date.now(), sites };
  return sites;
}

function lookingAt(bot) {
  const block = blockAtCursor(bot, 64);
  if (!block || block.boundingBox === 'empty') return null;
  return {
    name: block.name,
    distance:
      bot.entity && block.position
        ? Number(
            bot.entity.position
              .distanceTo(block.position.offset(0.5, 0.5, 0.5))
              .toFixed(1)
          )
        : null,
    x: block.position.x,
    y: block.position.y,
    z: block.position.z,
  };
}

function blockAtCursor(bot, distance) {
  try {
    return bot.blockAtEntityCursor ? bot.blockAtEntityCursor(distance) : null;
  } catch (_) {
    return null;
  }
}

function isExposed(bot, block) {
  // 有任意一面挨着空气 = 露出来的（山壁 / 地面），比埋在地下的好挖
  const offsets = [
    [0, 1, 0],
    [1, 0, 0],
    [-1, 0, 0],
    [0, 0, 1],
    [0, 0, -1],
  ];
  return offsets.some(([dx, dy, dz]) => {
    const neighbor = bot.blockAt(block.position.offset(dx, dy, dz));
    return !neighbor || neighbor.boundingBox === 'empty';
  });
}

function isDirectlyBelow(bot, block) {
  const dx = Math.abs(block.position.x + 0.5 - bot.entity.position.x);
  const dz = Math.abs(block.position.z + 0.5 - bot.entity.position.z);
  return block.position.y < bot.entity.position.y - 0.5 && dx < 1.5 && dz < 1.5;
}

function toolKindForBlock(name) {
  if (/stone|cobblestone|deepslate|_ore|obsidian|netherrack|terracotta|brick/.test(name)) {
    return 'pickaxe';
  }
  if (/log|wood|planks|fence|door|crafting_table|chest|barrel/.test(name)) {
    return 'axe';
  }
  if (/dirt|grass_block|sand|gravel|clay|snow|mud|soul_soil/.test(name)) {
    return 'shovel';
  }
  if (/leaves|wool|vine/.test(name)) {
    return 'shears';
  }
  return null;
}

async function equipBestTool(bot, block) {
  const kind = toolKindForBlock(block.name);
  if (!kind) return;
  const tool = bestTool(bot.inventory.items(), kind);
  if (tool && (!bot.heldItem || bot.heldItem.name !== tool.name)) {
    try {
      await bot.equip(tool, 'hand');
    } catch (_) {
      /* 换不上就徒手 */
    }
  }
}

async function mineTask(bot, args, task) {
  const name = pickBlockName(args);
  // D263：默认 4 个，避免"每一步只挖一个方块"的碎动作；仍可用 count 指定
  const count = Math.max(1, Math.min(Number((args && args.count) || 4), 64));
  if (!name) throw new Error('mine 需要 block（例如 {"block":"dirt","count":1}）');
  const info = blockInfo(bot, name);
  if (!info) throw new Error(`不认识的方块：${name}`);
  const needsPickaxe = [
    'stone',
    'cobblestone',
    'deepslate',
    '_ore',
  ].some((part) => name.includes(part));
  const hasPickaxe = bot.inventory
    .items()
    .some((item) => item.name.includes('pickaxe'));
  if (needsPickaxe && !hasPickaxe) {
    throw new Error(
      '[条件不满足] 没有镐子：挖石头/矿石不会掉落，先 craft 一把镐子'
    );
  }
  bot.pathfinder.setMovements(makeMovements(bot));
  let mined = 0;
  task.progress = { current: 0, target: count, unit: name };
  let escapedOnce = false;
  for (let index = 0; index < count; index += 1) {
    if (task.cancelled) break;
    // 多找几棵，走不到就换下一棵（别因为一棵树在墙后就直接失败）
    const raw = bot
      .findBlocks({ matching: info.id, maxDistance: 64, count: 32 })
      .map((position) => bot.blockAt(position))
      .filter(Boolean);
    // 不挖正下方（垂直向下 = 掉洞/岩浆）；优先露出来的方块
    const candidates = raw
      .filter((block) => !isDirectlyBelow(bot, block))
      .sort((left, right) => {
        const exposedLeft = isExposed(bot, left) ? 0 : 1;
        const exposedRight = isExposed(bot, right) ? 0 : 1;
        if (exposedLeft !== exposedRight) return exposedLeft - exposedRight;
        return (
          left.position.distanceTo(bot.entity.position) -
          right.position.distanceTo(bot.entity.position)
        );
      });
    if (!raw.length) {
      if (mined > 0) break;
      throw new Error(`附近 64 格内没找到 ${name}`);
    }
    if (!candidates.length) {
      if (mined > 0) break;
      throw new Error(
        `附近的 ${name} 都在脚底下/被埋着——不要垂直向下挖，先走到露出来的地方`
      );
    }
    let block = null;
    const deadline = Date.now() + 15000;
    for (const candidate of candidates.slice(0, 6)) {
      if (Date.now() > deadline) break;
      try {
        await gotoWithTimeout(
          bot,
          new goals.GoalNear(
            candidate.position.x,
            candidate.position.y,
            candidate.position.z,
            2
          ),
          12,
          `走到 ${name}`
        );
        block = candidate;
        break;
      } catch (_) {
        /* 这棵走不到，试下一棵 */
      }
    }
    if (!block) {
      if (mined > 0) break;
      // 过不去 → 先自己挖路出洞/绕出去，再试一次（别把问题直接丢给用户）
      if (!escapedOnce) {
        escapedOnce = true;
        const escaped = await escapeTask(bot, task);
        if (escaped.escaped && !task.cancelled) {
          index -= 1;
          continue;
        }
      }
      throw new Error(
        `附近有 ${name} 但过不去（路被挡住了）——先用 escape 出洞，或者换个地方`
      );
    }
    if (task.cancelled) break;
    await bot.lookAt(block.position.offset(0.5, 0.5, 0.5), true);
    if (!bot.canDigBlock(block)) {
      try {
        await gotoWithTimeout(
          bot,
          new goals.GoalNear(block.position.x, block.position.y, block.position.z, 1),
          8
        );
      } catch (_) {
        /* 已经尽量靠近了 */
      }
      await bot.lookAt(block.position.offset(0.5, 0.5, 0.5), true);
    }
    if (!bot.canDigBlock(block)) {
      if (mined > 0) break;
      throw new Error(`现在挖不了 ${name}（太远或被挡住）`);
    }
    await equipBestTool(bot, block);
    try {
      await withTimeout(bot.dig(block, true), 15000, `挖掘 ${name} 超时`);
    } catch (_) {
      // 偶尔会被打断（被怪推 / 目标变了）→ 重试一次
      await sleep(400);
      await bot.lookAt(block.position.offset(0.5, 0.5, 0.5), true);
      try {
        await withTimeout(bot.dig(block, true), 15000, `挖掘 ${name} 超时`);
      } catch (error) {
        if (mined > 0) break;
        throw error;
      }
    }
    mined += 1;
    task.progress = { current: mined, target: count, unit: name };
    await sleep(300);
    // 走到方块旁边，等掉落物进背包
    try {
      await gotoWithTimeout(
        bot,
        new goals.GoalNear(block.position.x, block.position.y, block.position.z, 1),
        8
      );
    } catch (_) {
      /* 捡不到就算了，下一块更重要 */
    }
  }
  return {
    mined,
    block: name,
    tool: bot.heldItem ? bot.heldItem.name : null,
    requested: count,
    remaining: Math.max(0, count - mined),
    cancelled: Boolean(task.cancelled),
    ...(mined < count && !task.cancelled
      ? { warning: `只挖到 ${mined}/${count} 个 ${name}` }
      : {}),
    ...(needsPickaxe && !hasPickaxe
      ? { warning: '没有镐子：挖石头/矿石不会掉落，先做一把镐子' }
      : {}),
  };
}

async function placeTask(bot, args, task) {
  const name = pickBlockName(args);
  const x = Number(args && args.x);
  const y = Number(args && args.y);
  const z = Number(args && args.z);
  if (!name) throw new Error('place 需要 block（例如 {"block":"dirt","x":0,"y":64,"z":0}）');
  if (![x, y, z].every(Number.isFinite)) throw new Error('place 需要 x / y / z 数字');
  const item = bot.inventory.items().find((entry) => entry.name === name);
  if (!item) throw new Error(`[条件不满足] 背包里没有 ${name}`);
  const target = new Vec3(x, y, z);
  if (bot.blockAt(target) && bot.blockAt(target).boundingBox !== 'empty') {
    throw new Error('那个位置已经有方块了');
  }
  // 六个方向找一个"能贴着放"的参考方块（不只看正下方）
  const directions = [
    [new Vec3(0, -1, 0), new Vec3(0, 1, 0)],
    [new Vec3(0, 1, 0), new Vec3(0, -1, 0)],
    [new Vec3(1, 0, 0), new Vec3(-1, 0, 0)],
    [new Vec3(-1, 0, 0), new Vec3(1, 0, 0)],
    [new Vec3(0, 0, 1), new Vec3(0, 0, -1)],
    [new Vec3(0, 0, -1), new Vec3(0, 0, 1)],
  ];
  const references = [];
  for (const [offset, faceVector] of directions) {
    const block = bot.blockAt(target.plus(offset));
    if (block && block.boundingBox !== 'empty') {
      references.push({ reference: block, face: faceVector });
    }
  }
  if (!references.length) throw new Error('这个位置四面悬空，没有可依托的方块');
  await bot.equip(item, 'hand');
  await gotoWithTimeout(bot, new goals.GoalNear(x, y, z, 3), 10, '走到放置点');
  if (!(await ensureStandClear(bot, target))) {
    throw new Error('让开目标格失败：附近没有安全落脚点');
  }
  if (task.cancelled) return { placed: false, cancelled: true };
  const errors = [];
  for (const { reference, face } of references) {
    try {
      await bot.lookAt(target.plus(new Vec3(0.5, 0.5, 0.5)), true);
      await bot.placeBlock(reference, face);
      return { placed: name, x, y, z };
    } catch (error) {
      errors.push(String((error && error.message) || error));
      // 有时服务端已经放上了，但客户端等更新超时；先看目标格再决定。
      const current = bot.blockAt(target);
      if (current && current.name === name) {
        return { placed: name, x, y, z };
      }
      await sleep(70);
    }
  }
  throw new Error(errors[errors.length - 1] || '放块失败');
}

function standable(bot, point) {
  const ground = bot.blockAt(point.offset(0, -1, 0));
  const feet = bot.blockAt(point);
  const head = bot.blockAt(point.offset(0, 1, 0));
  const solidGround =
    ground &&
    ground.boundingBox !== 'empty' &&
    !/water|lava/.test(ground.name || '');
  const airFeet =
    !feet ||
    (feet.boundingBox === 'empty' && !/water|lava/.test(feet.name || ''));
  const airHead =
    !head ||
    (head.boundingBox === 'empty' && !/water|lava/.test(head.name || ''));
  return Boolean(solidGround && airFeet && airHead);
}

function playerBlocksTarget(bot, target) {
  const position = bot.entity && bot.entity.position;
  if (!position) return false;
  const fx = Math.floor(position.x);
  const fy = Math.floor(position.y);
  const fz = Math.floor(position.z);
  return (
    (target.x === fx || target.x === fx + 1) &&
    (target.z === fz || target.z === fz + 1) &&
    target.y >= fy &&
    target.y <= fy + 1
  );
}

async function ensureStandClear(bot, target) {
  if (!playerBlocksTarget(bot, target)) return true;
  const offsets = [
    [2, 0],
    [-2, 0],
    [0, 2],
    [0, -2],
    [2, 2],
    [-2, 2],
    [2, -2],
    [-2, -2],
    [1, 2],
    [-1, 2],
    [1, -2],
    [-1, -2],
  ];
  for (const dy of [0, 1, -1]) {
    for (const [dx, dz] of offsets) {
      const point = new Vec3(target.x + dx, target.y + dy, target.z + dz);
      if (playerBlocksTarget(bot, point)) continue;
      if (!standable(bot, point)) continue;
      try {
        await gotoWithTimeout(
          bot,
          new goals.GoalNear(point.x, point.y, point.z, 1),
          5,
          '让开目标格'
        );
        return true;
      } catch (_) {
        /* 换下一个落点 */
      }
    }
  }
  return false;
}

async function digAtTask(bot, args, task) {
  const x = Number(args && args.x);
  const y = Number(args && args.y);
  const z = Number(args && args.z);
  if (![x, y, z].every(Number.isFinite)) {
    throw new Error('dig_at 需要 x / y / z 数字');
  }
  const target = new Vec3(x, y, z);
  const block = bot.blockAt(target);
  if (!block || block.boundingBox === 'empty') {
    return { mined: null, cleared: true, x, y, z, note: '目标位置本来就是空的' };
  }
  if (/bedrock|barrier|end_portal|command_block/.test(block.name || '')) {
    throw new Error(`[条件不满足] ${block.name} 挖不掉`);
  }
  await gotoWithTimeout(bot, new goals.GoalNear(x, y, z, 2), 10, '走到清理点');
  if (task.cancelled) return { mined: null, cleared: false, cancelled: true };
  await bot.lookAt(target.offset(0.5, 0.5, 0.5), true);
  await equipBestTool(bot, block);
  if (!bot.canDigBlock(block)) {
    throw new Error(`现在挖不了 (${x},${y},${z}) 的 ${block.name}（太远或被挡住）`);
  }
  try {
    await withTimeout(bot.dig(block, true), 15000, `清理 ${block.name} 超时`);
  } catch (error) {
    // 服务端可能已经清掉但客户端超时；先看真实状态再决定。
    await sleep(250);
    const current = bot.blockAt(target);
    if (current && current.boundingBox === 'empty') {
      return { mined: block.name, cleared: true, x, y, z };
    }
    throw error;
  }
  await sleep(200);
  const current = bot.blockAt(target);
  return {
    mined: block.name,
    cleared: !current || current.boundingBox === 'empty',
    x,
    y,
    z,
  };
}

async function pillarTask(bot, args, task) {
  const name = pickBlockName(args);
  const item = name
    ? bot.inventory.items().find((entry) => entry.name === name)
    : bot.inventory.items().find((entry) =>
        ['dirt', 'cobblestone', 'stone', 'oak_planks'].includes(entry.name)
      );
  if (!item) {
    throw new Error('[条件不满足] 背包里没有可以垫的方块（泥土 / 圆石 / 木板）');
  }
  const count = Math.max(1, Math.min(Number((args && args.count) || 1), 16));
  let placed = 0;
  task.progress = { current: 0, target: count, unit: name || item.name };
  for (let index = 0; index < count; index += 1) {
    if (task.cancelled) break;
    await bot.equip(item, 'hand');
    const base = bot.blockAt(bot.entity.position.offset(0, -1, 0));
    if (!base || base.boundingBox === 'empty') throw new Error('脚下没有可依托的方块');
    await bot.lookAt(base.position.offset(0.5, 1, 0.5), true);
    bot.setControlState('jump', true);
    await sleep(300);
    try {
      await bot.placeBlock(base, new Vec3(0, 1, 0));
      placed += 1;
      task.progress = {
        current: placed,
        target: count,
        unit: name || item.name,
      };
    } catch (_) {
      /* 时机差一点就少垫一格，继续 */
    }
    bot.setControlState('jump', false);
    await sleep(250);
  }
  return {
    placed,
    requested: count,
    remaining: Math.max(0, count - placed),
  };
}

async function attackTask(bot, args, task) {
  const wanted = pickAttackTarget(args);
  const count = Math.max(1, Math.min(Number((args && args.count) || 1), 20));
  const deadline = Date.now() + Math.max(30000, count * 20000);
  let killed = 0;
  task.progress = { current: 0, target: count, unit: wanted || 'hostile' };
  try {
    while (killed < count && Date.now() < deadline && !task.cancelled) {
      const entity = findHostile(bot, wanted);
      if (!entity) {
        if (killed > 0) break;
        throw new Error(
          wanted
            ? `[条件不满足] 附近没找到 ${wanted}`
            : '[条件不满足] 附近没有敌对生物'
        );
      }
      bot.pathfinder.setMovements(makeMovements(bot));
      bot.pathfinder.setGoal(new goals.GoalFollow(entity, 1.5), true);
      while (Date.now() < deadline && !task.cancelled && entity.isValid) {
        if (entity.position.distanceTo(bot.entity.position) <= 3) bot.attack(entity);
        await sleep(120);
      }
      if (entity.isValid) break;
      killed += 1;
      task.progress = {
        current: killed,
        target: count,
        unit: wanted || 'hostile',
      };
      await sleep(200);
    }
  } finally {
    try {
      bot.pathfinder.setGoal(null);
    } catch (_) {
      /* ignore */
    }
  }
  return {
    killed,
    target: wanted || 'hostile',
    requested: count,
    remaining: Math.max(0, count - killed),
    cancelled: Boolean(task.cancelled),
  };
}

async function eatTask(bot) {
  if (Number(bot.food) >= 20) return { ate: false, reason: '不饿' };
  const food = foodInInventory(bot, dataFor(bot));
  if (!food) {
    throw new Error(
      '[条件不满足] 背包里没有能吃的食物——先去找/合成食物，别一直重试 eat'
    );
  }
  await bot.equip(food, 'hand');
  await bot.consume();
  return { ate: true, item: food.name };
}

async function flyTask(bot, args) {
  const mode = String((bot.game && bot.game.gameMode) || '');
  if (mode !== 'creative' && mode !== 'spectator') {
    throw new Error('[条件不满足] 当前不是创造/旁观模式，不能飞');
  }
  if (!bot.creative) throw new Error('[条件不满足] 适配器没有加载 creative 控制');
  const x = Number(args && args.x);
  const y = Number(args && args.y);
  const z = Number(args && args.z);
  if ([x, y, z].every(Number.isFinite)) {
    await bot.creative.flyTo(new Vec3(x, y, z));
    return { flying: true, x, y, z };
  }
  if (args && args.enabled === false) {
    await bot.creative.stopFlying();
    return { flying: false };
  }
  await bot.creative.startFlying();
  return { flying: true };
}

async function fishTask(bot, args) {
  const rod = bot.inventory.items().find((item) => item.name === 'fishing_rod');
  if (!rod) throw new Error('[条件不满足] 背包里没有鱼竿');
  if (typeof bot.fish !== 'function') {
    throw new Error('[条件不满足] 当前适配器版本不支持钓鱼');
  }
  await bot.equip(rod, 'hand');
  const timeout = Math.max(3000, Math.min(Number((args && args.timeout) || 25000), 60000));
  const result = await withTimeout(bot.fish(), timeout, '钓鱼超时');
  return {
    caught: result && (result.name || result.displayName) || null,
    count: result ? Number(result.count || 1) : 0,
    timed_out: false,
  };
}

async function shootTask(bot, args, task) {
  const bow = bot.inventory
    .items()
    .find((item) => item.name === 'bow' || item.name === 'crossbow');
  if (!bow) throw new Error('[条件不满足] 背包里没有弓 / 弩');
  const creative = String((bot.game && bot.game.gameMode) || '') === 'creative';
  if (!creative && !bot.inventory.items().some((item) => /arrow$/.test(item.name))) {
    throw new Error('[条件不满足] 没有箭');
  }
  const wanted = String((args && (args.target || args.name)) || '').toLowerCase();
  let targetEntity = null;
  if (wanted) {
    targetEntity = bot.nearestEntity(
      (entity) =>
        entity &&
        entity.type !== 'item' &&
        String(entity.username || entity.name || '').toLowerCase().includes(wanted)
    );
  } else {
    targetEntity = bot.nearestEntity(
      (entity) =>
        entity &&
        entity.type === 'mob' &&
        isHostile(entity.name)
    );
  }
  const x = Number(args && args.x);
  const y = Number(args && args.y);
  const z = Number(args && args.z);
  let aim = null;
  if (targetEntity && targetEntity.position) {
    aim = targetEntity.position.offset(
      0,
      Number(targetEntity.height || 1.5) * 0.65,
      0
    );
  } else if ([x, y, z].every(Number.isFinite)) {
    aim = new Vec3(x, y, z);
  }
  if (!aim) throw new Error('[条件不满足] 没找到射击目标');
  const distance = bot.entity.position.distanceTo(aim);
  if (distance > 40) throw new Error(`[条件不满足] 目标太远（${distance.toFixed(1)} 格）`);
  await bot.equip(bow, 'hand');
  // 简易抛射补偿：越远抬得越高；近距离几乎平射。
  const lift = Math.min(5, 0.12 * distance + 0.01 * distance * distance);
  await bot.lookAt(aim.offset(0, lift, 0), true);
  const charge = Math.max(
    300,
    Math.min(Number((args && args.charge_ms) || (bow.name === 'crossbow' ? 1200 : 900)), 2000)
  );
  await bot.activateItem();
  await sleep(charge);
  if (task.cancelled) {
    await bot.deactivateItem();
    return { shot: false, cancelled: true };
  }
  await bot.deactivateItem();
  await sleep(350);
  return {
    shot: true,
    target:
      (targetEntity && (targetEntity.username || targetEntity.name)) ||
      `(${aim.x},${aim.y},${aim.z})`,
    distance: Number(distance.toFixed(1)),
    weapon: bow.name,
  };
}

async function boatTask(bot, args, task) {
  const boat = bot.inventory.items().find((item) => /_boat$/.test(item.name));
  if (!boat) throw new Error('[条件不满足] 背包里没有船');
  await bot.equip(boat, 'hand');
  let water = null;
  const x = Number(args && args.x);
  const y = Number(args && args.y);
  const z = Number(args && args.z);
  if ([x, y, z].every(Number.isFinite)) {
    const block = bot.blockAt(new Vec3(x, y, z));
    if (block && /water/.test(block.name || '')) water = block;
  }
  if (!water) {
    water = bot.findBlock({
      matching: (block) => Boolean(block && /water/.test(block.name || '')),
      maxDistance: 12,
    });
  }
  if (!water) throw new Error('[条件不满足] 附近 12 格内没有水面');
  await gotoWithTimeout(
    bot,
    new goals.GoalNear(water.position.x, water.position.y, water.position.z, 3),
    12,
    '走到水边'
  );
  if (task.cancelled) return { placed: false, cancelled: true };
  await bot.lookAt(water.position.offset(0.5, 1, 0.5), true);
  try {
    await bot.placeBlock(water, new Vec3(0, 1, 0));
  } catch (_) {
    await bot.activateItem();
    await sleep(150);
    await bot.deactivateItem();
  }
  await sleep(600);
  const entity = bot.nearestEntity(
    (item) => item && /boat/.test(String(item.name || '')) && item.position
  );
  if (entity) {
    await bot.mount(entity);
    return { placed: boat.name, mounted: true };
  }
  return { placed: boat.name, mounted: false };
}

async function mountTask(bot, args) {
  const wanted = String((args && (args.target || args.name)) || '').toLowerCase();
  const entity = bot.nearestEntity((item) => {
    if (!item || !item.position) return false;
    const name = String(item.username || item.name || '').toLowerCase();
    if (wanted) return name.includes(wanted);
    return /boat|minecart|horse|donkey|mule|llama|pig|strider/.test(name);
  });
  if (!entity) throw new Error('[条件不满足] 附近没有可骑的载具 / 生物');
  await bot.mount(entity);
  return { mounted: true, target: entity.username || entity.name || entity.type };
}

async function dismountTask(bot) {
  if (!bot.vehicle) return { mounted: false, note: '当前没有骑任何东西' };
  await bot.dismount();
  return { mounted: false };
}

async function useItemTask(bot, args) {
  if (!bot.heldItem) throw new Error('[条件不满足] 手上没有物品');
  const hold = Math.max(50, Math.min(Number((args && args.hold_ms) || 200), 2000));
  await bot.activateItem();
  await sleep(hold);
  await bot.deactivateItem();
  return { used: bot.heldItem.name };
}

async function dropTask(bot, args) {
  const name = pickBlockName(args);
  if (!name) throw new Error('drop 需要 item（例如 {"item":"dirt","count":1}）');
  const item = bot.inventory.items().find((entry) => entry.name === name);
  if (!item) throw new Error(`[条件不满足] 背包里没有 ${name}`);
  const count = Math.max(
    1,
    Math.min(Number((args && args.count) || item.count), item.count)
  );
  await bot.toss(item.type, null, count);
  return { dropped: name, count };
}

async function bringTask(bot, args, task) {
  const name = pickBlockName(args);
  if (!name) throw new Error('bring 需要 item（例如 {"item":"dirt","count":1}）');
  const item = bot.inventory.items().find((entry) => entry.name === name);
  if (!item) throw new Error(`[条件不满足] 背包里没有 ${name}`);
  const target = resolveFollowTarget(bot, args && args.player);
  if (!target) {
    throw new Error('[条件不满足] 没找到要把东西交给谁——你先走进她的视野');
  }
  const count = Math.max(
    1,
    Math.min(Number((args && args.count) || item.count), item.count)
  );
  bot.pathfinder.setMovements(makeMovements(bot));
  bot.pathfinder.setGoal(new goals.GoalFollow(target.entity, 2), true);
  const deadline = Date.now() + 30000;
  try {
    while (Date.now() < deadline && !task.cancelled) {
      if (target.entity.position.distanceTo(bot.entity.position) <= 3) break;
      await sleep(200);
    }
    if (task.cancelled) return { brought: false, cancelled: true };
    if (target.entity.position.distanceTo(bot.entity.position) > 3.5) {
      throw new Error('走不到你身边（路被挡住了）');
    }
    await bot.toss(item.type, null, count);
  } finally {
    try {
      bot.pathfinder.setGoal(null);
    } catch (_) {
      /* ignore */
    }
  }
  return { brought: name, count, to: target.username };
}

async function escapeTask(bot, task) {
  // 被卡住 / 困在洞里：向上挖一条 1×1 的路，而不是原地跳。
  // 关键：**必须验证真的上去了**（以前挖到一块就报成功，人还在坑里）。
  const startY = bot.entity.position.y;
  let tries = 0;
  for (let index = 0; index < 12; index += 1) {
    if (task.cancelled) return { escaped: false, tries, cancelled: true };
    tries = index + 1;
    const position = bot.entity.position.floored();
    let dug = false;
    for (const block of [
      bot.blockAt(position.offset(0, 2, 0)),
      bot.blockAt(position.offset(0, 1, 0)),
    ]) {
      if (block && block.boundingBox !== 'empty' && bot.canDigBlock(block)) {
        try {
          await withTimeout(bot.dig(block, true), 10000, '脱困挖掘超时');
          dug = true;
        } catch (_) {
          /* 换下一块 */
        }
      }
    }
    bot.setControlState('jump', true);
    await sleep(500);
    bot.setControlState('jump', false);
    await sleep(250);
    if (bot.entity.position.y > startY + 0.8) {
      return { escaped: true, tries, mode: 'dig' };
    }
  }

  // 挖不上去 → 用背包里的方块垫上去
  const building = bot.inventory.items().find((item) =>
    ['dirt', 'cobblestone', 'stone', 'oak_planks', 'deepslate'].includes(item.name)
  );
  if (building) {
    try {
      const result = await pillarTask(
        bot,
        { block: building.name, count: 8 },
        task
      );
      if (bot.entity.position.y > startY + 0.8) {
        return { escaped: true, tries, mode: 'pillar', placed: result.placed };
      }
    } catch (_) {
      /* 垫不上去就如实失败 */
    }
  }
  throw new Error(
    '[条件不满足] 上不去：挖了 12 次仍然没离开原位，也没有能垫脚的方块；先找路或先准备方块'
  );
}

async function holdTask(bot, task, args, kind) {
  const seconds = clampSeconds(args && args.seconds, 30, 600);
  state.followTarget = null;
  try {
    bot.pathfinder.setGoal(null);
  } catch (_) {
    /* 还没加载寻路也没关系 */
  }
  const sneaking = kind === 'rest';
  if (sneaking) {
    try {
      bot.setControlState('sneak', true);
    } catch (_) {
      /* 某些版本不支持就只站着 */
    }
  }
  task.ends_at = Date.now() + seconds * 1000;
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline && !task.cancelled) {
    await sleep(200);
  }
  if (sneaking) {
    try {
      bot.setControlState('sneak', false);
    } catch (_) {
      /* ignore */
    }
  }
  return {
    [kind === 'wait' ? 'waited' : 'rested']: seconds,
    cancelled: Boolean(task.cancelled),
  };
}

async function patrolTask(bot, args, task) {
  const seconds = clampSeconds(args && args.seconds, 6, 30);
  state.followTarget = null;
  const origin = bot.entity.position.floored();
  task.ends_at = Date.now() + seconds * 1000;
  const deadline = Date.now() + seconds * 1000;
  let moves = 0;
  const movements = makeMovements(bot);
  movements.canDig = false;
  try {
    bot.pathfinder.setMovements(movements);
  } catch (_) {
    /* 寻路不可用时下面会转视角 */
  }

  try {
    while (Date.now() < deadline && !task.cancelled) {
      const candidates = [];
      for (let index = 0; index < 8; index += 1) {
        const angle = Math.random() * Math.PI * 2;
        const distance = 1 + Math.random() * 2.5;
        const x = Math.floor(origin.x + Math.cos(angle) * distance);
        const z = Math.floor(origin.z + Math.sin(angle) * distance);
        const ground = bot.blockAt(new Vec3(x, origin.y - 1, z));
        const feet = bot.blockAt(new Vec3(x, origin.y, z));
        const head = bot.blockAt(new Vec3(x, origin.y + 1, z));
        if (!ground || ground.boundingBox === 'empty' || /water|lava/.test(ground.name)) {
          continue;
        }
        if (feet && feet.boundingBox !== 'empty') continue;
        if (head && head.boundingBox !== 'empty') continue;
        candidates.push({ x, y: origin.y, z });
      }
      if (candidates.length > 0) {
        const target = candidates[Math.floor(Math.random() * candidates.length)];
        try {
          await gotoWithTimeout(
            bot,
            new goals.GoalNear(target.x, target.y, target.z, 1),
            4,
            '待机巡逻'
          );
          moves += 1;
        } catch (_) {
          /* 这一步走不到就换个方向 */
        }
      } else {
        try {
          await bot.look(bot.entity.yaw + 0.9, 0, true);
        } catch (_) {
          /* ignore */
        }
        await sleep(350);
      }
      await sleep(150);
    }
  } finally {
    try {
      bot.pathfinder.setGoal(null);
    } catch (_) {
      /* ignore */
    }
  }
  return { patrolled: seconds, moves, cancelled: Boolean(task.cancelled) };
}

async function craftTask(bot, args, task) {
  const name = pickBlockName(args);
  if (!name) throw new Error('craft 需要 item（例如 {"item":"oak_planks","count":4}）');
  const data = dataFor(bot);
  const item = data.itemsByName[name];
  if (!item) throw new Error(`不认识的物品：${name}`);
  const count = Math.max(1, Math.min(Number((args && args.count) || 1), 64));
  task.progress = { current: 0, target: count, unit: name };
  let table = null;
  try {
    table = bot.findBlock({
      matching: (block) => Boolean(block && block.name.includes('crafting_table')),
      maxDistance: 8,
    });
  } catch (_) {
    table = null;
  }
  let recipes = bot.recipesFor(item.id, null, 1, table || null);
  if (!recipes.length && table) {
    recipes = bot.recipesFor(item.id, null, 1, null);
  }
  if (!recipes.length) {
    throw new Error(
      `[条件不满足] 现在做不了 ${name}（材料不够，或者需要工作台）——` +
        '先 craft 一个 crafting_table，再 place 它'
    );
  }
  if (table) {
    bot.pathfinder.setMovements(makeMovements(bot));
    await gotoWithTimeout(
      bot,
      new goals.GoalNear(table.position.x, table.position.y, table.position.z, 2),
      10,
      '走到工作台'
    );
  }
  const before = bot.inventory
    .items()
    .filter((entry) => entry.name === name)
    .reduce((total, entry) => total + entry.count, 0);
  await Promise.race([
    bot.craft(recipes[0], count, table || null),
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error('合成超时（工作台 / 材料？）')), 30000)
    ),
  ]);
  await sleep(300);
  const inventory = bot.inventory.items().map((entry) => ({
    item: entry.name,
    count: entry.count,
  }));
  const after = inventory
    .filter((entry) => entry.item === name)
    .reduce((total, entry) => total + entry.count, 0);
  const craftedCount = Math.max(0, after - before);
  task.progress = { current: craftedCount, target: count, unit: name };
  return {
    crafted: name,
    crafted_count: craftedCount,
    requested: count,
    remaining: Math.max(0, count - craftedCount),
    inventory,
    ...(craftedCount <= 0
      ? { warning: '背包里没看到新合成的物品（材料 / 工作台？）' }
      : {}),
  };
}

async function equipTask(bot, args) {
  const name = pickBlockName(args);
  if (!name) throw new Error('equip 需要 item（例如 {"item":"stone_pickaxe"}）');
  const item = bot.inventory.items().find((entry) => entry.name === name);
  if (!item) throw new Error(`[条件不满足] 背包里没有 ${name}`);
  await bot.equip(item, 'hand');
  return { equipped: name };
}

async function smeltTask(bot, args, task) {
  const name = pickBlockName(args);
  if (!name) throw new Error('smelt 需要 item（例如 {"item":"iron_ore","count":1}）');
  const data = dataFor(bot);
  const item = data.itemsByName[name];
  if (!item) throw new Error(`不认识的物品：${name}`);
  const count = Math.max(1, Math.min(Number((args && args.count) || 1), 64));
  task.progress = { current: 0, target: count, unit: name };
  const input = bot.inventory.items().find((entry) => entry.name === name);
  if (!input) throw new Error(`[条件不满足] 背包里没有 ${name}`);
  const fuelNames = [
    'coal',
    'charcoal',
    'oak_planks',
    'spruce_planks',
    'birch_planks',
    'oak_log',
    'stick',
  ];
  const fuel = bot.inventory.items().find((entry) => fuelNames.includes(entry.name));
  if (!fuel) {
    throw new Error('[条件不满足] 没有燃料（煤 / 木炭 / 木板 / 木头）');
  }
  const furnaceBlock = bot.findBlock({
    matching: (block) => Boolean(block && block.name.includes('furnace')),
    maxDistance: 8,
  });
  if (!furnaceBlock) {
    throw new Error('[条件不满足] 附近没有熔炉——先 craft 一个 furnace，再 place 它');
  }
  bot.pathfinder.setMovements(makeMovements(bot));
  await gotoWithTimeout(
    bot,
    new goals.GoalNear(
      furnaceBlock.position.x,
      furnaceBlock.position.y,
      furnaceBlock.position.z,
      2
    ),
    10,
    '走到熔炉'
  );
  const furnace = await bot.openFurnace(furnaceBlock);
  try {
    await furnace.putFuel(fuel.type, null, Math.max(1, Math.ceil(count / 8)));
    await furnace.putInput(input.type, null, count);
    const deadline = Date.now() + 30000;
    let output = null;
    while (Date.now() < deadline) {
      output = furnace.outputItem();
      if (output) break;
      await sleep(500);
    }
    if (!output) throw new Error('等不到烧炼结果（燃料 / 材料够吗？）');
    const taken = await furnace.takeOutput();
    const smeltedCount = taken ? taken.count : output.count;
    task.progress = { current: smeltedCount, target: count, unit: name };
    return {
      smelted: name,
      smelted_count: smeltedCount,
      requested: count,
      remaining: Math.max(0, count - smeltedCount),
    };
  } finally {
    bot.closeWindow(furnace);
  }
}

function taskDone(taskId, result, ok = true) {
  // sequence 内部：把子动作结果收集起来，不为每个小动作发 task_done
  if (state.collectingTaskIds.has(taskId)) {
    state.collectedResults.set(taskId, result || {});
    return;
  }
  const task = state.tasks.get(taskId);
  if (task) task.status = ok ? 'done' : 'failed';
  if (state.activeMoveTaskId === taskId) {
    state.activeMoveTaskId = null;
    state.activeMoveTaskAction = null;
  }
  emit({ type: 'task_done', task_id: taskId, ok, result: result || {} });
}

function taskFailed(taskId, message) {
  const task = state.tasks.get(taskId);
  if (task) task.status = 'failed';
  if (state.activeMoveTaskId === taskId) {
    state.activeMoveTaskId = null;
    state.activeMoveTaskAction = null;
  }
  emit({ type: 'task_done', task_id: taskId, ok: false, result: { error: String(message) } });
}

async function runCommand(action, args, taskId, task) {
  const bot = state.bot;
  if (!bot) throw new Error('还没有进服');

  if (['goto', 'bring', 'escape'].includes(action)) {
    state.activeMoveTaskId = taskId;
    state.activeMoveTaskAction = action;
    state.lastPosition = bot.entity && bot.entity.position ? bot.entity.position.clone() : null;
    state.lastMovedAt = Date.now();
  }

  if (action === 'sequence') {
    const steps = Array.isArray(args && args.steps) ? args.steps : [];
    if (!steps.length) throw new Error('sequence 需要 steps 数组');
    const results = [];
    for (let index = 0; index < steps.length; index += 1) {
      if (task.cancelled) break;
      const step = steps[index] || {};
      const subAction = String(step.action || '');
      if (!ACTIONS[subAction]) {
        results.push({ action: subAction, ok: false, error: '没有这个动作' });
        taskDone(taskId, { steps: results, failed: subAction, error: '没有这个动作' }, false);
        return;
      }
      if (['follow', 'mode', 'stop', 'sequence', 'patrol'].includes(subAction)) {
        results.push({
          action: subAction,
          ok: false,
          error: '不能放进 sequence（持续动作 / 嵌套）',
        });
        taskDone(
          taskId,
          { steps: results, failed: subAction, error: '不能放进 sequence（持续动作 / 嵌套）' },
          false
        );
        return;
      }
      const subTaskId = `${taskId}::${index}`;
      state.collectingTaskIds.add(subTaskId);
      state.collectedResults.delete(subTaskId);
      try {
        await runCommand(subAction, step.args || {}, subTaskId, task);
        results.push({
          action: subAction,
          ok: true,
          result: state.collectedResults.get(subTaskId) || {},
        });
      } catch (error) {
        results.push({
          action: subAction,
          ok: false,
          error: String((error && error.message) || error),
        });
        state.collectingTaskIds.delete(subTaskId);
        taskDone(
          taskId,
          {
            steps: results,
            failed: subAction,
            error: String((error && error.message) || error),
          },
          false
        );
        return;
      } finally {
        state.collectingTaskIds.delete(subTaskId);
        state.collectedResults.delete(subTaskId);
      }
    }
    taskDone(taskId, { steps: results, cancelled: Boolean(task.cancelled) });
    return;
  }

  if (action === 'command') {
    const raw = (args && (args.commands || args.command)) || [];
    const items = Array.isArray(raw) ? raw : [raw];
    const commands = items
      .map((item) => String(item || '').trim().replace(/^\//, ''))
      .filter(Boolean);
    if (commands.length === 0) throw new Error('[格式错误] command 需要 commands / command');
    for (const command of commands) {
      bot.chat(`/${command}`);
      await sleep(220);
    }
    taskDone(taskId, { commands });
    return;
  }

  if (action === 'inspect_blocks') {
    const positions = Array.isArray(args && args.positions) ? args.positions : [];
    if (!positions.length) throw new Error('[格式错误] inspect_blocks 需要 positions');
    const blocks = positions.slice(0, 4096).map((item) => {
      const x = Number(item && item.x);
      const y = Number(item && item.y);
      const z = Number(item && item.z);
      if (![x, y, z].every(Number.isFinite)) {
        return { x: item && item.x, y: item && item.y, z: item && item.z, name: null };
      }
      const block = bot.blockAt(new Vec3(x, y, z));
      return {
        x,
        y,
        z,
        name: block ? block.name : null,
      };
    });
    taskDone(taskId, { blocks });
    return;
  }

  if (action === 'survey_area') {
    const radius = Math.max(8, Math.min(128, Number((args && args.radius) || 64)));
    const step = Math.max(2, Math.min(16, Number((args && args.step) || 4)));
    const map = buildTerrainMap(bot, { radius, step, verticalSpan: 16, cacheMs: 4000 });
    if (!map) throw new Error('拿不到玩家位置，无法测绘');
    taskDone(taskId, map);
    return;
  }

  if (action === 'knowledge_query') {
    const query = String((args && args.query) || '').trim();
    const name = String((args && args.name) || '').trim();
    if (!query || !name) {
      throw new Error('[格式错误] knowledge_query 需要 query + name');
    }
    const mcData = minecraftData(bot.version);
    const result =
      query === 'recipe_for'
        ? recipeFor(mcData, name)
        : query === 'tool_tier_for'
          ? toolTierFor(mcData, name)
          : { ok: false, error: `不认识的 knowledge query：${query}` };
    taskDone(taskId, result);
    return;
  }

  if (action === 'plugin_list') {
    taskDone(taskId, { plugins: describePlugins(state.plugins) });
    return;
  }

  if (action === 'plugin_call') {
    const result = await callPlugin(
      state.plugins,
      String((args && args.plugin) || ''),
      String((args && args.action) || ''),
      (args && args.args) || {},
      { bot, mcData: minecraftData(bot.version) }
    );
    taskDone(taskId, result, Boolean(result.ok));
    return;
  }

  if (action === 'say') {
    const text = String((args && args.text) || '').slice(0, 200);
    if (!text) throw new Error('say 需要 text');
    bot.chat(text);
    taskDone(taskId, { said: text });
    return;
  }

  if (action === 'follow') {
    const target = resolveFollowTarget(bot, args && args.player);
    if (!target) {
      throw new Error('附近没找到玩家 —— 你先走进她的视野，或者告诉我玩家名');
    }
    state.followTarget = target.username;
    state.mode = 'follow';
    bot.pathfinder.setMovements(makeMovements(bot));
    bot.pathfinder.setGoal(new goals.GoalFollow(target.entity, 2), true);
    // follow 是长任务：不 taskDone，等 cancel / stop
    return;
  }

  if (action === 'goto') {
    const x = Number(args && args.x);
    const y = Number(args && args.y);
    const z = Number(args && args.z);
    if (![x, y, z].every(Number.isFinite)) throw new Error('goto 需要 x / y / z 数字');
    bot.pathfinder.setMovements(makeMovements(bot));
    await gotoWithTimeout(bot, new goals.GoalBlock(x, y, z), 20, '走到坐标');
    taskDone(taskId, { arrived: true, x, y, z });
    return;
  }

  if (action === 'stop') {
    bot.pathfinder.setGoal(null);
    state.followTarget = null;
    state.mode = 'free';
    taskDone(taskId, { stopped: true });
    return;
  }

  if (action === 'mine') {
    taskDone(taskId, await mineTask(bot, args, task));
    return;
  }

  if (action === 'place') {
    taskDone(taskId, await placeTask(bot, args, task));
    return;
  }

  if (action === 'dig_at') {
    taskDone(taskId, await digAtTask(bot, args, task));
    return;
  }

  if (action === 'fly') {
    taskDone(taskId, await flyTask(bot, args));
    return;
  }

  if (action === 'fish') {
    taskDone(taskId, await fishTask(bot, args));
    return;
  }

  if (action === 'shoot') {
    taskDone(taskId, await shootTask(bot, args, task));
    return;
  }

  if (action === 'boat') {
    taskDone(taskId, await boatTask(bot, args, task));
    return;
  }

  if (action === 'mount') {
    taskDone(taskId, await mountTask(bot, args));
    return;
  }

  if (action === 'dismount') {
    taskDone(taskId, await dismountTask(bot));
    return;
  }

  if (action === 'use_item') {
    taskDone(taskId, await useItemTask(bot, args));
    return;
  }

  if (action === 'pillar') {
    taskDone(taskId, await pillarTask(bot, args, task));
    return;
  }

  if (action === 'craft') {
    taskDone(taskId, await craftTask(bot, args, task));
    return;
  }

  if (action === 'smelt') {
    taskDone(taskId, await smeltTask(bot, args, task));
    return;
  }

  if (action === 'equip') {
    taskDone(taskId, await equipTask(bot, args));
    return;
  }

  if (action === 'attack') {
    taskDone(taskId, await attackTask(bot, args, task));
    return;
  }

  if (action === 'eat') {
    taskDone(taskId, await eatTask(bot));
    return;
  }

  if (action === 'drop') {
    taskDone(taskId, await dropTask(bot, args));
    return;
  }

  if (action === 'bring') {
    taskDone(taskId, await bringTask(bot, args, task));
    return;
  }

  if (action === 'escape') {
    taskDone(taskId, await escapeTask(bot, task));
    return;
  }

  if (action === 'rest') {
    taskDone(taskId, await holdTask(bot, task, args, 'rest'));
    return;
  }

  if (action === 'wait') {
    taskDone(taskId, await holdTask(bot, task, args, 'wait'));
    return;
  }

  if (action === 'patrol') {
    taskDone(taskId, await patrolTask(bot, args, task));
    return;
  }

  throw new Error(`不认识的动作：${action}`);
}

function attachRuntimeListeners(bot) {
  bot.on('message', (jsonMsg, position, sender) => {
    // 只把**玩家聊天**当成 TA 说话；system / game_info（命令反馈、系统提示）不算。
    if (position !== 'chat') return;
    const rendered = jsonMsg.toString();
    const match = /^<([^>]+)>\s([\s\S]*)$/.exec(rendered);
    let username = null;
    if (sender) {
      const hit = Object.values(bot.players || {}).find((player) => player.uuid === sender);
      if (hit) username = hit.username;
    }
    if (!username && match) username = match[1];
    if (!username || username === bot.username) return;
    emit({ type: 'chat', from: username, text: match ? match[2] : rendered });
  });

  bot.on('death', () => {
    emit({ type: 'death', cause: 'game' });
  });

  bot.on('health', () => {
    const health = Number(bot.health);
    if (Number.isFinite(health) && health < state.lastHealth) {
      const hostile = nearestHostile(bot, 6);
      let source = null;
      if (hostile) {
        const view = entityView(bot, hostile);
        source = {
          name: view.name,
          distance: view.distance,
          direction: view.direction,
          vertical: view.vertical,
          inferred: true,
        };
      }
      emit({
        type: 'damage',
        amount: state.lastHealth - health,
        health,
        source,
      });
    }
    if (Number.isFinite(health)) state.lastHealth = health;
  });

  bot.on('kicked', (reason) => {
    state.status = 'crashed';
    stopReflex();
    emit({ type: 'error', message: `被服务器踢了：${JSON.stringify(reason)}` });
  });

  bot.on('error', (error) => {
    emit({ type: 'error', message: String((error && error.message) || error) });
  });

  bot.on('end', (reason) => {
    if (state.stopping) return;
    if (state.status === 'running' || state.status === 'starting') {
      state.status = 'crashed';
      stopReflex();
      emit({ type: 'error', message: `连接结束了：${reason || 'unknown'}` });
    }
  });
}

function startHeartbeat() {
  stopHeartbeat();
  if (!(cli.heartbeat > 0)) return;
  state.heartbeatTimer = setInterval(() => emit({ type: 'heartbeat' }), cli.heartbeat * 1000);
}

function stopHeartbeat() {
  if (state.heartbeatTimer) {
    clearInterval(state.heartbeatTimer);
    state.heartbeatTimer = null;
  }
}

function startReflex() {
  stopReflex();
  if (!cli.reflex) return;
  state.lastPosition = null;
  state.lastMovedAt = Date.now();
  state.stuckTries = 0;
  state.reflexTimer = setInterval(() => {
    reflexTick().catch(() => {});
  }, 250);
}

function stopReflex() {
  if (state.reflexTimer) {
    clearInterval(state.reflexTimer);
    state.reflexTimer = null;
  }
}

// D323：只上报“附近有人/敌对生物”的事实变化，不替她做动作。
// 玩家进入 16 格、敌对生物进入 12 格时各报一次；离开更大范围时再报一次离开。
function startProximityTracking() {
  stopProximityTracking();
  state.nearPlayers.clear();
  state.nearHostiles.clear();
  state.proximityTimer = setInterval(() => {
    proximityTick().catch(() => {});
  }, 1000);
}

function stopProximityTracking() {
  if (state.proximityTimer) {
    clearInterval(state.proximityTimer);
    state.proximityTimer = null;
  }
  state.nearPlayers.clear();
  state.nearHostiles.clear();
}

async function proximityTick() {
  const bot = state.bot;
  if (!bot || state.status !== 'running' || !bot.entity) return;
  const players = new Map();
  const hostiles = new Map();
  for (const entity of Object.values(bot.entities || {})) {
    if (!entity || entity === bot.entity || !entity.position) continue;
    if (entity.type === 'player') {
      const view = entityView(bot, entity);
      const name = String(view.name || '');
      if (!name || view.distance === null) continue;
      if (view.distance <= 24) players.set(name, view.distance);
      const previous = state.nearPlayers.get(name);
      if (view.distance <= 16 && previous === undefined) {
        emit({
          type: 'player_near',
          player: name,
          distance: view.distance,
          direction: view.direction,
          vertical: view.vertical,
        });
      } else if (view.distance > 24 && previous !== undefined) {
        emit({ type: 'player_far', player: name, distance: view.distance });
      }
      continue;
    }
    if (entity.type === 'mob' && isHostile(entity.name)) {
      const view = entityView(bot, entity);
      if (view.distance === null) continue;
      if (view.distance <= 18) hostiles.set(view.name, view.distance);
      const previous = state.nearHostiles.get(view.name);
      if (view.distance <= 12 && previous === undefined) {
        emit({
          type: 'hostile_near',
          name: view.name,
          distance: view.distance,
          direction: view.direction,
          vertical: view.vertical,
        });
      } else if (view.distance > 18 && previous !== undefined) {
        emit({ type: 'hostile_far', name: view.name, distance: view.distance });
      }
    }
  }
  state.nearPlayers = players;
  state.nearHostiles = hostiles;
}

async function reflexTick() {
  const bot = state.bot;
  if (!bot || state.status !== 'running' || state.reflexBusy || !bot.entity) return;
  state.reflexBusy = true;
  try {
    const now = Date.now();
    const position = bot.entity.position;
    if (state.lastPosition && hasMoved(state.lastPosition, position)) {
      state.lastMovedAt = now;
    }
    state.lastPosition = position.clone();

    const selfConfig = state.reflexConfig.modes
      ? state.reflexConfig.modes.self_preservation
      : null;
    if (selfConfig && selfConfig.enabled) {
      const hasIncident = await incidentTick(bot, selfConfig, now);
      if (hasIncident) return;
    }

    for (const [mode, config] of orderedModes(state.reflexConfig)) {
      const last = Number(state.reflexLastRun[mode] || 0);
      if (now - last < Number(config.cooldown || 0) * 1000) continue;
      const result = await runReflexMode(bot, mode, config, now);
      if (!result) continue;
      state.reflexLastRun[mode] = now;
      emit({
        type: 'reflex',
        mode,
        action: result.action,
        detail: result.detail || '',
        priority: config.priority,
        interrupts: config.interrupts,
      });
      break;
    }
  } finally {
    state.reflexBusy = false;
  }
}

function hasRunningTask() {
  return Array.from(state.tasks.values()).some(
    (task) => task.status === 'running' && task.action !== 'patrol'
  );
}

function jumpBriefly(bot, ms = 450) {
  try {
    bot.setControlState('jump', true);
    setTimeout(() => {
      try {
        bot.setControlState('jump', false);
      } catch (_) {
        /* 断线了就结束 */
      }
    }, ms);
  } catch (_) {
    /* ignore */
  }
}

const INCIDENT_REPORT_MS = 2000;
const INCIDENT_BODY_MS = 1500;
const INCIDENT_WATER_OXYGEN = 220;

function detectIncident(bot) {
  if (!bot || !bot.entity) return null;
  const health = Number(bot.health);
  const oxygen = Number(bot.oxygenLevel);
  if (bot.entity.isInLava) {
    return { kind: 'lava', oxygen, health };
  }
  if (
    bot.entity.isInWater &&
    (!Number.isFinite(oxygen) || oxygen <= INCIDENT_WATER_OXYGEN)
  ) {
    return { kind: 'water', oxygen, health };
  }
  if (bot.entity.isInFire) {
    return { kind: 'fire', oxygen, health };
  }
  if (Number.isFinite(health) && health <= 5) {
    return { kind: 'low_health', oxygen, health };
  }
  return null;
}

function incidentPayload(bot, kind, phase) {
  const position = bot.entity && bot.entity.position;
  let nearestPlayer = null;
  if (position && bot.players) {
    for (const player of Object.values(bot.players)) {
      if (
        !player ||
        player.username === bot.username ||
        !player.entity ||
        !player.entity.position
      ) {
        continue;
      }
      const other = player.entity.position;
      const distance = position.distanceTo(other);
      if (!nearestPlayer || distance < nearestPlayer.distance) {
        nearestPlayer = {
          name: player.username,
          x: other.x,
          y: other.y,
          z: other.z,
          distance,
          in_water: Boolean(player.entity.isInWater),
          in_lava: Boolean(player.entity.isInLava),
          on_fire: Boolean(player.entity.isInFire),
        };
      }
    }
  }
  const items =
    bot.inventory && typeof bot.inventory.items === 'function'
      ? bot.inventory.items()
      : [];
  const held = bot.heldItem ? bot.heldItem.name : null;
  const hasBoat = items.some((item) => /_boat$/.test(item.name || ''));
  const blockCount = items
    .filter((item) => {
      const name = item.name || '';
      return /stone|cobble|dirt|plank|log|sand|gravel|terracotta/.test(name);
    })
    .reduce((total, item) => total + Number(item.count || 0), 0);
  const shore = kind === 'water' ? findNearestShore(bot, 16) : null;
  const activeTask = state.activeMoveTaskId
    ? {
        task_id: state.activeMoveTaskId,
        action: state.activeMoveTaskAction || '',
      }
    : null;
  return {
    type: 'incident',
    incident_id: state.incident && state.incident.id,
    incident_kind: kind,
    phase,
    position: position
      ? { x: position.x, y: position.y, z: position.z }
      : null,
    health: Number(bot.health),
    oxygen: Number(bot.oxygenLevel),
    in_water: Boolean(bot.entity.isInWater),
    in_lava: Boolean(bot.entity.isInLava),
    on_fire: Boolean(bot.entity.isInFire),
    active_task: activeTask,
    nearest_shore: shore
      ? {
          x: shore.x,
          y: shore.y,
          z: shore.z,
          distance: position ? position.distanceTo(shore) : null,
        }
      : null,
    nearest_player: nearestPlayer,
    held,
    has_boat: hasBoat,
    block_count: blockCount,
  };
}

async function incidentTick(bot, config, now) {
  const found = detectIncident(bot);
  if (!found) {
    if (state.incident) {
      emit({
        type: 'incident_cleared',
        incident_id: state.incident.id,
        incident_kind: state.incident.kind,
      });
      state.incident = null;
    }
    return false;
  }
  const isNew = !state.incident || state.incident.kind !== found.kind;
  if (isNew) {
    state.incidentCounter += 1;
    state.incident = {
      id: `incident-${state.incidentCounter}`,
      kind: found.kind,
      startedAt: now,
      lastReportAt: 0,
      lastBodyAt: 0,
    };
  }
  const incident = state.incident;
  const phase = isNew ? 'started' : 'update';
  const mustReport =
    isNew || now - Number(incident.lastReportAt || 0) >= INCIDENT_REPORT_MS;
  if (mustReport) {
    incident.lastReportAt = now;
    emit(incidentPayload(bot, incident.kind, phase));
  }

  // 身体只做最低限度保命：不选路线、不重复固定解。
  // 有任务在跑时完全让位，避免和 goto / escape / pillar 抢控制权。
  if (
    !hasRunningTask() &&
    now - Number(incident.lastBodyAt || 0) >= INCIDENT_BODY_MS
  ) {
    incident.lastBodyAt = now;
    if (incident.kind === 'water' || incident.kind === 'lava' || incident.kind === 'fire') {
      jumpBriefly(bot, 650);
    } else if (config.interrupts) {
      try {
        bot.pathfinder.setGoal(null);
      } catch (_) {
        /* ignore */
      }
    }
  }
  return true;
}

function findNearestShore(bot, radius = 16) {
  const position = bot.entity && bot.entity.position;
  if (!position) return null;
  const base = position.floored();
  const candidates = [];
  for (let dx = -radius; dx <= radius; dx += 1) {
    for (let dz = -radius; dz <= radius; dz += 1) {
      const distance = Math.hypot(dx, dz);
      if (distance < 1 || distance > radius) continue;
      candidates.push({ dx, dz, distance });
    }
  }
  candidates.sort((left, right) => left.distance - right.distance);
  for (const candidate of candidates) {
    for (let dy = 2; dy >= -4; dy -= 1) {
      const point = new Vec3(
        base.x + candidate.dx,
        base.y + dy,
        base.z + candidate.dz
      );
      const ground = bot.blockAt(point.offset(0, -1, 0));
      const feet = bot.blockAt(point);
      const head = bot.blockAt(point.offset(0, 1, 0));
      if (
        ground &&
        ground.boundingBox !== 'empty' &&
        !/water|lava/.test(ground.name || '') &&
        (!feet || feet.boundingBox === 'empty') &&
        (!head || head.boundingBox === 'empty')
      ) {
        return point;
      }
    }
  }
  return null;
}

async function runReflexMode(bot, mode, config, now) {
  return null;
}

function nearestHostile(bot, radius) {
  return bot.nearestEntity(
    (entity) =>
      entity &&
      entity.type === 'mob' &&
      entity.position &&
      isHostile(entity.name) &&
      isCloseEnough(entity.position.distanceTo(bot.entity.position), radius)
  );
}

async function startSession(config) {
  if (state.status === 'starting' || state.status === 'running') {
    throw httpError(409, 'SESSION_EXISTS', '已经有一局了');
  }

  const host = String((config && config.host) || cli.mcHost);
  const port = Number((config && config.port) || cli.mcPort);
  const username = String((config && config.username) || cli.username);

  state.status = 'starting';
  state.config = { host, port, username };
  state.reflexConfig = normalizeReflexConfig((config && config.reflexes) || {});
  state.reflexLastRun = {};
  state.activeMoveTaskId = null;
  state.activeMoveTaskAction = null;
  state.incident = null;
  state.incidentCounter = 0;
  emit({ type: 'status', state: 'starting' });

  let versionInfo;
  try {
    versionInfo = await pingServer(host, port);
  } catch (error) {
    state.status = 'idle';
    throw httpError(
      502,
      'CONNECT_FAILED',
      `连不上服务器 ${host}:${port} —— 世界开着吗？（${error.message}）`
    );
  }

  if (versionInfo.resolved && !SUPPORTED_VERSIONS.includes(versionInfo.resolved)) {
    state.status = 'idle';
    throw httpError(
      409,
      'UNSUPPORTED_VERSION',
      `这个服务器是 ${versionInfo.resolved}，我只支持 ${SUPPORTED_VERSIONS[0]} ~ ${
        SUPPORTED_VERSIONS[SUPPORTED_VERSIONS.length - 1]
      }`
    );
  }

  const bot = mineflayer.createBot({
    host,
    port,
    username,
    auth: 'offline',
    version: (config && config.version) || false,
  });
  state.bot = bot;
  state.lastHealth = 20;
  bot.loadPlugin(pathfinder);
  attachRuntimeListeners(bot);

  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('20 秒内没进服（可能被服务器拒绝）')), 20000);
    const settle = (fn) => {
      clearTimeout(timer);
      fn();
    };
    bot.once('spawn', () => settle(resolve));
    bot.once('error', (error) => settle(() => reject(error)));
    bot.once('kicked', (reason) =>
      settle(() => reject(new Error(`被服务器踢了：${JSON.stringify(reason)}`)))
    );
    bot.once('end', (reason) => settle(() => reject(new Error(`连接结束了：${reason}`))));
  }).catch((error) => {
    state.status = 'crashed';
    state.bot = null;
    try {
      bot.quit();
    } catch (_) {
      /* 已经在断了 */
    }
    throw httpError(502, 'JOIN_FAILED', `进服失败：${error.message}`);
  });

  state.sessionId = `mc-${Math.random().toString(16).slice(2, 10)}`;
  state.status = 'running';
  state.stopping = false;
  state.tasks.clear();
  state.counter = 0;
  state.mode = 'free';
  state.plugins = [];
  state.followTarget = null;
  state.plugins = loadPlugins({
    bot,
    mcData: minecraftData(bot.version),
  });
  startHeartbeat();
  startReflex();
  startProximityTracking();
  startInventoryTracking();
  emit({
    type: 'status',
    state: 'running',
    version: bot.version,
    username: bot.username,
    plugins: describePlugins(state.plugins),
  });
  return { session_id: state.sessionId, state: state.status, version: bot.version };
}

async function stopSession() {
  state.stopping = true;
  state.status = 'stopping';
  stopHeartbeat();
  stopReflex();
  stopProximityTracking();
  stopInventoryTracking();
  const bot = state.bot;
  if (bot) {
    try {
      bot.quit();
    } catch (_) {
      /* ignore */
    }
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, 3000);
      bot.once('end', () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
  state.bot = null;
  state.followTarget = null;
  state.mode = 'free';
  state.status = 'ended';
  emit({ type: 'status', state: 'ended' });
  return { ok: true };
}

function snapshot() {
  const bot = state.bot;
  if (!bot) return { state: state.status };
  const position = bot.entity && bot.entity.position;
  const inventory = inventoryDetails(bot);
  const nearMap = buildTerrainMap(bot, { radius: 8, step: 1, verticalSpan: 12 });
  const farMap = buildTerrainMap(bot, { radius: 64, step: 8, verticalSpan: 12 });
  const nearby = Object.values(bot.players || {})
    .filter((player) => player.username !== bot.username)
    .map((player) => ({
      name: player.username,
      distance:
        player.entity && position
          ? Number(player.entity.position.distanceTo(position).toFixed(1))
          : null,
    }));
  const inventoryItems = bot.inventory
    ? bot.inventory.items().map((item) => {
        const food = dataFor(bot).foodsByName[item.name];
        return {
          item: item.name,
          count: item.count,
          ...(food && Number(food.foodPoints) > 0 ? { food: true } : {}),
        };
      })
    : [];
  const inventoryByItem = Array.from(
    inventoryItems.reduce((counts, item) => {
      const current = counts.get(item.item) || { item: item.item, count: 0, food: false };
      current.count += Number(item.count || 0);
      current.food = current.food || Boolean(item.food);
      counts.set(item.item, current);
      return counts;
    }, new Map()).values()
  ).sort((left, right) => left.item.localeCompare(right.item));
  const emptySlots =
    bot.inventory && typeof bot.inventory.emptySlotCount === 'function'
      ? bot.inventory.emptySlotCount()
      : null;
  const entityList = nearbyEntities(bot, 64, 30);
  const nearbyPlayers = Object.values(bot.players || {})
    .filter(
      (player) =>
        player &&
        player.username !== bot.username &&
        player.entity &&
        player.entity.position
    )
    .map((player) => entityView(bot, player.entity))
    .sort((left, right) => left.distance - right.distance)
    .slice(0, 12);
  const nearbyHostiles = entityList.filter(
    (item) => item.type === 'mob' && item.hostile
  );
  const nearbyMobs = entityList.filter(
    (item) => item.type === 'mob' && !item.hostile
  );
  return {
    state: state.status,
    version: bot.version,
    player: {
      name: bot.username,
      x: position ? Number(position.x.toFixed(2)) : null,
      y: position ? Number(position.y.toFixed(2)) : null,
      z: position ? Number(position.z.toFixed(2)) : null,
      health: bot.health,
      food: bot.food,
      yaw: bot.entity ? Number(bot.entity.yaw.toFixed(2)) : null,
      pitch: bot.entity ? Number(bot.entity.pitch.toFixed(2)) : null,
    },
    inventory: inventoryItems,
    inventory_by_item: inventoryByItem,
    inventory_details: inventory,
    inventory_tools: inventory.tools,
    inventory_ammo: inventory.ammo,
    inventory_boats: inventory.boats,
    inventory_foods: inventory.foods,
    equipment: inventory.equipment,
    tool_guidance: {
      pickaxe: (bestTool(inventory.items, 'pickaxe') || {}).name || null,
      axe: (bestTool(inventory.items, 'axe') || {}).name || null,
      shovel: (bestTool(inventory.items, 'shovel') || {}).name || null,
      shears: (bestTool(inventory.items, 'shears') || {}).name || null,
    },
    inventory_empty_slots: emptySlots,
    inventory_full: emptySlots === 0,
    held: bot.heldItem
      ? { item: bot.heldItem.name, count: bot.heldItem.count }
      : null,
    game_mode: bot.game ? bot.game.gameMode : null,
    blocks: {
      below: position ? blockView(bot.blockAt(position.offset(0, -1, 0))) : null,
      front: position ? blockView(blockAtCursor(bot, 3)) : null,
    },
    looking_at: lookingAt(bot),
    nearby_blocks: mergeBlockCounts(
      nearbyBlocks(bot, 8, 1),
      nearbyBlocks(bot, 48, 4)
    ),
    nearby_entities: entityList,
    nearby_players: nearbyPlayers,
    nearby_hostiles: nearbyHostiles,
    nearby_mobs: nearbyMobs,
    entity_sense: {
      players: nearbyPlayers,
      hostiles: nearbyHostiles,
      passive_mobs: nearbyMobs,
    },
    hazards: {
      health: Number.isFinite(Number(bot.health)) ? Number(bot.health) : null,
      food: Number.isFinite(Number(bot.food)) ? Number(bot.food) : null,
      oxygen: Number.isFinite(Number(bot.oxygenLevel))
        ? Number(bot.oxygenLevel)
        : null,
      in_water: Boolean(bot.entity && bot.entity.isInWater),
      in_lava: Boolean(bot.entity && bot.entity.isInLava),
      on_fire: Boolean(bot.entity && bot.entity.isInFire),
      fall_distance:
        bot.entity && Number.isFinite(Number(bot.entity.fallDistance))
          ? Number(bot.entity.fallDistance.toFixed(2))
          : null,
    },
    dropped_items: nearbyDroppedItems(bot, 12),
    local_grid: localGrid(bot, 2),
    environment: environmentInfo(bot),
    terrain: terrainSense(bot),
    world: { near: nearMap, far: farMap },
    flat_sites: flatSites(bot),
    // 树林里很难找到 5×5 全空；3×3 是同一套蓝图的最小尺寸，作为降级候选。
    flat_sites_3: findFlatSites(bot, 3, 3, 3, 64, 5, 2),
    nearby: nearbyPlayers,
    following: state.followTarget,
    mode: state.mode,
    tasks: Array.from(state.tasks.entries()).map(([id, task]) => ({
      task_id: id,
      ...task,
    })),
  };
}

// ---------- HTTP / WS ----------

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
  });
  res.end(body);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      if (data.length > 1024 * 1024) {
        reject(new Error('请求体太大'));
        req.destroy();
      }
    });
    req.on('end', () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch (error) {
        reject(error);
      }
    });
    req.on('error', reject);
  });
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (req.headers.authorization !== `Bearer ${cli.token}`) {
      return sendJson(res, 401, { code: 'UNAUTHORIZED', message: '适配器 token 不对' });
    }

    if (req.method === 'GET' && url.pathname === '/health') {
      return sendJson(res, 200, {
        ok: true,
        adapter_version: ADAPTER_VERSION,
        protocol_version: PROTOCOL_VERSION,
        uptime: Math.round((Date.now() - startedAt) / 100) / 10,
        mineflayer: require('mineflayer/package.json').version,
      });
    }

    if (req.method === 'GET' && url.pathname === '/capabilities') {
      return sendJson(res, 200, CAPABILITIES);
    }

    if (req.method === 'POST' && url.pathname === '/session/start') {
      const body = await readJson(req);
      const result = await startSession(body.config || {});
      return sendJson(res, 200, result);
    }

    if (req.method === 'POST' && url.pathname === '/session/stop') {
      return sendJson(res, 200, await stopSession());
    }

    if (req.method === 'POST' && url.pathname === '/command') {
      const body = await readJson(req);
      const action = String(body.action || '');
      const args = body.args || {};
      if (state.status !== 'running') {
        return sendJson(res, 409, { code: 'NO_SESSION', message: '还没进服' });
      }
      if (!ACTIONS[action]) {
        return sendJson(res, 400, {
          code: 'UNKNOWN_ACTION',
          message: `没有这个动作：${action}`,
        });
      }
      if (action === 'mode') {
        // 常态模式：不建任务、不发 task_done。
        return sendJson(res, 200, { accepted: true, ...applyMode(state.bot, args) });
      }
      if (action === 'configure_reflex') {
        state.reflexConfig = normalizeReflexConfig(args.config || {});
        return sendJson(res, 200, { accepted: true, modes: state.reflexConfig.modes });
      }
      state.counter += 1;
      const taskId = `task-${state.counter}`;
      const task = { action, args, status: 'running', cancelled: false };
      state.tasks.set(taskId, task);
      Promise.resolve()
        .then(() => runCommand(action, args, taskId, task))
        .catch((error) => taskFailed(taskId, error.message));
      return sendJson(res, 200, { accepted: true, task_id: taskId });
    }

    if (req.method === 'POST' && url.pathname === '/cancel') {
      const body = await readJson(req);
      const taskId = String(body.task_id || '');
      const task = state.tasks.get(taskId);
      if (task) {
        task.status = 'cancelled';
        task.cancelled = true;
        if (task.action === 'follow' || task.action === 'goto') {
          try {
            state.bot.pathfinder.setGoal(null);
          } catch (_) {
            /* ignore */
          }
          state.followTarget = null;
        }
        emit({ type: 'task_done', task_id: taskId, ok: false, result: { cancelled: true } });
      }
      return sendJson(res, 200, { ok: true });
    }

    if (req.method === 'GET' && url.pathname === '/snapshot') {
      return sendJson(res, 200, snapshot());
    }

    return sendJson(res, 404, { code: 'NOT_FOUND', message: '没有这个端点' });
  } catch (error) {
    return sendJson(res, error.status || 500, {
      code: error.code || 'ADAPTER_ERROR',
      message: error.message || String(error),
    });
  }
});

const wss = new WebSocketServer({ noServer: true });

server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname !== '/events' || url.searchParams.get('token') !== cli.token) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
});

wss.on('connection', (ws) => {
  state.sockets.add(ws);
  while (state.buffered.length > 0) {
    ws.send(JSON.stringify(state.buffered.shift()));
  }
  ws.on('close', () => state.sockets.delete(ws));
  ws.on('error', () => state.sockets.delete(ws));
});

if (require.main === module) {
  server.listen(cli.port, '127.0.0.1', () => {
    console.log(
      `minecraft adapter listening on 127.0.0.1:${cli.port} (mineflayer ${require('mineflayer/package.json').version}, ${SUPPORTED_VERSIONS.length} versions)`
    );
  });
}

// 单元测试用：不启动 HTTP 服务，直接拿假 bot 验快照结构。
module.exports = { state, snapshot, inventoryDetails, detectIncident };
