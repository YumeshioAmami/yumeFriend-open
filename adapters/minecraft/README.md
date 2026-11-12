# Minecraft 适配器（P1）

实现 `docs/game_adapter.md` 的契约 v1，用 **mineflayer** 以**真玩家**身份进离线模式的世界。

## 安装

```bash
cd adapters/minecraft
npm install
```

`node_modules/` 不入库（`.gitignore` 已覆盖）；换机器后重装一次即可。

## 启动

由主代码的 `AdapterProcess` 启动（端口和一次性 token 由它给）：

```bash
node index.js --port <适配器端口> --token <token>
```

可选的默认值：`--mc-host 127.0.0.1 --mc-port 25565 --username Xiyin_ --heartbeat 5`。

## 一局怎么开

`POST /session/start`：

```json
{"config": {"host": "127.0.0.1", "port": 51856, "username": "Xiyin_"}}
```

流程：**先 ping 拿协议号 → 查版本 → 跟 `mineflayer.testedVersions` 比 → 离线模式进服**（D252）。
不支持 / 连不上都会返回一句人话（`UNSUPPORTED_VERSION` / `CONNECT_FAILED`）。

## 能力（P1）

| 动作 | 说明 |
|---|---|
| `follow(player?)` | 跟着玩家（不填 = 最近的）；长任务，直到 cancel / stop |
| `goto(x, y, z)` | 寻路走到坐标 |
| `say(text)` | 在游戏里说一句 |
| `stop()` | 停下当前寻路 / 跟随 |
| `mine(block, count?)` | 找最近的方块，寻路过去挖掉（最多 64 个） |
| `place(block, x, y, z)` | 从背包拿一个方块放到坐标（下方要有依托） |
| `attack(target?, count?)` | 打附近的敌对生物（不填 = 常见敌对怪；最多 20 只） |
| `eat()` | 吃东西（血少 / 饿的时候） |
| `drop(item, count?)` | 把背包里的物品丢出去 |
| `bring(item, count?, player?)` | 走到玩家身边再交给他（不是原地丢） |
| `escape()` | 卡住/困在洞里时向上挖 1×1 脱困 |
| `pillar(block?, count?)` | 站在原地往上垫方块（爬高 / 出坑） |
| `craft(item, count?)` | 合成物品（木板 / 木棍 / 工作台 / 镐子…） |
| `smelt(item, count?)` | 用熔炉烧炼（需要燃料） |
| `equip(item)` | 把背包里的物品拿到手上（换工具 / 食物） |

## 反射层（P2，默认开）

这些是"身体本能"，不进 LLM（宪法§1：性格归人格，本能归系统）：

| 反射 | 触发 | 动作 |
|---|---|---|
| 自动吃 | 血 < 12 且饱食 ≥ 18 | 从背包找吃的，吃一口 |
| 自动反击 | 3.5 格内有敌对生物 | 打一下（0.7s 冷却） |
| ~~脱困~~ | ~~有任务在跑但 3 秒没挪窝~~ | **已删除**（用户："定时跳一跳太弱智"）；需要脱困时她自己调 `escape` |

关掉：启动参数加 `--no-reflex`。

## 游戏内聊天（P4.1）

- **只把玩家聊天当成 TA 说话**；系统消息 / 命令反馈（`Teleported …`、`Set the time …`）忽略。
- 玩家在游戏里说话 → 主代码跑**完整工具循环**（她可以 `follow` / `mine` / `say`），
  回复会发到**游戏里**（她没自己 `game_say` 时，主代码兜底替她说）。
- 参数名宽容：`mine` / `place` 接受 `block`、`target`、`name`。
- 父进程（yumeFriend）被强杀后，适配器 5 秒内自己退出，不留孤儿。

## 已知边界

- **离线模式**（`auth: 'offline'`），不做微软正版登录（D251）。
- **她进服之后，按 Esc 不会冻结世界**（集成服务器检测到有其他玩家就不再暂停——2026-10-18 用户实测）；
  只有她不在线时，单人世界才可能被暂停。
- 挖 / 放 / 打 / 吃 / drop、记忆、桌面面板都已接上（P2~P4）。
- **模组物品/方块**：`minecraft-data` 只有原版数据；服务端模组（Forge/Fabric）的自定义物品/方块
  可能与名字对不上（能看到 ID，但不一定能按名字找/挖）。原版没问题，模组要实机再验。

## 打包（P4 / Q43）

`npm install` 会带完整 `minecraft-data`（449.5 MB，含基岩 + 73 个版本）。
打包前必须裁剪：

```bash
python scripts/prune_minecraft_data.py --apply
```

只保留 8 个 Java 版本 + 必需的 `pc/common` / `bedrock/common`（`node_modules` 降到 ~74.5 MB）。
`scripts/build_desktop.ps1` 已经自动做这一步。
