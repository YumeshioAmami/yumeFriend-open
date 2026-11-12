'use strict';

// P6 / D288：从 minecraft-data 返回事实，不靠模型记忆。

const TIER_ORDER = [
  'wooden',
  'golden',
  'stone',
  'iron',
  'diamond',
  'netherite',
];

function itemName(mcData, id) {
  const item = mcData.items && mcData.items[id];
  return item ? item.name : null;
}

function simplifyIngredients(mcData, ingredients) {
  const counts = new Map();
  for (const id of ingredients || []) {
    const name = itemName(mcData, id);
    if (!name) continue;
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  return Array.from(counts.entries()).map(([item, count]) => ({ item, count }));
}

function recipeFor(mcData, itemNameOrId) {
  const key = String(itemNameOrId || '').replace(/^minecraft:/, '');
  const item =
    mcData.itemsByName[key] ||
    (Number.isFinite(Number(key)) ? mcData.items[Number(key)] : null);
  if (!item) {
    return { ok: false, error: `没有这个物品：${key}` };
  }
  const recipes = (mcData.recipes && mcData.recipes[item.id]) || [];
  const simplified = recipes.slice(0, 8).map((recipe) => {
    const flat = [];
    if (Array.isArray(recipe.inShape)) {
      for (const row of recipe.inShape) {
        for (const id of row || []) {
          if (id != null) flat.push(id);
        }
      }
    } else if (Array.isArray(recipe.ingredients)) {
      flat.push(...recipe.ingredients);
    }
    return {
      output: {
        item: item.name,
        count: Number(recipe.result && recipe.result.count) || 1,
      },
      ingredients: simplifyIngredients(mcData, flat),
      shaped: Array.isArray(recipe.inShape),
      requires_table:
        (Array.isArray(recipe.inShape) &&
          (recipe.inShape.length > 2 ||
            recipe.inShape.some((row) => Array.isArray(row) && row.length > 2))) ||
        false,
    };
  });
  return {
    ok: true,
    query: 'recipe_for',
    item: item.name,
    recipes: simplified,
    count: simplified.length,
    source: 'minecraft-data',
  };
}

function toolTierFor(mcData, blockNameOrId) {
  const key = String(blockNameOrId || '').replace(/^minecraft:/, '');
  const block =
    mcData.blocksByName[key] ||
    (Number.isFinite(Number(key)) ? mcData.blocks[Number(key)] : null);
  if (!block) {
    return { ok: false, error: `没有这个方块：${key}` };
  }
  const tools = [];
  for (const id of Object.keys(block.harvestTools || {})) {
    const item = mcData.items[Number(id)];
    if (item) tools.push(item.name);
  }
  const tiers = tools
    .map((name) => {
      const bare = name.replace(/_pickaxe$/, '');
      return { name, tier: TIER_ORDER.indexOf(bare) + 1 };
    })
    .filter((item) => item.tier > 0);
  tiers.sort((a, b) => a.tier - b.tier);
  return {
    ok: true,
    query: 'tool_tier_for',
    block: block.name,
    material: block.material || null,
    hardness: block.hardness,
    diggable: block.diggable,
    tools,
    minimum_tier: tiers.length ? tiers[0].tier : 0,
    minimum_tool: tiers.length ? tiers[0].name : null,
    tier_names: TIER_ORDER,
    source: 'minecraft-data',
  };
}

module.exports = { recipeFor, toolTierFor, TIER_ORDER };
