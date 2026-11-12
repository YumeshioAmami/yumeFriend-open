'use strict';

module.exports = {
  isAvailable(mcData) {
    return Boolean(
      mcData &&
        mcData.itemsByName &&
        mcData.itemsByName.example_mod_item
    );
  },
  actions: {
    async mod_scan() {
      return { scanned: true };
    },
  },
};
