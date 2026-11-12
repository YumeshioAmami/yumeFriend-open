function foodInInventory(bot, data) {
  return bot.inventory.items().find((item) => {
    // foodPoints 在 foodsByName 里，不在 itemsByName 里。
    const info = data.foodsByName[item.name] || data.itemsByName[item.name];
    return info && Number(info.foodPoints) > 0;
  });
}

module.exports = { foodInInventory };
