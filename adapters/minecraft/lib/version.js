'use strict';

// D252：版本清单不手写 —— minecraft-data 提供 协议号 ↔ 版本名 的映射。

const minecraftData = require('minecraft-data');

let _dataPaths = null;

function loadDataPaths() {
  if (_dataPaths !== null) return _dataPaths;
  try {
    // P4 / Q43：裁剪后这里只剩保留下来的版本
    _dataPaths = require('minecraft-data/minecraft-data/data/dataPaths.json');
  } catch (_) {
    _dataPaths = false;
  }
  return _dataPaths;
}

function pcVersions() {
  return (minecraftData.versions && minecraftData.versions.pc) || [];
}

function resolveVersionName(protocol, fallback) {
  const hit = pcVersions().find((item) => item.version === protocol);
  return hit ? hit.minecraftVersion : fallback;
}

function protocolsFor(versionName) {
  return pcVersions()
    .filter((item) => item.minecraftVersion === versionName)
    .map((item) => item.version);
}

/** 只保留"mineflayer 说能玩 + 本地真的有数据"的版本（裁剪后自动变少）。 */
function supportedVersions(tested) {
  const paths = loadDataPaths();
  if (!paths || !paths.pc) return [...tested];
  const available = new Set(Object.keys(paths.pc));
  return [...tested].filter((version) => available.has(version));
}

module.exports = { resolveVersionName, protocolsFor, supportedVersions };
