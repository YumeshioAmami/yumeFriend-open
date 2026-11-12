'use strict';

// 模型经常会用不同的参数名（block / target / name ...）。
// 这里做一层**宽容归一**：少一个参数名，不该让整件事失败。

function pickBlockName(args) {
  if (!args || typeof args !== 'object') return '';
  return String(
    args.block || args.target || args.name || args.item || args['方块'] || args['物品'] || ''
  ).trim();
}

function pickAttackTarget(args) {
  if (!args || typeof args !== 'object') return '';
  return String(args.target || args.name || args.mob || args['实体'] || '').trim();
}

module.exports = { pickBlockName, pickAttackTarget };
