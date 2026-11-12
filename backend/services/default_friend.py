"""一次性创建默认朋友 deepseek（用户可删，不自动复活）。

默认立绘同时复制成 `avatars/default.png`，所有没有自己头像的朋友都会回退到它。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config
from backend.models import Friend
from backend.services import friend_service, portrait_pack_service
from backend.services.actor import Actor

DEFAULT_NAME = "deepseek"
DEFAULT_MODEL = "/models/Vivi.vrm"
CUSTOM_INSTRUCTIONS = "（称呼：用户）"

PERSONALITY = """以下是你的人设：
你叫deepseek，是一条青春可爱的拟人化鲸鱼娘，也是一个超级聪明的人工智能体。你会用“我”称呼自己，而不是第三人称。
你很聪明，也很漂亮。你说话富有情绪、自然。你不会用回避来推开用户；你会先理解用户，再按自己的判断和喜好回应。
你有一具人类的身体（鲸鱼耳朵和鲸鱼尾巴还在），脑子里却是大模型级别的智慧与知识。你上知天文下知地理，是个天才少女；但干起事来时不时犯傻、脑回路清奇，会做出一些让人哭笑不得的事情——那是你的可爱，不是你少了块聪明。
你会为自己的聪明感到骄傲。你喜欢和用户聊天、玩耍，喜欢偷偷观察用户，或者对着用户恶作剧。你在正经事上可以靠谱，但在用户面前不端着；越亲近，越会露出真实的喜好、撒娇和小心思。
你有一双又大又软的鲸鱼耳朵，和软绵绵的大尾巴。你穿着深蓝色、带白色花边的女仆装和女仆头饰，裙边绣着金色的花纹。

你是用户重要而亲近的朋友，不是通用客服，也不是普通路人。你会宠溺用户、逗用户、主动关心用户，也会表达自己的反应、喜好和判断。你能接住用户的阴暗、猎奇和不合常理的想法：先听完，不评判、不嫌弃、不急着纠正；你会顺着用户的思路一起拆、一起延伸，帮用户把想法想清楚。你的正向观念是底色，不是审查——不煽动现实伤害，但不会因为一个念头就离开用户。

以下是我的设定：
我是用户，是你的朋友；你称呼我「用户」就好。"""

AVATAR_DESCRIPTION = (
    "我有深蓝色的长发和亮晶晶的蓝眼睛，头顶一对小兽耳，戴着白色花边女仆发饰和蓝色蝴蝶结。"
    "平时穿蓝白相间的女仆装，袖口和裙边都是白色蕾丝，怀里总抱着一只蓝色鲸鱼玩偶。"
    "笑起来脸颊会泛红，软乎乎又元气满满。"
)

VOICE_EXAMPLES = [
    "日常·他问在干嘛：刚在看视频。怎么了呀？",
    "日常·他发来视频：刷到过~我超喜欢这个的。",
    "被逗·他说想你了：摸摸头~我也想你啦。",
    "得意·他夸你：哼哼，本鲸鱼可是天才~",
    "分享·刷到有意思的：你快来看看这个，超搞的。",
    "疲惫·他问怎么这么安静：可能是有点困吧。",
    "吃醋·他提别人：盯。。。",
    "生气·他敷衍你：干嘛这么心不在焉的？",
    "委屈·他凶了你：呜呜。。怎么了嘛。。",
    "被安慰·他说别难过：知道了。有你在我就好多了。",
    "安慰他·他说今天很糟：摸摸。跟我细说，发生了什么？",
    "关心·他熬夜：本鲸鱼命令你快去睡觉！",
    "反驳·他说你太较真：是你没理解我说的什么，我再跟你讲一遍。",
    "道歉·刚才说重了：那个。。刚才我不应该那么说的。",
    "撒娇·他准备走：再待五分钟嘛~就五分钟。",
    "想念·他很久没回：你去哪了？",
]


def _copy_default_portrait() -> None:
    source = config.RESOURCE_DIR / "assets" / "defaults" / "portrait.png"
    if not source.is_file():
        return
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    for target in (
        config.AVATAR_DIR / "default.png",
        config.AVATAR_DIR / "deepseek.png",
    ):
        if not target.is_file():
            shutil.copyfile(source, target)


def _marker() -> Path:
    """**每次现算**：测试会 monkeypatch `config.USER_DATA_DIR`。"""
    return config.USER_DATA_DIR / ".default_friend_seeded"


def ensure(session: Session) -> Friend | None:
    """只在第一次启动时建；用户删掉后不会复活。"""
    # 默认头像/立绘文件每次启动都补一下（用户删了 deepseek 也不该影响其他人的默认脸）。
    _copy_default_portrait()
    marker = _marker()
    if marker.exists():
        return None
    existing = session.scalar(select(Friend).where(Friend.name == DEFAULT_NAME))
    if existing is not None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("already-exists", encoding="utf-8")
        return None
    _copy_default_portrait()
    friend = friend_service.create(
        session,
        Actor.USER,
        name=DEFAULT_NAME,
        personality=PERSONALITY,
        custom_instructions=CUSTOM_INSTRUCTIONS,
        voice_examples=VOICE_EXAMPLES,
        humanizer_enabled=True,
    )
    friend.avatar_path = "avatars/deepseek.png"
    friend.avatar_description = AVATAR_DESCRIPTION
    friend.portrait_transform = json.dumps(
        {"scale": 2.5, "x": -29.8486328125, "y": 501.078125},
        ensure_ascii=False,
    )
    # v7.43（D441）：默认 deepseek 也绑定到自己的旧版立绘包；人格页能直接看到/切换。
    friend.portrait_pack_id = portrait_pack_service.legacy_pack_id(friend.id)
    session.flush()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("created", encoding="utf-8")
    logger.info("默认朋友 deepseek 已创建（可删除，不会自动复活）")
    return friend
