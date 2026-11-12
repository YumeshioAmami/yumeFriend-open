'use strict';

const test = require('node:test');
const assert = require('node:assert');
const Vec3 = require('vec3');

const { shelterLayout, findFlatSites } = require('../lib/build');

test('shelterLayout builds walls bottom-up, then roof outside-in', () => {
  const layout = shelterLayout({ x: 10, y: 64, z: 20 }, 5, 5, 3);
  const walls = layout.filter((block) => block.kind === 'wall');
  const roofs = layout.filter((block) => block.kind === 'roof');

  assert.equal(walls.length + roofs.length, layout.length);
  assert.equal(roofs.length, 25);
  // 墙：5×5 周长 16，每层 3 层，正面中间两格是门洞
  assert.equal(walls.length, 16 * 3 - 2);
  // 第一块墙在 y+1，最后一块墙在 y+3
  assert.equal(walls[0].y, 65);
  assert.equal(walls[walls.length - 1].y, 67);
  // 屋顶在墙顶再上一层
  assert.equal(roofs[0].y, 68);
});

test('shelterLayout leaves a two-block door on the front face', () => {
  const layout = shelterLayout({ x: 0, y: 64, z: 0 }, 5, 5, 3);
  const door = layout.filter(
    (block) => block.kind === 'wall' && block.x === 2 && block.z === 0
  );
  assert.equal(door.length, 1); // 只剩门洞上方那一格
  assert.equal(door[0].y, 67);
});

test('shelterLayout clamps silly dimensions', () => {
  const layout = shelterLayout({ x: 0, y: 64, z: 0 }, 99, 99, 99);
  const walls = layout.filter((block) => block.kind === 'wall');
  const roofs = layout.filter((block) => block.kind === 'roof');
  // width/depth 被夹到 7，height 被夹到 4
  assert.equal(roofs.length, 49);
  assert.ok(walls.every((block) => block.y >= 65 && block.y <= 68));
});

test('findFlatSites finds 5x5 flat ground near the player', () => {
  const bot = {
    entity: {
      position: new Vec3(0, 64, 0),
    },
    blockAt(position) {
      // 地面在 y=63，64~68 都是空气
      return position.y === 63
        ? { name: 'dirt', boundingBox: 'block', position }
        : null;
    },
  };
  const sites = findFlatSites(bot, 5, 5, 3, 16, 3);
  assert.equal(sites.length, 3);
  assert.ok(sites.every((site) => site.ground === 'dirt'));
  assert.ok(sites.every((site) => site.distance >= 2));
});
