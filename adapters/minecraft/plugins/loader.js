'use strict';

/**
 * P7 / D288：模组插件加载器。
 *
 * 插件目录结构：
 *   plugins/<id>/manifest.json
 *   plugins/<id>/index.js  → { isAvailable?(bot, mcData), actions: {name: async(args, ctx)} }
 *
 * requires_mod 只做“物品 / 方块 / 插件自检”的软探测；缺了就 skipped，不影响主代码。
 */

const fs = require('fs');
const path = require('path');

const API_VERSION = 1;

function readManifest(dir) {
  const manifestPath = path.join(dir, 'manifest.json');
  const raw = fs.readFileSync(manifestPath, 'utf8');
  const manifest = JSON.parse(raw);
  if (!manifest.id || !manifest.name || !manifest.actions) {
    throw new Error('manifest 需要 id / name / actions');
  }
  if (Number(manifest.api_version || 1) !== API_VERSION) {
    throw new Error(`不支持的插件 api_version：${manifest.api_version}`);
  }
  return manifest;
}

function detectRequiresMod(manifest, mod, mcData) {
  const required = String(manifest.requires_mod || '').trim();
  if (!required) return { available: true, reason: '' };
  if (typeof mod.isAvailable === 'function') {
    try {
      return mod.isAvailable(mcData)
        ? { available: true, reason: '' }
        : { available: false, reason: `没有检测到模组依赖：${required}` };
    } catch (error) {
      return { available: false, reason: `模组探测失败：${error.message}` };
    }
  }
  const exists =
    Boolean(mcData && mcData.itemsByName && mcData.itemsByName[required]) ||
    Boolean(mcData && mcData.blocksByName && mcData.blocksByName[required]);
  return exists
    ? { available: true, reason: '' }
    : { available: false, reason: `没有检测到模组物品/方块：${required}` };
}

function loadPlugins(options = {}) {
  const root = options.root || __dirname;
  const mcData = options.mcData || null;
  const output = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = path.join(root, entry.name);
    const manifestPath = path.join(dir, 'manifest.json');
    const modulePath = path.join(dir, 'index.js');
    if (!fs.existsSync(manifestPath) || !fs.existsSync(modulePath)) continue;
    try {
      const manifest = readManifest(dir);
      const mod = require(modulePath);
      const availability = detectRequiresMod(manifest, mod, mcData);
      output.push({
        manifest,
        actions: mod.actions || {},
        available: availability.available,
        reason: availability.reason,
      });
    } catch (error) {
      output.push({
        manifest: { id: entry.name, name: entry.name, actions: {} },
        actions: {},
        available: false,
        reason: `加载失败：${error.message}`,
      });
    }
  }
  return output;
}

function describePlugins(plugins) {
  return plugins.map((plugin) => ({
    id: plugin.manifest.id,
    name: plugin.manifest.name,
    version: plugin.manifest.version || '0',
    api_version: plugin.manifest.api_version || 1,
    requires_mod: plugin.manifest.requires_mod || null,
    available: Boolean(plugin.available),
    reason: plugin.reason || '',
    actions: Object.entries(plugin.manifest.actions || {}).map(([name, spec]) => ({
      name,
      description: String((spec && spec.description) || ''),
      effect: String((spec && spec.effect) || 'interact'),
      params: (spec && spec.params) || {},
    })),
  }));
}

async function callPlugin(plugins, pluginId, actionName, args, context = {}) {
  const plugin = plugins.find((item) => item.manifest.id === pluginId);
  if (!plugin) {
    return { ok: false, error: `没有这个插件：${pluginId}` };
  }
  if (!plugin.available) {
    return {
      ok: false,
      skipped: true,
      error: `[条件不满足] 插件 ${pluginId} 未启用：${plugin.reason || '模组不存在'}`,
    };
  }
  const action = plugin.actions[actionName];
  if (typeof action !== 'function') {
    return { ok: false, error: `插件 ${pluginId} 没有动作：${actionName}` };
  }
  try {
    const result = await action(args || {}, context);
    return { ok: true, plugin: pluginId, action: actionName, result };
  } catch (error) {
    return {
      ok: false,
      plugin: pluginId,
      action: actionName,
      error: String((error && error.message) || error),
    };
  }
}

module.exports = { API_VERSION, loadPlugins, describePlugins, callPlugin };
