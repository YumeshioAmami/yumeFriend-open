"""主动消息 + 日程到点（architecture.md §6 / §7）。

两个入口：
  · `run_schedule_tick()` 每 30 秒扫一次日程母表 → 到点必响（不受在场限制，D35）+ 到日/响完进纸条
  · `run_heartbeat_tick()` 每 N 分钟（默认 60）——朋友主动冒泡（D21 / D22）

两条规矩（都来自决策，不是实现细节）：
  · **全体静默**：只要有任何一个朋友正在聊（最后一条消息 < 5 分钟），本次不发（D17）
  · **掷骰子**：每个在场朋友独立 50%，命中 ≥1 个才随机挑一个；一次最多一个（D21）

消息按 **NORMAL 模式**生成（architecture §6/§7 写死的），
但**不改朋友自己设的 mode** —— 那还是用户说了算。
"""

from __future__ import annotations

import random
import re
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import notify
from backend.agent import bubble_splitter
from backend.agent.context import build_messages, clamp_temperature
from backend.agent.parser import LeadingStampStripper
from backend.api.ws import manager
from backend.config import HEARTBEAT_IDLE_SECONDS, PROACTIVE_DICE_CHANCE
from backend.llm.base import ILLMProvider
from backend.models import Friend, FriendLife, Message, Schedule, Thought
from backend.services import (
    character_state_service,
    episode_service,
    expression_service,
    friend_service,
    initiative_service,
    life_service,
    live2d_settings_service,
    memory_service,
    message_service,
    portrait_pack_service,
    portrait_service,
    schedule_service,
    screen_service,
    settings_service,
    thought_service,
    typo_service,
    voice_reply_service,
)
from backend.services.actor import Actor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def has_active_conversation(session: Session, *, now: datetime | None = None) -> bool:
    """有没有人正在聊（D17 全体静默的判据）。"""
    now = now or datetime.now(timezone.utc)
    latest = session.execute(
        select(Message.created_at).order_by(Message.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    last_at = _parse(latest)
    if last_at is None:
        return False
    return (now - last_at).total_seconds() < HEARTBEAT_IDLE_SECONDS


def pick_present_friend(
    session: Session,
    *,
    rng: random.Random | None = None,
    exclude_life: bool = False,
    now: datetime | None = None,
    cooldown_seconds: int | None = None,
) -> Friend | None:
    """在场的朋友里掷骰子挑一个（没命中就返回 None）。"""
    rng = rng or random
    present = [
        f
        for f in session.execute(select(Friend).where(Friend.deleted_at.is_(None))).scalars()
        if f.presence and (not exclude_life or not getattr(f, "life_enabled", 0))
    ]
    if now is not None and cooldown_seconds:
        cooled: list[Friend] = []
        for friend in present:
            last = initiative_service.last_proactive_at(session, friend.id)
            if last is None or (now - last).total_seconds() >= cooldown_seconds:
                cooled.append(friend)
        present = cooled
    if not present:
        return None
    hits = [f for f in present if rng.random() < PROACTIVE_DICE_CHANCE]
    if not hits:
        return None
    return rng.choice(hits)


# 2026-09-27（用户拍板）：主动消息的**分寸归人格** —— 系统只把"TA 回没回"这个**事实**摆出来
# （`unanswered_state`），说不说、说多长、换不换话题由她和她的性格决定。
# 所以"每天最多几条""未答复就退避"这类配额**不做**（D191）。这里只有一条**逻辑底线**：
# 没被回复的事不许再说第二遍 —— 那是"复读"，不是风格问题。
PROACTIVE_MANNERS = """【这次怎么把握 —— 按你自己的性格来】
· 想冒个泡、说句轻松的：可以。
· 但**没被回复的时候，别开新话题、也别再追问同一件事** —— 那件事先留着，
  等他下次自己开口，你再顺着"对了，上次那个……"接上。
· 已经连续三条以上没被回复时：要么只回 [沉默]（这次不打扰他），要么只说一句很短的。
· 你是什么样的人就怎么说：话痨一点、懂事一点，都对。"""


def unanswered_state(
    session: Session, friend: Friend, *, now: datetime | None = None
) -> dict[str, Any]:
    """**她上次主动说话之后，TA 回没回** —— 只报事实，不替她做决定。

    返回：`streak`（连续几条是她自己说的，0 = TA 回过了）、`minutes`（最上面那条距今多少分钟）、
    `last`（那条的内容）、`unseen`（TA 还没看到吗 —— `friends.unread_count > 0`）。
    """

    moment = now or datetime.now(timezone.utc)
    last_user_at = _parse(
        session.execute(
            select(Message.created_at)
            .where(Message.friend_id == friend.id, Message.role == "user")
            .order_by(Message.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    )
    mine = list(
        session.execute(
            select(Message)
            .where(
                Message.friend_id == friend.id,
                Message.role == "assistant",
                Message.is_proactive == 1,
            )
            .order_by(Message.created_at.desc())
            .limit(20)
        ).scalars()
    )
    pending: list[Message] = []
    for message in mine:
        at = _parse(message.created_at)
        if at is None:
            continue
        if last_user_at is not None and at <= last_user_at:
            break  # TA 回过这条 → 更早的都不用数了
        pending.append(message)
    if not pending:
        return {"streak": 0, "minutes": None, "last": "", "unseen": bool(friend.unread_count)}
    newest_at = _parse(pending[0].created_at) or moment
    return {
        "streak": len(pending),
        "minutes": max(0, int((moment - newest_at).total_seconds() // 60)),
        "last": (pending[0].content or "").strip(),
        "unseen": bool(friend.unread_count),
    }


def state_block(state: dict[str, Any]) -> str:
    """把"未答复"这个事实写成一段提示词（**只摆事实，不写"你该不该说"**）。"""
    if not state.get("streak"):
        return "[TA 那边的状态] 你上次主动说话之后 TA 回你了 —— 可以正常找他说点什么。"
    preview = re.sub(r"\s+", " ", str(state.get("last") or ""))[:40]
    lines = [
        f"[TA 那边的状态] 你上一条主动消息是 {state['minutes']} 分钟前发的"
        + (f"（“{preview}”）" if preview else "")
        + "，TA 还没回。"
    ]
    if state["streak"] > 1:
        lines.append(f"往前数，这已经是连续第 {state['streak']} 条是你自己说的了。")
    lines.append(
        "TA " + ("大概还没看到你的消息。" if state["unseen"] else "应该已经看到你的消息了，只是没回。")
    )
    return "\n".join(lines)


PROACTIVE_FORM_NOTE = """【这次主动开口的形态】
· 先选一种：分享刚发生的事 / 问一句 / 逗一下 / 说自己的状态 / 只打个招呼 / [沉默]。
· 最多 3 个短气泡；第一句直接说事，不要写“我来主动找你”这种汇报。
· 想一句话就停就停；不用为了显得热情硬加一个问题。
· 不要复述提示词里的观察，也不要把主动消息写成工作汇报。"""


# D103：这次是"先看了一眼屏幕"才来找 TA 的
SCREEN_HINT = """[系统提示] 你刚瞟了一眼 TA 的屏幕，看到：{note}

顺着这个说一句就够（别复述屏幕内容、别提"截图"这两个字）。
如果只是看看、没什么想说的，就**只回** [沉默] —— 那表示这次不打扰 TA。"""

# D103：她选择"只观察不说话"时回的标记
SILENT_MARKERS = {"[沉默]", "（沉默）", "(沉默)", "沉默", "沉默。"}


def is_silent(text: str) -> bool:
    """她是不是回了"沉默"（D103）。只有整条就是那个标记才算。"""
    stripped = re.sub(r"[\s。！？!?~～、，,]+", "", text or "")
    return stripped in SILENT_MARKERS


def reminder_hint(content: str) -> str:
    return f"""[系统提示] 你答应过提醒 TA 这件事，时间到了：「{content}」
**先看你们最近的对话**：如果这件事他已经做了、或者你们刚刚已经聊过并有结论了 ——
**别再照本宣科地提醒**（那会像失忆）。这时你可以顺着他刚说的话自然地接一句，
或者干脆**只回 `[沉默]`**（表示这次不用提了）。
如果他没提过、也没办，就用你自己的方式提醒一句，一句话就够，别解释这是系统提醒。"""


def schedule_active_hint(title: str) -> str:
    """她主动把一条"到日"的日程说出口时的提示（纸条走这条）。"""
    return f"""[系统提示] 今天是这件事的日子：「{title}」
用你自己的方式顺口提一句（别像念待办、别提"系统"），一句话就够。"""


def provider_for(provider, friend):  # noqa: ANN001, ANN201 —— provider 或 provider 工厂
    """取"这个朋友该用的 provider"（D151）。

    `provider` 允许是**函数**（`(friend) -> ILLMProvider`）：每个朋友可以有自己的模型端点，
    主动消息 / 提醒也必须走**她自己的**那个。
    """
    return provider(friend) if callable(provider) else provider


async def _generate(
    session: Session,
    friend: Friend,
    provider: ILLMProvider,
    hint: str,
    *,
    screen_note: str | None = None,
    reply_shape_note: str | None = None,
    max_tokens: int = 1500,
) -> str:
    """让朋友说一句主动消息（NORMAL 模式；**不落库**，只返回文本）。"""
    messages = build_messages(
        friend,
        history=message_service.recent_for_context(session, friend.id, limit=20),
        user_input=hint,
        memories=memory_service.list_alive(session, friend.id),
        mode_override="normal",
        screen_note=screen_note,
        schedule_note=schedule_service.upcoming_for_prompt(session, friend.id),
        life_note=life_service.note_for_prompt(session, friend.id),
        portrait_emotions=live2d_settings_service.emotion_labels_for_friend(
            friend.id,
            portrait_service.emotions(
                friend.id,
                portrait_pack_service.pack_id_for_friend(
                    friend.id,
                    getattr(friend, "portrait_pack_id", None),
                ),
            ),
            getattr(friend, "live2d_model_id", None),
            getattr(friend, "visual_mode", "live2d"),
        ),
        expression_note=(
            expression_service.note_for_prompt(session, friend.id)
            if getattr(friend, "humanizer_enabled", 0)
            else None
        ),
        reply_shape_note=reply_shape_note,
    )
    chunks: list[str] = []
    # v4.3（D249）：主动消息也**跟朋友自己的温度走**；她没设过就还是原来的 0.8
    temperature = clamp_temperature(getattr(friend, "temperature", None))
    # D116：思考常开的模型会吃掉预算，别让主动消息变空
    # v3.5：主动消息也是"她说话"，同样要留够被思考链吃掉的预算（600 → 1500）
    async for event in provider.chat_stream(
        messages,
        temperature=0.8 if temperature is None else temperature,
        max_tokens=max_tokens,
    ):
        if event.get("type") == "content":
            chunks.append(event.get("delta") or "")
        elif event.get("type") == "done":
            break
    text = "".join(chunks).strip()
    # 主动消息也可能学历史开头的 [HH:MM]；这条路没有 agent loop 的剥离器，这里补一次。
    stamp = LeadingStampStripper()
    text = (stamp.feed(text) + stamp.flush()).strip()
    # v3.51（D241）：主动消息这条路**不跑工具循环**（只取文本），所以她在正文里写的
    # `<tool>...</tool>` 块既不会执行、也不会被摘掉 —— 用户会看到原始 JSON（实测过一条 get_time）。
    # 这里直接把块摘掉（主动消息本来也不需要工具：时间/屏幕/纸条都由提示词给）。
    cleaned = re.sub(r"<tool>.*?</tool>", "", text, flags=re.S).strip()
    if cleaned != text:
        logger.warning("主动消息里出现了工具块（这条路不跑工具），已摘掉不显示")
    return cleaned


async def look_at_screen(session: Session, friend: Friend) -> str | None:
    """D103：主动消息的第一步 —— 先看一眼屏幕，拿一句观察。

    失败（没配视觉 / 截不到屏 / 模型抽风）**不算错**：返回 None，这次就不带屏幕话题
    （D83 的口径：可选能力缺失不影响主流程）。
    """
    try:
        png = screen_service.capture()
        note = await screen_service.observe(png, session=session)
    except Exception as exc:  # noqa: BLE001 —— 看屏失败绝不能拖累主动消息
        logger.warning("主动消息看屏失败（这次不带屏幕话题）：{}", exc)
        return None
    screen_service.set_latest(friend.id, note)
    logger.info("主动消息看屏：{} → {}", friend.name, note[:40])
    return note


async def deliver(
    session: Session,
    friend: Friend,
    content: str,
    *,
    segments: list[str] | None = None,
) -> Message:
    """落库 + 未读 +1 + WebSocket 推送 + 桌面弹窗（D70）。"""
    if getattr(friend, "humanizer_enabled", 0):
        if segments and len(segments) > 1:
            segments = [typo_service.maybe_typo(item, rate=0.02) for item in segments]
        else:
            content = typo_service.maybe_typo(content, rate=0.02)
    if segments and len(segments) > 1:
        content = "\n".join(segments)
    message = message_service.create(
        session,
        friend.id,
        role="assistant",
        content=content,
        mode_at_send=friend.current_mode,
        is_proactive=True,
        stream_status=message_service.COMPLETED,
    )
    if segments and len(segments) > 1:
        message.segments = json.dumps(segments, ensure_ascii=False)
        session.flush()
    friend.unread_count = (friend.unread_count or 0) + 1
    session.flush()
    await manager.broadcast(
        {
            "type": "new_message",
            "friend_id": friend.id,
            "friend_name": friend.name,
            "message": message_service.to_dict(message),
        }
    )
    # 桌面外壳（pywebview）会在这一步弹一张自己画的卡片（D70）。
    # 没有外壳时（例如只跑 `python -m backend.main`）这里什么都不发生。
    notify.emit(
        {
            "kind": "proactive",
            "friend_id": friend.id,
            "friend_name": friend.name,
            "text": content,
        }
    )
    # 主动消息也是"她的回复"：开了语音开关就一样配音（D85）。失败不影响上面这些。
    try:
        voice_reply_service.schedule(message.id)
    except Exception as exc:  # noqa: BLE001 —— 配音绝不许拖累主动消息
        logger.warning("主动消息配音排队失败：{}", exc)
    return message


async def run_heartbeat_tick(
    session: Session,
    provider: ILLMProvider,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
    screen_watch: bool | None = None,
) -> dict[str, Any]:
    """一次普通心跳：有真实开口理由就走真实候选，否则走旧的掷骰子 checkin。"""
    return await run_initiative_tick(
        session,
        provider,
        rng=rng,
        now=now,
        screen_watch=screen_watch,
        allow_checkin=True,
    )


def _proactive_segments(text: str) -> list[str]:
    """主动消息既可显式 `<<<MSG>>>`，也可由后端按语义切成最多 3 泡。"""
    raw = (text or "").strip()
    if not raw:
        return []
    parts = [part.strip() for part in re.split(r"<<<MSG>>>", raw) if part.strip()]
    if len(parts) > 1:
        return bubble_splitter.split_segments(parts, max_chars=34, max_bubbles=3)
    return bubble_splitter.split_bubbles(raw, max_chars=34, max_bubbles=3)


def _pick_candidate(candidates: list[Any], rng: random.Random):
    top = max(float(item.score) for item in candidates)
    return rng.choice([item for item in candidates if float(item.score) == top])


def _consume_candidate(
    session: Session,
    candidate: Any,
    *,
    sent: bool,
    moment: datetime,
    spoken_text: str = "",
) -> None:
    """按来源结算这次开口：生活观察清空；纸条说了才转 awaiting。"""
    friend = candidate.friend
    if candidate.kind == "life":
        life = session.get(FriendLife, friend.id)
        if life is not None:
            life.pending_share = ""
            life.last_share_at = moment.isoformat()
            life.updated_at = moment.isoformat()
            session.flush()
            if sent and spoken_text:
                life_service.record_project_thought(session, friend.id, spoken_text)
        return
    if candidate.kind == "thought" and sent and candidate.source_id:
        thought = session.get(Thought, candidate.source_id)
        if thought is None:
            return
        thought_service.mark_said(session, thought, now=moment)
        if thought.schedule_id:
            schedule = session.get(Schedule, thought.schedule_id)
            if schedule is not None:
                schedule_service.mark_said(session, schedule, now=moment)
        logger.info("主动消息用了纸条：{} → 转待回（{}）", friend.name, thought.content[:30])


async def run_initiative_tick(
    session: Session,
    provider: ILLMProvider,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
    screen_watch: bool | None = None,
    allow_checkin: bool = False,
) -> dict[str, Any]:
    """统一主动消息入口（D351）：先找真实理由，再决定说 / 不说。"""
    thought_service.purge_expired(session, now=now)
    if has_active_conversation(session, now=now):
        return {"sent": 0, "reason": "active_conversation"}
    rng = rng or random
    moment = now or datetime.now(timezone.utc)
    candidates = initiative_service.collect_candidates(session, now=moment)
    if candidates:
        candidate = _pick_candidate(candidates, rng)
    elif allow_checkin:
        friend = pick_present_friend(
            session,
            rng=rng,
            now=moment,
            cooldown_seconds=initiative_service.INITIATIVE_COOLDOWN_SECONDS,
        )
        if friend is None:
            return {"sent": 0, "reason": "no_dice_hit"}
        candidate = initiative_service.checkin_candidate(friend)
    else:
        return {"sent": 0, "reason": "no_candidate"}

    friend = candidate.friend
    if screen_watch is None:
        screen_watch = bool(settings_service.get(session).screen_watch_enabled)
    screen_note = (
        await look_at_screen(session, friend)
        if candidate.kind == "checkin" and screen_watch
        else None
    )

    parts = [candidate.hint]
    try:
        state = character_state_service.get_or_create(session, friend.id)
        state_note = character_state_service.to_prompt(state)
        if state_note:
            parts.append(state_note)
    except Exception as exc:  # noqa: BLE001
        logger.warning("主动消息取状态失败（不影响这轮）：{}", exc)
    try:
        episode_note = episode_service.prompt_note(session, friend.id)
        if episode_note:
            parts.append(episode_note)
    except Exception as exc:  # noqa: BLE001
        logger.warning("主动消息取情节失败（不影响这轮）：{}", exc)
    parts += [state_block(unanswered_state(session, friend, now=moment)), PROACTIVE_MANNERS]
    if screen_note:
        parts.append(SCREEN_HINT.format(note=screen_note))

    content = await _generate(
        session,
        friend,
        provider_for(provider, friend),
        "\n\n".join(parts),
        screen_note=screen_note,
        reply_shape_note=PROACTIVE_FORM_NOTE,
        max_tokens=900,
    )
    if not content:
        logger.warning("主动消息生成为空，跳过（朋友={}, 来源={}）", friend.name, candidate.kind)
        return {"sent": 0, "reason": "empty"}
    if is_silent(content):
        _consume_candidate(session, candidate, sent=False, moment=moment)
        logger.info(
            "主动消息：{} 来源={} 这次选择不说（沉默）",
            friend.name,
            candidate.kind,
        )
        return {"sent": 0, "reason": "silent"}

    bubbles = _proactive_segments(content)
    _consume_candidate(
        session,
        candidate,
        sent=True,
        moment=moment,
        spoken_text=content,
    )
    await deliver(session, friend, content, segments=bubbles)
    logger.info(
        "主动消息：{} 来源={} → {} 个气泡",
        friend.name,
        candidate.kind,
        len(bubbles) or 1,
    )
    return {
        "sent": 1,
        "friend": friend.name,
        "kind": candidate.kind,
        "bubbles": len(bubbles) or 1,
    }


async def run_life_share_tick(
    session: Session,
    provider: ILLMProvider,
    *,
    rng: random.Random | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """兼容入口：30 秒 tick 处理所有**非 checkin** 的真实候选。"""
    return await run_initiative_tick(
        session,
        provider,
        rng=rng,
        now=now,
        screen_watch=False,
        allow_checkin=False,
    )


async def run_schedule_tick(
    session: Session, provider: ILLMProvider, *, now: datetime | None = None
) -> dict[str, Any]:
    """扫一遍日程母表（v4.0 / D243）：

    · 先归档过缓冲 / 硬上限的；
    · `pending` 且到点/到日 → 推进：
        - 有 `time` → **到点必响**（reminder 行为，**不管朋友在不在场**，D35），响完也进纸条；
        - 只到日   → 直接进纸条（由她找机会提；不响）。
    到点提醒**先让她看最近的对话**：她已经判断"他办完了 / 不用提了"（回 `[沉默]`）→ 这条收尾，
    不再重复响（D245；否则会出现"事情早做完了她还照本宣科提醒"的失忆感）。

    `provider` 同 `run_heartbeat_tick`：可以是函数（按朋友给，D151）。
    """
    moment = now or datetime.now(timezone.utc)
    schedule_service.archive_expired(session, now=moment)
    due = schedule_service.scan_due(session, now=moment)
    sent = 0
    noted = 0
    skipped = 0
    for schedule in due:
        friend = session.get(Friend, schedule.friend_id)
        if friend is None or friend.deleted_at is not None:
            schedule.state = "archived"  # 朋友没了，这条日程作废
            schedule.updated_at = datetime.now(timezone.utc).isoformat()
            continue
        if schedule.time:
            try:
                content = await _generate(
                    session,
                    friend,
                    provider_for(provider, friend),
                    reminder_hint(schedule.title),
                )
            except Exception as exc:  # noqa: BLE001 —— 生成失败也要响，别把提醒吞了
                logger.warning("日程提醒生成失败，改用原文发出：{}", exc)
                content = ""
            if is_silent(content):
                # 她看了上下文，判断"已经办完 / 不用提了" → 收尾，不再重复响（D245）
                schedule_service.settle(session, "system", schedule.id, reason="done")
                skipped += 1
                logger.info(
                    "日程到点：{} 看了上下文，判断不用提了 → 收尾（{}）",
                    friend.name,
                    schedule.title[:30],
                )
                continue
            if not content:
                content = f"（提醒）{schedule.title}"
            await deliver(session, friend, content)
            schedule_service.activate(session, schedule, now=moment, said=True)
            # reminder 说完也进纸条：她刚提过 → 落 awaiting，别紧接着又问一遍（D243 改版）
            thought_service.add_for_schedule(
                session, friend.id, schedule=schedule, said=True, now=moment
            )
            sent += 1
            logger.info("日程到点：{} → {}", friend.name, schedule.title[:30])
        else:
            schedule_service.activate(session, schedule, now=moment, said=False)
            thought_service.add_for_schedule(
                session, friend.id, schedule=schedule, said=False, now=moment
            )
            noted += 1
            logger.info("日程到日（进纸条）：{} → {}", friend.name, schedule.title[:30])
    return {"sent": sent, "noted": noted, "skipped": skipped, "due": len(due)}
