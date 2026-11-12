'use strict';

function items(bot) {
  return bot.inventory && typeof bot.inventory.items === 'function'
    ? bot.inventory.items()
    : [];
}

module.exports = {
  actions: {
    async inventory_summary(_args, { bot }) {
      const list = items(bot);
      return {
        items: list.map((item) => ({ item: item.name, count: item.count })),
        held: bot.heldItem
          ? { item: bot.heldItem.name, count: bot.heldItem.count }
          : null,
        used_slots: Array.isArray(bot.inventory && bot.inventory.slots)
          ? bot.inventory.slots.filter(Boolean).length
          : list.length,
      };
    },

    async drop_item(args, { bot }) {
      const wanted = String((args && args.item) || '').trim();
      const count = Math.max(1, Math.min(64, Number((args && args.count) || 1)));
      const item = items(bot).find((entry) => entry.name === wanted);
      if (!item) {
        throw new Error(`[条件不满足] 背包里没有 ${wanted || '这个物品'}`);
      }
      if (typeof bot.toss !== 'function') {
        throw new Error('当前适配器不能丢物品');
      }
      await bot.toss(item.type, null, Math.min(count, item.count));
      return { dropped: wanted, count: Math.min(count, item.count) };
    },

    async use_held_item(_args, { bot }) {
      if (!bot.heldItem) {
        throw new Error('[条件不满足] 手上没有物品');
      }
      if (typeof bot.activateItem !== 'function') {
        throw new Error('当前适配器不能使用手持物');
      }
      bot.activateItem();
      await new Promise((resolve) => setTimeout(resolve, 250));
      if (typeof bot.deactivateItem === 'function') {
        bot.deactivateItem();
      }
      return { used: bot.heldItem.name };
    },
  },
};
