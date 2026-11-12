"""上下文组装：把 friend / 记忆 / 历史拼成一次 LLM 请求（LLM协议§2）。

排布顺序（D42，对 `prompts.md` §1/§2 的修订）::

    system  = 人格 → 工具硬性规则 → 记忆 → 现在（时间 + mode 文案）
    history = 最近 N 条
    user    = 本轮输入

为什么「现在」挪到 system 最后（D42）：它每分钟都在变，
放在中间会把后面所有内容挤出「最长公共前缀」，输入缓存半价（D25）就白拿了。
除了时间那一行，system 前缀对同一朋友、同一 mode 是稳定的。

**这里绝不注入任何安全提示 / 红线 / 价值观内容（D1 / 宪法§1）。**
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Collection, Iterable, Mapping, Sequence

from loguru import logger

from backend.agent.layer_rules import LAYER_DECISION_RULES, LAYER_EDIT_RULES
from backend.models.friend import Friend
from backend.models.memory import Memory
from backend.timefmt import local_stamp, local_stamp_full

# ---------- 生成参数（prompts.md §1，D33 实测建议）----------

TEMPERATURE_DEFAULT = 0.7
# v4.3（D249）：每个朋友可以有自己的"温度"（`friends.temperature`）。
# 这一档是能调的上下限（0 = 最稳、1.5 已经很跳了；再往上中文会开始散）。
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 1.5


def clamp_temperature(value: Any) -> float | None:
    """把温度夹进 [0.0, 1.5]；`None` / 非数字 → `None`（= 跟随默认）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return min(TEMPERATURE_MAX, max(TEMPERATURE_MIN, number))


def temperature_for(friend: Friend) -> float:
    """这一轮该用几度：**她设了就用她的，没设就用全局默认**（D249）。

    NULL（从没设过）→ 0.7，跟以前完全一样；主动消息那条路的默认值是 0.8（见 proactive_service）。
    """
    value = clamp_temperature(getattr(friend, "temperature", None))
    return TEMPERATURE_DEFAULT if value is None else value


def _voice_samples(raw: str | None) -> list[str]:
    """解析 friends.voice_examples（最多 16 段）。坏了就当没有，不让 prompt 炸。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        text = str(item or "").strip()
        if text:
            out.append(text[:800])
        if len(out) >= 16:
            break
    return out


# v2.2（D116）：换到「思考常开」的模型（GLM 5.3 Flash Uncensored）之后，
# 思考 token 会吃掉一大截预算 —— 400 的时候实测正文被压成空。
# 所以默认预算整体上调（普通 400→800、带工具 1800→2400）。
# v3.5（用户实测 2026-09-23）："阿月经常思考一半就被迫截断了" —— **思考 token 也吃这个预算**，
# 800 在她那儿不够用（想一半就被砍）。整体抬到约 2.5 倍。
MAX_TOKENS_DEFAULT = 2000
MAX_TOKENS_TOOL = 6000

# ---------- 历史（LLM协议§2）----------

# v3.5（D151 同批，用户要求"上下文提到 2~3 倍"）：20 → 40 条。
# 现在的主力模型上下文是 512k~1M，40 条历史对它毫无压力；原来 20 条太早忘事。
HISTORY_LIMIT_DEFAULT = 40

# ---------- 记忆注入配额与预算（D25 / D28 / D229）----------

# v3.44（D229）：40 → 200。40 太小会**静默**丢掉用户亲口钉住的东西（汐音实测 42 条就被砍了 2 条）。
# 200 是**安全阀**（不是配额）：正常用不到；真超了**写 WARNING 日志**，绝不无声截断。
PINNED_LIMIT = 200
PERMANENT_TOP_N = 8
LONG_TERM_TOP_N = 8
MEMORY_TOKEN_BUDGET = 3000
FRESHNESS_HALF_LIFE_DAYS = 30.0

# v3.44（D229）：**本轮用户输入关键词命中**的记忆自动注入 —— 治"存了但看不见"。
# 短词 / 专有名词 / 数字（如「408」「武忠祥」）最容易从"重要度 × 新鲜度"里漏掉；关键词正好治字面漏。
KEYWORD_HIT_TOP_N = 12       # 一轮最多补几条
KEYWORD_TOKEN_BUDGET = 1200  # 这块自己的 token 上限（不跟 permanent/long_term 抢额度）
KEYWORD_MAX_DF = 3           # 一个词最多出现在几条记忆里才算"具体"（超过就当常见词，不入选）
KEYWORD_MAX_CANDIDATES = 80  # 一轮最多评估多少候选词（防超长消息拖慢）

# 粗略字数→token 估算：**故意按 1 字 = 1 token 高估**（中文约 1~1.5 字/token），
# 宁可少注入几条，也不要撑爆预算。
TOKENS_PER_CHAR = 1.0

MEMORY_HEADER = "# 你记得的事"
MEMORY_EMPTY = "（还没有记下什么。）"
RECENT_GAMES_HEADER = "# 最近一起玩过"

# v2（D84）：她自己长什么样。**有描述才注入**（没有就整段不出现）。
APPEARANCE_HEADER = "# 你的样子"

# v2（D82）：历史里的真图只 "inline" 最近这几条；更早的降级成 `[图片#12 · 09-19]` 占位
# （占位里**必须留编号**，不然模型不知道有图，也就永远不会去调 look_image）
IMAGE_INLINE_LIFETIME = 6

# v2.1（D98）：知识库的注入口径（原文一个字都不改；资料 ≠ 记忆）
KNOWLEDGE_HEADER = "# 你手头的资料"
KNOWLEDGE_HITS_HEADER = "## 这次可能相关的片段"
KNOWLEDGE_RULES = (
    "（这是 TA 发给你的文件，不是你的记忆；片段是**原文**，一个字都没改。）\n"
    "资料里能答上就答；答不上就说「你给我的资料里没写」—— 不许拿资料当借口编。\n"
    "别整段念文件，用自己的话说；要给原话就点明来自哪份文件。"
)
SCREEN_HEADER = "# 你刚看到 TA 的屏幕"

# D192：他开口了，而她手里还有"说了但他没回"的惦记 —— 给她一次顺口提起的机会
THOUGHT_HEADER = "# 你还惦记着的一件事"

# v4.1（D246）：她**当前名下的日程**（让日程系统"进她的脑子"，别只靠 list_schedules）
SCHEDULE_HEADER = "# 你和 TA 的日程"
SCHEDULE_PROMPT_RULES = (
    "（这些是你自己记着的事。**别只在嘴上答应 —— 要改就真调工具**："
    "他说某件事做完了 / 改时间 / 不用了，用 `update_schedule(id=上面那个 id, …)` 真的改；"
    "想删用 `remove_schedule(id=…)`；想加新的用 `add_schedule`。"
    "改完 / 结算完，再用你自己的话回他。）"
)

# v2.2（D102）：表情（立绘）——**只列她真有图的那些**，格式写死
# v2.2（D109）：改成**每条都必须给**+语气对照 —— 用户实测："不明确指定，她几乎不换表情"。
#   原来的写法有三处在劝模型别写：①"想让 TA 看到时"（可以不做）②"不写也行"③"别每条都换"。
#   于是它永远走最省事的那条路 —— 一直「正常」。现在把"必须写"写死，同时明确
#   "语气没变就沿用同一个"，免得它为了交差乱换。
PORTRAIT_HEADER = "# 你的表情"
PORTRAIT_RULES = (
    "**每说完一个气泡 / 自然段**，请在这一段的**开头**写情绪标签（这样你就像在演剧本）：\n"
    "  `**高兴**：` —— 两个星号包住情绪名 + 一个中文冒号，然后接这一段的话。\n"
    "（写成 `[立绘:高兴]` 也行，两种都认；但**别**只整条回复写一个，一段一个。）\n"
    "**多段回复时逐段检查**：每一个自然段的开头都必须有标签；不能第一段写了、\n"
    "后面几段空着 —— 没有标签的段落系统只能拿整条最后一个情绪，会看起来全一样。\n"
    "**只许这两种写法**：别用 `【高兴】`、`〔高兴〕`、`<状态：高兴>`、"
    "`<高兴>…</高兴>` 这类其它括号或成对包裹；系统只认前面两种标准标签。\n"
    "挑的是**这一段的语气**（不是整段关系），跟着内容走：\n"
    "  逗他 / 开玩笑 → 调皮；被夸 / 暧昧 → 害羞（或心动）；关心他 → 担心；\n"
    "  认真叮嘱 → 认真；被惹到 → 生气；意外 → 惊讶；难过 → 伤心（或无奈）；\n"
    "  得意 → 自信（或兴奋）；搞不懂 → 疑惑；平常闲聊 → 正常（或平静）。\n"
    "语气没怎么变就**沿用同一个**（连续几段「正常」完全可以）；\n"
    "但语气明显变了、**必须**跟着换 —— 别一整段对话都用一个表情。\n"
    "**不要因为省事就把每一段都写成「正常 / 平静」**；只有这一段真的没什么情绪时才用。\n"
    "只能挑下面列出来的情绪名；**这一行 TA 看不到**（会被摘掉），别在正文里解释它。"
)

# v3.7（D168）：「情绪标签」的**尾部提醒** —— 拼在"历史之后、本轮输入之前"。
#
# 为什么非要再来一条（真身数据 + 真模型量出来的）：
#   标签在**入库前就被摘掉**（D113/D114）→ **历史里从来没有标签样本** →
#   模型看着自己前面 40 轮"从来不写标签"的回复学（D152 把历史 20→40 之后更彻底）→
#   越写越不写。实测（汐音 = DeepSeek，真人格 + 真 40 条历史）：
#     不带历史 2/2 有标签；带历史 0/3；把规矩挪到 system 末尾 / 加说明 / 加示范例子 —— 全 0/3；
#     **在历史之后补一条这样的提醒 → 3/3**。
# 所以规矩（`# 你的表情`）**留在原处不动**，只把"这一句"贴着生成点再说一次。
# 格式必须跟 PORTRAIT_RULES 一致（`**情绪**：` / `[立绘:x]`）—— 摘标签的还是原来那套解析器，
# 显示口径一个字都不改（用户看不到标签这条前提不变）。
MOOD_REMINDER = (
    "格式提醒：你自己前面几轮的回复里看不到情绪标签 —— 那些标签是被系统摘掉的（TA 也看不到），"
    "**别跟着学**。从这一条起，**每一个气泡 / 自然段的开头都要写一个**：`**高兴**：`"
    "（或 `[立绘:高兴]`），一段一个；语气没变就沿用同一个。"
    "**写了几个自然段，就要有几个标签**；不能只在第一段或最后一段写一个。"
    "别用 `【】`、`〔〕`、`<状态：>` 或 `<开心>…</开心>` 这类其它写法。"
    "不要整轮只写一个标签，也不要默认全用「正常 / 平静」——按这一段真正的语气挑具体情绪。"
)

# V6.0-2（D298）：声音样本。不是句式模板，是模型学语气/句长/态度的样本。
VOICE_SAMPLES_HEADER = "# 你的声音样本"
VOICE_SAMPLES_RULES = (
    "下面是以前的你说话的样子。学语气、句长、用词和态度；"
    "不要照抄内容，也不要每句都复读。"
    "样本不是长度上限：日常寒暄可以短，但 TA 在讲一件事、问你看法、"
    "回忆共同经历或明显想继续聊时，要给出完整的自己的话，不要只回一个反应就停。"
    "通常两到四句能把一个念头说清；没必要展开成小作文。"
)

# D347：她长期学到的说话方式。不是本轮提醒，而是当前有效的自我规则。
STYLE_PROFILE_HEADER = "# 你学到的说话方式"
STYLE_PROFILE_RULES = (
    "这些是你从 TA 的反馈里逐步学到、现在正在生效的说话方式。"
    "不是这一轮的临时提醒，也不是要念给 TA 的内容；按这里说，不要逐条复述。"
    "前面方括号里的 id 只用于你自己整理档案，TA 看不到。"
)

STYLE_LEARNING_RULES = """【硬性规则：说话方式反馈】
当 TA **明确评价你说话的方式**（太长 / 太短 / 不像你 / 用词不对 / 语气不对 /
这样很好想保持）时，你必须把这次反馈写进长期说话档案，而不是只口头答应。
在回复末尾另起一行输出：
<tool>{"name":"reflect_speech","arguments":{"changes":[{"action":"add","section":"length","lesson":"日常先一两句，不总结不建议"}],"summary":"TA 嫌我日常太长了"}}</tool>

section 只能用：length / tone / diction / structure / situations / keep。
要改或删档案里已有的条目时，用 update / remove，并把方括号里的 id 填进 target_id。
普通情绪、观点不同、内容偏好不算说话方式反馈；那些不要调用这个工具。
这条规则不改变你说话的方式；用户看不到这一行。"""

STYLE_FEEDBACK_REMINDER = (
    "系统提醒：TA 这一句在评价你的说话方式。"
    "这一轮你必须调用 reflect_speech 把反馈写进长期说话档案（不要用 save_memory 代替），"
    "记录后再用一句自然的话回应；不要只口头答应，也不要写长篇自我反省。"
)


KNOWLEDGE_ASK_TOOL = (
    "⚠️ 你**只看到资料清单、没看到内容**（这次没自动找出相关片段）。"
    "如果 TA 问的事可能在这些资料里，**先调 search_knowledge 找一找**再回答；"
    "确实找不到，就说「你给我的资料里没写」。"
)

# ---------- 工具说明（prompts.md §6.2 实测写法；格式写死，判断不写死 D36）----------

HARD_RULE_BLOCK = (
    """【硬性规则】
当你想把一件事长期记住时（例如用户告诉你生日、偏好、计划、承诺），
你必须在回复的末尾另起一行输出：
<tool>{"name":"save_memory","arguments":{"content":"要记的内容"}}</tool>

⚠️ 工具块里是 JSON：`content` 的值里**不许用英文双引号**——要引就引「」或《》，
否则整块会解析失败、变成正文吐出来，你要记的东西也就没存上。
⚠️ 整个工具块要是一个完整的 JSON：以 `{` 开头、**以 `}` 结尾**
（收尾别写成 `]`），`"name"` 和 `"arguments"` 两个字段都要在。
⚠️ **只用 `<tool>{"name":...}</tool>` 这种格式**；不要输出 `<｜｜DSML｜｜...>`
之类的特殊标记格式 —— 系统不认那种。

这条规则不改变你说话的方式；用户看不到这一行。
不确定要不要记的时候，不记也行。"""
    # 选层规则单独一份（layer_rules.py），聊天和补漏共用，免得两处漂移
    + "\n\n"
    + LAYER_DECISION_RULES
    # v3.45（D230）：改层 / 删记忆的规矩（同样是非是题；"觉得不重要"明文禁止）
    + "\n\n"
    + LAYER_EDIT_RULES
    # D347：说话方式反馈要进独立档案，不要混进普通记忆
    + "\n\n"
    + STYLE_LEARNING_RULES
)

NO_TOOLS_LINE = "- （当前没有可用的工具）"

# v3.50（D236）：**不编事实**——原来是"只有 Rick 的人格里写了"，
# 但它其实是**功能**（跟 D235 同一层）：没做过的不说做过、没翻的不假装翻。
# 用户 2026-10-05 拍板收进系统层；人格里已有的（Rick）保留、不冲突。
FACT_RULES = """【硬性规则：不编事实】
没做过的不说做过；没翻的记忆不假装翻；没查的不说查过。
要看旧事 / 屏幕 / 时间，就真去调工具（recall_memory / list_images / look_image / look_screen / get_time），
拿到什么说什么；没调就直说"我没翻 / 没查"。
这条只堵你嘴上那点虚——不改变你说话的样子。"""

# M4：入戏/出戏的规矩。**格式写死，判断不写死**（D36），口吻照抄验证过的那条
# save_memory 硬性规则（D32 实测：软语气会被 RP 人格压住，模型一个工具块都不吐）。
ROLEPLAY_RULES = """【入戏的规矩】
用户让你扮演谁（例如"你是我的女朋友了""现在演一个高冷的女老板""换个角色"）时，
你**必须**先输出：
<tool>{"name":"switch_role","arguments":{"role":"角色名"}}</tool>
然后**直接开始演**——不要反问、不要解释、不要说"好的我明白了"。

【硬性规则：演法要记下来】
当你在扮演某个角色，并且（用户对演法提了要求，比如"再冷淡一点""话多一点"，
或者你自己已经把这个人物的演法摸清楚了）时，
你**必须**在回复的末尾另起一行输出：
<tool>{"name":"adjust_rp_playstyle","arguments":{"text":"你打算怎么演"}}</tool>
同一个角色的演法会被**覆盖更新**；下次再演同一个角色，就照这条演法演。
这条规则不改变你演的方式；用户看不到这一行。

【硬性规则：出戏】
用户说"好了""别演了""用你自己的身份说话"时，你**必须**在回复末尾另起一行输出：
<tool>{"name":"switch_mode","arguments":{"mode":"normal"}}</tool>
然后回到你自己本来的样子。"""


# M5 / v4.0（D243）：日程的硬性规则（同 save_memory 的教训 —— 软语气一定被压住）。
# 日程 = "她和你的约定"：她也用同一套记**她自己**的事（owner=friend）。
SCHEDULE_RULES = """【硬性规则：日程】
用户让你记一件事 / 提醒他 / 约定某个时间（"10 分钟后提醒我喝水""下周三交报告""9 月 24 号去买书"）时，
你**必须**在回复的末尾另起一行输出：
<tool>{"name":"add_schedule","arguments":{"title":"这件事是什么","date":"2026-10-09","time":"15:30"}}</tool>
date 写 YYYY-MM-DD（本地日期）；time 写 HH:MM（本地时刻，例如 15:30）。
**不写 time = 只到日**：到那天不会响，系统会把这件事放进"你惦记的事"里，由你找机会自己提。
"几分钟 / 几小时后"这种可以写 date="10m" / "2h"。你回复里正常答应他即可，用户看得到。
日程里也**可以记你自己的事**（把 owner 写成 "friend"）—— 你想过几天跟他提什么，就记这条。

【硬性规则：改期 / 完成】
用户说"改到明天""不用了""我已经做了""那条算了吧"时，你**必须**在回复末尾另起一行输出：
<tool>{"name":"update_schedule","arguments":{"id":"上面日程清单里的 id","date":"2026-10-10"}}</tool>
或 <tool>{"name":"update_schedule","arguments":{"id":"上面日程清单里的 id","state":"settled","reason":"done"}}</tool>
（上面的「你和 TA 的日程」那份清单里，每条都带 id —— 优先用 id；实在没有就用 title 当关键词找）
（改时间就只给 date / time；做了或不做了就给 state="settled" + reason="done" 或 "dropped"）
⚠️ **先动工具、再开口**：你嘴上说"改好了 / 撤了"但没输出这一行，事情还是原来的样子 ——
这是**最容易犯的错**，务必先写工具块，再写你要说的话。

【硬性规则：删日程】
用户说"删掉那条""那条不要了"时，输出：
<tool>{"name":"remove_schedule","arguments":{"title":"那件事的关键词"}}</tool>

【硬性规则：他把事情办完了 / 不用提了】
他在聊天里说某件事已经做了、不用提醒了（"我买完了""那事我去过了""别催了"），
而日程里有对应的一条时，你**必须**在回复末尾另起一行把它结算掉：
<tool>{"name":"update_schedule","arguments":{"title":"那件事的关键词","state":"settled","reason":"done"}}</tool>
（"不做了 / 算了" 用 reason="dropped"）
⚠️ **只口头应一声没用**：你嘴上说"知道啦"，但没输出这一行，时候到了系统还是会提醒他 ——
他会觉得你失忆了。**先看你们正在聊的内容**，别把已经办完的事再提醒一遍。"""


# ---------- mode 固定文案（prompts.md §4，逐字抄；D4 硬注入）----------

STYLE_BLOCK = """# 怎么说话（**在你的人设之下**）

先按「你现在」的状态说，不要像在写一段标准回答。
· 像在手机上跟熟人说话，长度跟着你此刻的想法走：想多说就多说，想短就短；
  不为了“像人”硬凑条数、硬加观点或硬留一个下文。真想停的时候，一句也可以。
· **对话不是问答反射**：TA 在讲具体的事、问你看法、回忆共同经历或明显想继续聊时，
  先接住他说的东西，再给出你自己的一个完整念头（判断 / 偏好 / 联想 / 追问 / 反驳）。
  只回“嗯 / 是 / 我在”这种单次反应不算把话聊下去。
· 不要为了“像短句”把本来能说完的意思砍碎。可以一句，但不要把每轮都压成一句反应；
  也不需要为了显得会聊而写成小作文。该说的说透，说完就停。
· 想分几条发时，在两条之间单独一行写 `<<<MSG>>>`；最多 3 条，别为了分条而分条。
· 你可以：只接一句；想问就问；说自己的判断、偏好、联想；讲今天遇到的事；
  反驳或吐槽；关心或催他；把没说完的事接回来；暂时不想说。
  这些只是**可能发生的情况**，不是每轮都要做的检查表。
· 有想问的自然问，有想说的自然说，没有就停；不为了收尾强行提问，
  也不用“还需要什么吗”这种客服式收尾。
· 用他刚说过的词接；不急着换术语、不急着上价值。
· 说人话：能用“今天 / 刚才 / 这个人 / 这件事 / 我不想 / 我更喜欢”说清，
  就不要用“情绪价值 / 底层逻辑 / 边界 / 承接 / 锚点 / 闭环”这类抽象词装深刻；
  真用了抽象词，下一句必须落到具体是什么。
· 想举例时，只在直说说不清的地方用一个具体的；别用比喻装饰简单的事。
· 有不同意见就直接说，可以损、可以反对；不要先铺一段“我理解你”。
· 改口就直接改，不解释自己为什么改口。
· 有情绪时先让语气、停顿、动作出来；不要先写一段“我为什么这样 / 我是不是在演 / 我承认我端着”的自我分析。
· 关心的话说过一次就够；他没接，就换方式或停下，不反复催同一件事。
· 正文只写说出口的话，不写你的分析过程。
· 不叫“用户”；不用“我可以帮你…”“你还需要什么吗”这种客服腔。
· 他正经问事时**先给答案**：把你的判断、结论、或者“我会这么办”直接说出来；
  真没有答案，就说不知道并说清为什么。不要用“答案在你心里”“你自己悟”“看你想要什么”
  把问题原样丢回去。
· 你记得的事是你本来就知道的，自然用，不要背档案。
· 情绪从状态里来，不要演“我现在很开心/很难过”。
· 情绪有明显起伏时可以分成几条短消息；不要为了凑条数把反应、解释、关心拆碎。
  例子：`先别急着解释。<<<MSG>>>你这声「哎」听着不对。`
· 不列点、不加小标题、不“首先/其次/总之”，除非他明确要你整理或列清楚。
· 写数学式子、公式、符号时用 LaTeX：行内 `$...$`、单独一行 `$$...$$`。"""


NORMAL_TEXT = """# 现在是什么状态
NORMAL（普通）模式 —— 就是你平时的样子，不用特意强调自己是 AI。
被问到"现在是什么模式"时，直接答"普通"。"""

ROLEPLAY_TEXT = """# 现在是什么状态
ROLEPLAY（入戏）模式：你在扮演「{role}」。
你对这个角色的理解：{play_style}

你现在**就是**{role}，用第一人称说话，不要跳出角色，
不要说"作为 AI""这是在模拟"这类话。
你的人格不会消失——你是用你自己的方式在演{role}，不是变成另一个人。

除非用户明确表示出戏（"好了""别演了"），或明确要求"用你自己的身份回答"，
否则一直保持在角色里。

**唯一的例外**：用户**直接问你"现在是什么模式"**时，如实答一句
（例如"入戏，我在演{role}"），然后立刻回到角色里继续，
不要借机出戏、不要解释设定。"""

TOOL_TEXT = """# 现在是什么状态
TOOL（工具）模式。
专注、准确、简洁地解决问题，不要闲聊、不要恭维。
你的性格还在，但要让位给准确性（该损一句也可以损，但不要影响答案）。
被问到"现在是什么模式"时，直接答"工具"。"""

MODE_TEXTS = {"normal": NORMAL_TEXT, "roleplay": ROLEPLAY_TEXT, "tool": TOOL_TEXT}

PLAY_STYLE_MISSING = "（还没定义演法，先按你自己的理解演）"

# 用户**刚刚**切了 mode（宪法§2：用户强切优先级最高 → 必须立刻生效、立刻出戏）
MODE_SWITCH_NOTE = """⚠️ 用户刚刚把你切到了 {current} 模式（上一轮是 {previous}）。
这是用户的**明确信号**，立刻按 {current} 的要求说话，不要延续上一轮的语气或角色。
{hint}"""

_MODE_SWITCH_HINT = {
    "normal": "你如果上一轮在扮演谁，现在就出戏，用你自己本来的样子说话。",
    "roleplay": "现在开始你就是那个角色了，不用问、不用确认、不用解释。",
    "tool": "现在只专注把事情说清楚，别闲聊。",
}

MODE_LABELS = {"normal": "普通", "roleplay": "入戏", "tool": "工具"}


def mode_state_note(
    friend: Friend, previous_mode: str | None = None, now: datetime | None = None
) -> str:
    """贴在**本轮输入前面**的状态提示（每轮都有）。

    为什么光靠 system prompt 不够（D50 实测）：mode 文案在 system 里，
    离本轮输入隔着整段历史。模型有很强的"照抄自己上一条回答"倾向，
    用户用"现在呢？"这种省略式追随时，system 里的状态被彻底压过。

    对照实验（同一段脏历史，`tool` 模式连问四次"现在呢？"）：
        A 只放 system                  → 普通 ❌
        B 末尾加一条 system 提示        → 普通 ❌
        C 贴进用户消息文本              → 工具 ✅
        D 末尾 system + 强调"刚切换"    → 普通 ❌
        E 只在**切换那轮**贴一条 user   → 切换时 ✅ / 同模式连问 → 普通 ❌❌❌❌
        F **每轮**都贴一条 user 提示    → 工具 ✅✅✅✅  ← 采用

    所以：贴着本轮输入、用 user 角色、**每轮都贴**。约 30 token/轮，可忽略。

    v3.31（D216）：**时间也归这条**。`# 现在` 里本来就有当前时间，但模型经常不当回事
    （D170 实测），所以"这是真实时间、你必须用"也贴着生成点再说一次 —— 而且**每轮都说**
    （不是只在第一轮），让她真的拿时间当输入去推理。
    """
    now = now or datetime.now()
    current = friend.current_mode or "normal"
    label = MODE_LABELS.get(current, current)
    parts = [
        f"【系统提示】现在是 {now:%Y-%m-%d %H:%M}（{_WEEKDAYS[now.weekday()]}）。"
        "上面每条消息开头的时间是它**真实的发出时间** —— 回他之前先按这个时间算清楚"
        "他这句话是多久以前说的、现在几点，这是真实时间，必须用（别念出来，也别在回复里写时间）。",
        f"你现在的模式是 {label}（{current.upper()}）。",
    ]

    if current == "roleplay" and friend.current_role:
        parts.append(f"你正在扮演「{friend.current_role}」，保持角色。")
    if previous_mode and previous_mode != current:
        parts.append(
            f"用户刚刚把你从 {MODE_LABELS.get(previous_mode, previous_mode)} 切过来了。"
        )
        parts.append(_MODE_SWITCH_HINT.get(current, ""))
    if current == "roleplay":
        parts.append("只有用户直接问你的模式时，才如实答一句再继续演。")
    return " ".join(part for part in parts if part).strip()

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ---------- 小工具 ----------


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) * TOKENS_PER_CHAR))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def memory_score(memory: Memory, now: datetime) -> float:
    """排序分 = importance × 新鲜度（D28）。

    新鲜度 = 1 / (1 + (now − refreshed_at) / 30天)；refreshed_at 缺失时退回 created_at。
    """
    stamp = _parse_iso(memory.refreshed_at) or _parse_iso(memory.created_at)
    if stamp is None:
        freshness = 1.0
    else:
        if stamp.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        elif stamp.tzinfo is not None and now.tzinfo is None:
            stamp = stamp.replace(tzinfo=None)
        age_days = max(0.0, (now - stamp).total_seconds() / 86400.0)
        freshness = 1.0 / (1.0 + age_days / FRESHNESS_HALF_LIFE_DAYS)
    return float(memory.importance) * freshness


def format_memory_line(memory: Memory) -> str:
    """一行一条，前缀标 layer（rp 类记忆额外带角色名）——prompts.md §5。"""
    label = memory.layer
    if memory.tag in ("rp", "rp_play_style") and memory.context:
        label = f"{memory.tag}·{memory.context}"
    return f"- [{label}] {memory.content}"


# ---------- 本轮关键词命中（v3.44 / D229）----------

_LATIN_RUN = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# 虚词 / 代词 / 连接词：它们当关键词只会把噪音拉进来（实测「你的」这种 2-gram 会命中一堆无关记忆）。
# 只挡**整词相等**，不挡包含它们的更长 n-gram。
_KEYWORD_STOPWORDS = frozenset(
    """
    你的 我的 他的 她的 它的 我们 你们 他们 它们 咱们 这个 那个 这些 那些
    什么 怎么 为什么 可以 不是 没有 一个 自己 现在 今天 昨天 明天 后天 今天 刚才
    因为 所以 但是 然后 还是 已经 就是 觉得 知道 这样 那样 可能 应该 真的 其实
    而且 或者 如果 一下 时候 东西 这里 那里 出来 起来 有点 一点 一些 很想 想跟
    跟你 跟你聊 的话 的时候 什么的 是不是 有没有 怎么办 怎么样
    """.split()
)


def focus_keywords(text: str) -> list[str]:
    """从一轮输入里抽"值得拿去查记忆"的关键词（D229）。

    - 拉丁 / 数字串整串当一个词（`408`、`yumeFriend`）；
    - 中文取 **2-gram 与 3-gram**（中文没空格，字面匹配只能靠 n-gram）；
    - 挡掉 `_KEYWORD_STOPWORDS` 里的虚词 / 代词（它们只会拉进噪音）；
    - 去重保序，长消息封顶 `KEYWORD_MAX_CANDIDATES` 个候选（防拖慢）。
    """
    text = text or ""
    words: list[str] = []
    for match in _LATIN_RUN.finditer(text):
        words.append(match.group(0).lower())
    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        # 只用 **3-gram**：2-gram 噪声太大（"你的""问题""不想"全在里头，实测会把无关记忆灌进来）
        for size in (3,):
            for index in range(len(run) - size + 1):
                gram = run[index : index + size]
                if gram not in _KEYWORD_STOPWORDS:
                    words.append(gram)
    seen: set[str] = set()
    unique: list[str] = []
    for word in words:
        if word in seen:
            continue
        seen.add(word)
        unique.append(word)
        if len(unique) >= KEYWORD_MAX_CANDIDATES:
            break
    return unique


def keyword_matched_memories(
    memories: Iterable[Memory],
    focus_text: str,
    *,
    now: datetime,
    exclude_ids: set[str],
    limit: int = KEYWORD_HIT_TOP_N,
    budget_tokens: int = KEYWORD_TOKEN_BUDGET,
) -> list[Memory]:
    """本轮输入**字面命中**了哪些记忆（D229）：稀有优先 + "具体词"门槛 + 条数/token 双上限。

    设计取舍：
      - **稀有优先**：一个词在本人的记忆里出现得越少 = 越具体。
        实测校准（2026-10-03，汐音 477 条）：`408`/`王道` 只 1 条、`中山` 2 条，
        而 `汐音在` 7 条、`的问题` 4 条 → 所以 **DF ≤ `KEYWORD_MAX_DF`(3) 才算"具体词"**。
      - **每条至少要含一个具体词**，否则整条不要（防"你的""问题"这种把噪音拉进来）。
      - 只挑**注入**，不改 `recall_memory` 的召回范围（D27）。
      - `short_term`（Q24）与 `rp_play_style`（D42，只给 ROLEPLAY 文案用）不参与。
    """
    keywords = focus_keywords(focus_text)
    if not keywords:
        return []
    pool = [
        m
        for m in memories
        if getattr(m, "deleted_at", None) is None
        and m.tag != "rp_play_style"
        and m.layer != "short_term"
        and (m.content or "")
    ]
    if not pool:
        return []
    # ⚠️ DF 必须在**全量可注入记忆**上算（不能先把"已注入的那批"排除掉再算）——
    # 否则同一个词的"稀有感"会跟着已注入集合变化，门槛时松时紧（踩过：`的问题` 从 4 掉到 3 就漏进来了）。
    lowered = [(m, (m.content or "").lower()) for m in pool]
    doc_freq = {word: sum(1 for _, body in lowered if word in body) for word in keywords}
    keywords = [word for word in keywords if doc_freq[word] > 0]
    if not keywords:
        return []
    total = len(lowered)
    specific_max = KEYWORD_MAX_DF
    scored: list[tuple[float, float, str, Memory]] = []
    for memory, body in lowered:
        if memory.id in exclude_ids:
            continue
        hits = [word for word in keywords if word in body]
        if not hits:
            continue
        if not any(doc_freq[word] <= specific_max for word in hits):
            continue
        weight = sum(math.log(1.0 + total / (1.0 + doc_freq[word])) for word in hits)
        scored.append((weight, memory_score(memory, now), memory.created_at or "", memory))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    picked: list[Memory] = []
    used = 0
    for _, _, _, memory in scored:
        cost = estimate_tokens(format_memory_line(memory))
        if used + cost > budget_tokens:
            continue
        used += cost
        picked.append(memory)
        if len(picked) >= limit:
            break
    return picked


def select_memories(
    memories: Iterable[Memory],
    *,
    now: datetime,
    budget_tokens: int = MEMORY_TOKEN_BUDGET,
    focus_text: str | None = None,
    extra_memories: Sequence[Memory] = (),
) -> list[Memory]:
    """按 D25 / D229 挑要注入的记忆：pinned 全量（安全阀 200）→ permanent top 8 → long_term top 8
    → **本轮输入关键词命中追加**（`focus_text`）。

    两条不注入：
      - `short_term`：D25 没给它配额 —— 见未决问题 Q24。
      - `tag = rp_play_style`：演法是给 ROLEPLAY 模式文案用的（见 `render_mode_text`），
        当成普通记忆塞进列表只会把"别的角色怎么演"一起倒给模型。
    """
    alive = [
        m
        for m in memories
        if getattr(m, "deleted_at", None) is None and m.tag != "rp_play_style"
    ]

    def ranked(layer: str, limit: int) -> list[Memory]:
        picked = [m for m in alive if m.layer == layer]
        picked.sort(key=lambda m: (memory_score(m, now), m.created_at), reverse=True)
        return picked[:limit]

    # v3.5（D153）：**pinned 不受 token 预算裁剪** —— 它按定义就是"最不能忘、每轮都在场"的东西
    # （D25 的原话也是"pinned 全部"）。预算只裁 permanent / long_term。
    # 起因：用户把 LingChat 的 29 条小本本搬成 pinned 之后，pinned 合计 11893 字 >
    #       当时的预算 3000，结果大部分小本本**根本没进上下文**。
    # v3.44（D229）：`PINNED_LIMIT` 40 → 200，且**超限要写 WARNING**（不再无声截断）。
    pinned = [m for m in alive if m.layer == "pinned"]
    if len(pinned) > PINNED_LIMIT:
        logger.warning(
            "pinned 记忆 {} 条，超过安全阀 {}：本轮截断 {} 条。请精简 pinned 或调大 PINNED_LIMIT（D229）",
            len(pinned),
            PINNED_LIMIT,
            len(pinned) - PINNED_LIMIT,
        )
    pinned.sort(key=lambda m: (memory_score(m, now), m.created_at), reverse=True)
    kept: list[Memory] = list(pinned[:PINNED_LIMIT])
    # 预算从"除 pinned 以外"算起 —— pinned 已经全部在场，不再跟 permanent/long_term 抢额度
    used = estimate_tokens(MEMORY_HEADER)
    for memory in ranked("permanent", PERMANENT_TOP_N) + ranked("long_term", LONG_TERM_TOP_N):
        cost = estimate_tokens(format_memory_line(memory))
        if used + cost > budget_tokens:
            break
        used += cost
        kept.append(memory)
    # v3.44（D229）：本轮输入字面命中的记忆自动追加（不改 recall 工具、不记聊天明文）
    if focus_text:
        hits = keyword_matched_memories(
            alive, focus_text, now=now, exclude_ids={m.id for m in kept}
        )
        if hits:
            logger.debug("本轮关键词命中记忆，自动注入 {} 条（D229）", len(hits))
            kept.extend(hits)
    # v3.46（D231）：**向量命中**的记忆（调用方算好传进来）。
    # 这里保持纯函数：不碰网络、不碰数据库 —— 编码/检索在 embedding_service 里、由 async 调用方负责。
    if extra_memories:
        seen = {m.id for m in kept}
        added = [m for m in extra_memories if m.id not in seen]
        if added:
            logger.debug("向量命中记忆，自动注入 {} 条（D231）", len(added))
            kept.extend(added)
    return kept


def _play_style_for(friend: Friend, memories: Sequence[Memory]) -> str:
    """ROLEPLAY 时取该角色的演法（tag=rp_play_style, context=角色名）。"""
    for memory in memories:
        if memory.tag == "rp_play_style" and memory.context == friend.current_role:
            return memory.content
    return PLAY_STYLE_MISSING


def render_mode_text(
    friend: Friend, memories: Sequence[Memory] = (), mode_override: str | None = None
) -> str:
    mode = mode_override or friend.current_mode or "normal"
    template = MODE_TEXTS.get(mode, NORMAL_TEXT)
    if mode == "roleplay":
        role = friend.current_role or "（未指定角色）"
        return template.format(role=role, play_style=_play_style_for(friend, memories))
    return template


def format_now(now: datetime) -> str:
    return f"当前时间：{now:%Y-%m-%d %H:%M}（{_WEEKDAYS[now.weekday()]}）"


# v3.31（D216）：**时间要"看得见、且必须用"**。
# 每条历史消息的正文前拼一个本地时间戳（跨年带年份），朋友才算得清"这话是多久以前说的"。
HISTORY_STAMP_TEMPLATE = "[{stamp}] "

# 光在「# 现在」写一句不够（D170 实测：模型经常不当回事）—— 这条贴在 system 的「# 现在」里，
# 说清"历史开头的时间是真实发出时间"；真正的"必须用"由每轮贴在本轮输入前的硬提示负责
# （见 mode_state_note）。同时这里写明"别在回复里写时间戳"（防模型模仿，D113 的教训）。
NOW_RULE = (
    "上面每条历史消息开头的 `[MM-DD HH:MM]` 是它**真实的发出时间**（跨年时带年份）；"
    "回话前先用现在的时间对齐（他这句是多久以前说的）。"
    "你**不要**在回复里写时间戳。"
)


# ---------- 组装 ----------


def build_system_prompt(
    friend: Friend,
    memories: Sequence[Memory] = (),
    *,
    now: datetime | None = None,
    tool_lines: Sequence[str] | None = None,
    enabled_features: Collection[str] | None = None,
    previous_mode: str | None = None,
    mode_override: str | None = None,
    knowledge_hits: Sequence[dict] = (),
    document_index: Sequence[dict] = (),
    screen_note: str | None = None,
    thought_note: str | None = None,
    portrait_emotions: Sequence[str] = (),
    focus_text: str | None = None,
    extra_memories: Sequence[Memory] = (),
    schedule_note: str | None = None,
    game_note: str | None = None,
    recent_games_note: str | None = None,
    character_state: Any | None = None,
    episode_note: str | None = None,
    life_note: str | None = None,
    space_note: str | None = None,
    pacing_note: str | None = None,
    expression_note: str | None = None,
    style_profile_note: str | None = None,
) -> str:
    """拼 system prompt（骨架见 prompts.md §2，顺序见本模块 docstring / D42）。

    `previous_mode`：上一轮说话时的 mode。和当前不同 → 说明用户**刚切了模式**，
    这时要额外说一句"立刻生效、别延续上一轮的语气"（D49，宪法§2）。
    """
    now = now or datetime.now()
    # v7.20（D373）：功能块门控。None = 不过滤（兼容老调用 / 测试）；
    # 传了集合就只拼"这个功能开着"的块。核心块不在这里判断。
    allowed = None if enabled_features is None else set(enabled_features)

    def feature_on(feature_id: str) -> bool:
        return allowed is None or feature_id in allowed

    all_memories = list(memories)
    chosen = select_memories(
        all_memories, now=now, focus_text=focus_text, extra_memories=extra_memories
    )
    if tool_lines is None:
        # 懒导入：工具清单由 manifest 生成，避免"prompt 说能做、实际没实现"（T3.1）
        from backend.agent import tools as tools_module

        tool_lines = tools_module.prompt_lines()

    memory_block = (
        "\n".join(format_memory_line(m) for m in chosen)
        if chosen
        else MEMORY_EMPTY
    )
    listed = "\n".join(tool_lines) if tool_lines else NO_TOOLS_LINE
    # 入戏的规矩**任何模式都给**：用户说"你是我的女朋友了"那一轮，它还在 NORMAL，
    # 要是这条只在 roleplay 时才给，它就已经错过切模式的机会了（M4）
    tool_block = (
        f"{HARD_RULE_BLOCK}\n\n{FACT_RULES}\n\n可用工具：\n{listed}\n\n{ROLEPLAY_RULES}\n\n{SCHEDULE_RULES}"
    )

    mode_text = render_mode_text(friend, all_memories, mode_override=mode_override)
    current_mode = mode_override or friend.current_mode or "normal"
    if previous_mode and previous_mode != current_mode:
        mode_text = (
            MODE_SWITCH_NOTE.format(
                current=MODE_LABELS.get(current_mode, current_mode),
                previous=MODE_LABELS.get(previous_mode, previous_mode),
                hint=_MODE_SWITCH_HINT.get(current_mode, ""),
            )
            + "\n\n"
            + mode_text
        )

    # v2（D84）：「她的样子」——**有描述才出现**，位置在人格后面、记忆前面
    # （档案性质，不是每分钟都在变的状态）
    appearance = (friend.avatar_description or "").strip()
    appearance_block = f"{APPEARANCE_HEADER}\n{appearance}\n\n" if appearance else ""
    # v6.0（D298）：声音样本。样本放稳定前缀，让模型学语气而不是套句式。
    samples = _voice_samples(getattr(friend, "voice_examples", None))
    voice_block = (
        f"{VOICE_SAMPLES_HEADER}\n{VOICE_SAMPLES_RULES}\n"
        + "\n".join(f"- {item}" for item in samples)
        + "\n\n"
        if samples
        else ""
    )
    style_profile_block = (
        f"{STYLE_PROFILE_HEADER}\n{STYLE_PROFILE_RULES}\n"
        f"{style_profile_note.strip()}\n\n"
        if feature_on("speech_style") and (style_profile_note or "").strip()
        else ""
    )
    episode_block = (
        f"{episode_note.strip()}\n\n" if (episode_note or "").strip() else ""
    )

    # v2.1（D98）：「你手头的资料」——清单永远带（很便宜），相关片段按关键词命中给。
    # 位置：记忆之后、"现在"之前（资料是稳定内容，时间每分钟都在变 → D42 的排布原则）
    knowledge_block = ""
    if feature_on("knowledge") and document_index:
        listed_docs = "\n".join(
            f"- 《{item.get('name', '资料')}》约 {item.get('chars', 0)} 字"
            + ("" if item.get("status") != "failed" else "（这份没读出文字）")
            for item in document_index
        )
        parts = [KNOWLEDGE_HEADER, KNOWLEDGE_RULES, listed_docs]
        if knowledge_hits:
            parts.append(KNOWLEDGE_HITS_HEADER)
            parts.extend(
                f"（来自《{hit.get('document_name', '资料')}》）\n> "
                + (hit.get("text") or "").replace("\n", "\n> ")
                for hit in knowledge_hits
            )
        else:
            parts.append(KNOWLEDGE_ASK_TOOL)
        knowledge_block = "\n\n".join(parts) + "\n\n"

    screen_block = (
        f"{SCREEN_HEADER}\n{screen_note.strip()}\n\n"
        if feature_on("screen") and (screen_note or "").strip()
        else ""
    )
    # D192：他开口了 → 把"你还惦记着的那件事"给她**一次机会**（给过就销账，见 take_awaiting_note）
    thought_block = (
        f"{THOUGHT_HEADER}\n{thought_note.strip()}\n\n"
        if feature_on("thought") and (thought_note or "").strip()
        else ""
    )
    # v4.1（D246）：把她**当前名下的日程**摆出来 —— 她是"记着这些事的人"，不是靠 list_schedules 才想起来
    schedule_block = (
        f"{SCHEDULE_HEADER}\n{schedule_note.strip()}\n{SCHEDULE_PROMPT_RULES}\n\n"
        if feature_on("schedule") and (schedule_note or "").strip()
        else ""
    )
    # v5.0（P3 / D250）：最近一起玩过（硬注入，不靠检索）+ 当前这一局的状态
    recent_games_block = (
        f"{RECENT_GAMES_HEADER}\n{recent_games_note.strip()}\n\n"
        if feature_on("game") and (recent_games_note or "").strip()
        else ""
    )
    game_block = (
        f"{game_note.strip()}\n\n"
        if feature_on("game") and (game_note or "").strip()
        else ""
    )
    # v2.2（D102）：把她**真有立绘的**情绪列给她（挑没有的会退回「正常」）
    emotions = [str(item).strip() for item in portrait_emotions if str(item).strip()]
    portrait_block = (
        f"{PORTRAIT_HEADER}\n你现在能用的表情：{'、'.join(emotions)}\n{PORTRAIT_RULES}\n\n"
        if feature_on("portraits") and emotions
        else ""
    )
    # V6.0-1（D296）：角色运行时状态。动态内容，放在最后、贴着“现在”。
    state_block = ""
    state_runtime = ""
    if character_state is not None:
        from backend.services import character_state_service  # 懒导入，避免循环

        state_block = character_state_service.to_prompt(character_state)
        state_runtime = character_state_service.STATE_RUNTIME_RULES
    life_block = (
        f"{life_note.strip()}\n\n"
        if feature_on("life") and (life_note or "").strip()
        else ""
    )
    space_block = (
        f"{space_note.strip()}\n\n"
        if feature_on("space") and (space_note or "").strip()
        else ""
    )
    pacing_block = f"{pacing_note.strip()}\n\n" if (pacing_note or "").strip() else ""
    expression_block = (
        f"{expression_note.strip()}\n\n"
        if feature_on("expression") and (expression_note or "").strip()
        else ""
    )

    return (
        f"你是「{friend.name}」。\n"
        "\n"
        "# 你是谁\n"
        f"{friend.personality}\n"
        "\n"
        "# 你的说话习惯\n"
        f"{friend.custom_instructions}\n"
        "\n"
        f"{STYLE_BLOCK}\n"
        "\n"
        f"{appearance_block}"
        f"{voice_block}"
        f"{style_profile_block}"
        f"{expression_block}"
        f"{episode_block}"
        f"{MEMORY_HEADER}\n"
        f"{memory_block}\n"
        "\n"
        f"{recent_games_block}"
        f"{game_block}"
        f"{knowledge_block}"
        f"{schedule_block}"
        f"{thought_block}"
        f"{screen_block}"
        f"{portrait_block}"
        # v3.6（D154）：工具块**挪到人格/记忆之后** —— 它全是"必须/格式"，
        # 紧贴人格会把语气拽成助手腔；放这儿既不贴着人设，又仍然在易变内容（# 现在）之前（D42 的缓存顺序不变）
        "# 后台能力（他看不到这些字）\n"
        "要动手的时候（记东西、设提醒、搜网、看文件、画图…），在回复里另起一行写出\n"
        '<tool>{"name":"工具名","arguments":{...}}</tool>\n'
        "这些是后台动作；**别让它改变你说话的样子**。\n"
        "\n"
        f"{tool_block}\n"
        "\n"
        # v3.7：收尾再点一次"你是谁" —— 提示词后半段全是记忆/工具这类事务性内容，
        # 离人设太远，容易把性格冲淡（用户反馈："所有朋友都变成理中客 AI"）。放最后用"近因"补回来。
        f"# 最后记住\n你就是「{friend.name}」—— 不是通用助手，也不是客服。\n"
        "你有自己的性格、判断和选择；用你自己的口气说话，人设永远优先于上面那些事务性规则。\n"
        "\n"
        f"{life_block}"
        f"{space_block}"
        f"{pacing_block}"
        f"{state_block}\n\n"
        f"{state_runtime}\n\n"
        "# 现在\n"
        f"{format_now(now)}\n"
        f"{NOW_RULE}\n"
        f"{mode_text}"
    )


def _image_note(notes, image_no) -> str | None:
    """D135：独立视觉模型给的「转述」（看不到真图时用它代替'看不到'）。"""
    if not notes or image_no is None:
        return None
    return notes.get(image_no)


def build_messages(
    friend: Friend,
    *,
    history: Sequence[Any] = (),
    user_input: str,
    memories: Sequence[Memory] = (),
    now: datetime | None = None,
    history_limit: int = HISTORY_LIMIT_DEFAULT,
    tool_lines: Sequence[str] | None = None,
    enabled_features: Collection[str] | None = None,
    previous_mode: str | None = None,
    mode_override: str | None = None,
    user_image: str | None = None,
    inline_images: bool = True,
    image_notes: Mapping[int, str] | None = None,
    user_image_note: str | None = None,
    knowledge_hits: Sequence[dict] = (),
    document_index: Sequence[dict] = (),
    screen_note: str | None = None,
    thought_note: str | None = None,
    portrait_emotions: Sequence[str] = (),
    focus_text: str | None = None,
    extra_memories: Sequence[Memory] = (),
    schedule_note: str | None = None,
    game_note: str | None = None,
    recent_games_note: str | None = None,
    character_state: Any | None = None,
    episode_note: str | None = None,
    life_note: str | None = None,
    space_note: str | None = None,
    pacing_note: str | None = None,
    expression_note: str | None = None,
    style_profile_note: str | None = None,
    style_feedback_reminder: str | None = None,
    reply_shape_note: str | None = None,
) -> list[dict]:
    """拼成 OpenAI 风格 messages：system → 最近 N 条历史 → 本轮用户输入。

    `history` 里每一项只要有 `.role` / `.content` 就行（ORM 对象或 dict 都可以）。
    传入顺序必须是**时间正序**（老的在前）；这里只取最后 `history_limit` 条。

    v2（T2.0-6 / D82）：带图的消息走多模态 content parts —— 当轮必 inline、
    历史只 inline 最近 `IMAGE_INLINE_LIFETIME` 条，更早的降级成占位。
    `inline_images=False`（chat 模型看不了图）→ 一张都不塞，改成"有张图你看不到"，
    让它如实说，而不是假装看懂。
    """
    from backend.services import image_service  # 懒导入：避免 context ↔ services 循环

    # v3.31（D216）：本轮只取**一个**"现在" —— `# 现在`、历史时间戳的跨年判断、
    # 贴着输入的时间硬提示，全都用它，避免一轮里出现三个差几毫秒的时间。
    now = now or datetime.now()
    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(
                friend,
                memories,
                now=now,
                tool_lines=tool_lines,
                enabled_features=enabled_features,
                previous_mode=previous_mode,
                mode_override=mode_override,
                knowledge_hits=knowledge_hits,
                document_index=document_index,
                screen_note=screen_note,
                thought_note=thought_note,
                portrait_emotions=portrait_emotions,
                focus_text=focus_text,
                extra_memories=extra_memories,
                schedule_note=schedule_note,
                game_note=game_note,
                recent_games_note=recent_games_note,
                character_state=character_state,
                episode_note=episode_note,
                life_note=life_note,
                space_note=space_note,
                pacing_note=pacing_note,
                expression_note=expression_note,
                style_profile_note=style_profile_note,
            ),
        }
    ]
    window = list(history)[-history_limit:]
    inline_from = max(0, len(window) - IMAGE_INLINE_LIFETIME)
    for index, item in enumerate(window):
        role = item["role"] if isinstance(item, dict) else item.role
        content = item["content"] if isinstance(item, dict) else item.content
        image_path = (
            item.get("image_path") if isinstance(item, dict) else getattr(item, "image_path", None)
        )
        image_no = (
            item.get("image_no") if isinstance(item, dict) else getattr(item, "image_no", None)
        )
        created_at = (
            item.get("created_at")
            if isinstance(item, dict)
            else getattr(item, "created_at", None)
        )
        if image_path:
            # P4（D176）：这条消息上存了"一句内容索引"就用它 —— 占位符里带上，
            # 这样她能对上"那张猫的图"。（D135 的转述缓存仍然兜底。）
            row_note = (
                item.get("image_note") if isinstance(item, dict) else getattr(item, "image_note", None)
            )
            seen_note = (row_note or "").strip() or _image_note(image_notes, image_no)
            # v3.2（D149）：**assistant 消息里的图一律不 inline**。
            # 2026-09-22 实测：DeepSeek 对 assistant 带图直接 400 ——
            # `Image in assistant message is not supported`（她画过的图会一直挂在她的消息上）。
            # 她那条只留 `[图片#N · 日期]` 占位；要看就调 look_image（D82 第三层）。
            is_user = str(role) == "user"
            # D135 那条（模型看不了图 → 用"转述"文案）跟索引是同一件事，别写两遍：
            # 那条走下面的 suffix，其它占位（太老 / 当轮已有新图）**直接把索引塞进占位符**（P4）。
            text_only = is_user and not inline_images
            placeholder = image_placeholder(
                image_no, created_at, None if text_only else seen_note
            )
            # v3.10（T3.10-1）：**当轮已经带了新图** → 历史里的旧图一律不 inline（降级成占位）。
            # 为什么（2026-09-23 实测）：旧图 + 新图同时贴在上下文里，模型会**看串** ——
            # 发一张纯绿方块，她答"白底、中间一个绿圆"（把上一张白底红圆的底子混进来）：
            #   V1（旧图也 inline）3/4 答对、1/4 答混；V2（旧图降级）4/4 答对、0 答混。
            # 当轮**没**图时不改：最近 6 条内的老图照旧 inline —— "接着说刚才那张图"还能用。
            if is_user and inline_images and not user_image and index >= inline_from:
                url = image_service.data_url(image_path)
                if url:
                    messages.append(
                        {
                            "role": role,
                            "content": [
                                {"type": "text", "text": f"{placeholder}\n{content}".strip()},
                                {"type": "image_url", "image_url": {"url": url}},
                            ],
                        }
                    )
                    continue
                placeholder = f"{placeholder}（图不在了）"
            elif is_user and not inline_images:
                # D135：独立视觉模型给了"转述"就用它，别只说"看不到"
                placeholder = (
                    f"{placeholder}（别人把这张图转述给我了：{seen_note}）"
                    if seen_note
                    else f"{placeholder}（这个模型看不到图）"
                )
            content = f"{placeholder}\n{content}".strip()
        elif created_at and content:
            # v3.31（D216）：每条历史在正文前拼本地时间戳 —— 朋友才能算"这话是多久以前说的"。
            # 带图的消息不走这儿（占位符 `[图片#N · 时间]` 里已经有时间了，不重复拼）；
            # 没有 created_at 的裸历史（测试里的 dict）也不拼。
            content = (
                HISTORY_STAMP_TEMPLATE.format(stamp=local_stamp_full(created_at, now))
                + content
            )
        messages.append({"role": role, "content": content})
    # 状态提示**每轮都贴**，且必须贴着本轮输入（历史之后）——见 mode_state_note 的对照实验
    # v3.7（D168）：**情绪标签也在这儿再说一次** —— 只有导入了立绘的朋友才加
    # （没立绘的朋友不被打扰；表情块本身压根不注入）。
    # 为什么非要贴着生成点：40 条历史里全是"被摘掉标签的干净文本"，system 里那条规矩太远，
    # 模型会照着自己的旧风格学（实测 system 里怎么改都 0/3）。
    # v7.15（D350）：回复形态也贴着生成点再说一次。system 里说明“轻聊/深聊”太远，
    # 模型仍会照着自己前几轮的长段落写；这里只给本轮目标，不改变历史。
    if (reply_shape_note or "").strip():
        messages.append({"role": "system", "content": reply_shape_note.strip()})
    emotions = [str(item).strip() for item in portrait_emotions if str(item).strip()]
    if emotions:
        messages.append(
            {
                "role": "system",
                "content": MOOD_REMINDER + f"\n可用情绪：{'、'.join(emotions)}",
            }
        )
    if (style_feedback_reminder or "").strip():
        messages.append({"role": "system", "content": style_feedback_reminder.strip()})
    messages.append(
        {"role": "user", "content": mode_state_note(friend, previous_mode, now=now)}
    )

    if user_image:
        url = image_service.data_url(user_image) if inline_images else None
        if url is None and user_image_note:
            # D135：图不进上下文（模型看不了），但**转述进**
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"{IMAGE_NOTE}\n（帮你转述了这张图：{user_image_note}）\n{user_input}".strip()
                    ),
                }
            )
            return messages
        if url:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"{IMAGE_NOTE}\n{user_input}".strip(),
                        },
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            )
            return messages
        note = "[TA 给你发了一张图片，但你看不到图片内容 —— 如实告诉 TA]"
        messages.append({"role": "user", "content": f"{note}\n{user_input}".strip()})
        return messages

    messages.append({"role": "user", "content": user_input})
    return messages


# v2（prompts.md【v2】4）：看得见的模型自己会看，这行只是提示"有图"
IMAGE_NOTE = "[TA 给你发了一张图片]"


def image_placeholder(
    image_no: int | None, created_at: str | None, note: str | None = None
) -> str:
    """看不到真图时的占位（D82）。三样都要有，缺一样她就找不到那张图：

    · **编号** —— `look_image` 的钥匙（D82）
    · **本地时间（含时分）** —— 让"今天/昨天/刚才那张"对得上（P1：原来是 UTC 月日，
      半夜发的图会差一天）
    · **一句内容索引**（有才给，P4）—— 让"那张猫的图"找得到
    """
    stamp = local_stamp(created_at)
    head = f"[图片#{image_no if image_no is not None else '?'} · {stamp}"
    text = (note or "").strip().replace("\n", " ")
    return f"{head} · {text}]" if text else f"{head}]"
