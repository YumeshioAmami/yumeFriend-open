"""Agent 主循环（LLM协议§5 / architecture.md §3）。

一轮用户输入会发生什么：

    组装上下文
      ↓
    ┌─ 循环（最多 MAX_TOOL_ROUNDS 轮）──────────────────┐
    │  流式调用 LLM                                      │
    │    ├─ content delta   → 转发 + 累积（工具块已剥离） │
    │    ├─ reasoning delta → 转发（不落库，D23）         │
    │    └─ tool_call       → 转发 + 收集                 │
    │  本轮结束：没有工具 → 跳出                          │
    │           有工具   → 执行 → 发 tool_result → 回灌 → 继续 │
    └───────────────────────────────────────────────────┘
      ↓
    超过轮数上限 → 追加"请直接用自然语言收尾"，且**不再给工具**

工具执行后**立刻提交**：不然客户端中途断开时，"它已经记住了"这件事会跟着回滚。
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Sequence

from loguru import logger
from sqlalchemy.orm import Session

from backend.agent import tools as tools_module
from backend.agent.context import (
    MAX_TOKENS_DEFAULT,
    MAX_TOKENS_TOOL,
    build_messages,
    temperature_for,
)
from backend.agent.parser import (
    LineStampStripper,
    MoodTagParser,
    ToolBlockParser,
    normalize_native_tool_call,
)
from backend.agent.repeats import strip_repeats
from backend.agent.segment_block import SegmentParser
from backend.agent.state_block import StateBlockParser
from backend.llm.base import ILLMProvider
from backend.models.friend import Friend
from backend.models.memory import Memory
from backend.services import usage_service

# 多轮工具的上限（LLM协议§5）
MAX_TOOL_ROUNDS = 5

FINAL_ROUND_HINT = "[系统] 请直接用自然语言回答用户，不要再调用任何工具。"

# v4.1（D246）：空回复 / 被截断的兜底重试（最多这么多次）。
# 背景：deep_thinking 开着时，思考链**和正文共用 max_tokens** —— 模型偶尔整轮只思考不说话
# （正文空 → 前端显示「（空）」），或者思考吃满预算把正文截断（`停止=length`）。
MAX_RECOVERIES = 2
EMPTY_RECOVERY_HINT = (
    "[系统] 你刚才这一轮**没有说出任何话**（可能全花在思考上了）。"
    "请直接用一句话回答用户，不要再输出思考过程、也不要调用工具。"
)
LENGTH_CONTINUE_HINT = (
    "[系统] 你上一句被长度限制截断了。**接着上面那句继续说完**，"
    "不要重复已经说过的内容，也不要重新开头。"
)


def _to_event(parsed: dict[str, Any]) -> dict[str, Any]:
    """解析器只懂 text / tool_call；上层统一用 content / tool_call。"""
    if parsed["type"] == "text":
        return {"type": "content", "delta": parsed["text"]}
    return parsed


async def _vision_notes(
    session: Session,
    history: Sequence[Any],
    user_image: str | None,
    inline_images: bool,
) -> tuple[dict[int, str], str | None]:
    """D135：**独立配了视觉模型**时，把图转述成文字（聊天模型自己看不了图）。

    - 历史里的图**只读缓存、绝不联网** —— 不然每轮对话都要多等一次视觉模型（划不来）。
    - 本轮新图才去问一次（同一张图问过就命中缓存）。
    - 没配 / 视觉就是跟随聊天（图直接 inline）/ 出错 → 返回空，
      上层退回 `[TA 给你发了一张图片，但你看不到图片内容]` 那句人话（D83），**绝不抛**。
    """
    if inline_images:
        return {}, None

    from backend.services import image_service  # 懒导入：避免 context ↔ services 循环
    from backend.services import vision_service

    notes: dict[int, str] = {}
    for item in history:
        if isinstance(item, dict):
            image_path = item.get("image_path")
            image_no = item.get("image_no")
        else:
            image_path = getattr(item, "image_path", None)
            image_no = getattr(item, "image_no", None)
        if not image_path or image_no is None:
            continue
        try:
            number = int(image_no)
        except (TypeError, ValueError):
            continue
        if number in notes:
            continue
        abs_file = image_service.abs_path(image_path)
        note = vision_service.cached(str(abs_file)) if abs_file is not None else None
        if note:
            notes[number] = note

    user_note: str | None = None
    if user_image:
        abs_file = image_service.abs_path(user_image)
        if abs_file is not None:
            user_note = await vision_service.describe(session, str(abs_file))
    return notes, user_note


async def stream_reply(
    *,
    friend: Friend,
    user_input: str,
    provider: ILLMProvider,
    session: Session,
    history: Sequence[Any] = (),
    memories: Sequence[Memory] = (),
    now: Any = None,
    deep_thinking: bool = False,
    previous_mode: str | None = None,
    user_image: str | None = None,
    inline_images: bool = True,
    knowledge_hits: Sequence[dict] = (),
    document_index: Sequence[dict] = (),
    screen_note: str | None = None,
    portrait_emotions: Sequence[str] = (),
    game_turn: bool = False,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    max_game_says: int = 1,
    usage_purpose: str = "chat",
    character_state: Any | None = None,
    episode_note: str | None = None,
    life_note: str | None = None,
    space_note: str | None = None,
    pacing_note: str | None = None,
    max_tokens_cap: int | None = None,
    expression_note: str | None = None,
    reply_shape_note: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """跑一轮对话（含工具循环），产出归一化事件流。"""
    # v7.20（D373）：本轮的功能开关快照。中途改开关不影响这一轮。
    from backend.features import flags as feature_flags  # noqa: PLC0415

    enabled_features = feature_flags.snapshot()
    if enabled_features is None and session is not None:
        enabled_features = feature_flags.refresh(session)

    # D135：聊天模型看不了图、但用户单配了视觉模型 → 先把图"转述"成文字再进上下文
    if game_turn:
        image_notes, user_image_note = [], None
    else:
        image_notes, user_image_note = await _vision_notes(
            session, history, user_image, inline_images
        )
    # D192 / v4.0（D243）：他开口了 → 把"她惦记的事"（含日程派生的纸条，最多 3 条）给她看一眼，
    # 由她自己决定提不提 —— 不再"给一次就销账"（无限制 + 提示词收敛，D243 改版）
    from backend.services import thought_service  # noqa: PLC0415 —— 懒导入（避免循环）

    thought_note = None
    if (
        session is not None
        and not game_turn
        and feature_flags.enabled("thought")
    ):
        try:
            thought_note = thought_service.notes_for_turn(session, friend.id)
        except Exception as exc:  # noqa: BLE001 —— 纸条出问题绝不许拖累这一轮对话
            logger.warning("取纸条失败（不影响这轮）：{}", exc)
    # v4.1（D246）：把她当前名下的日程摆进 system prompt —— 她才"记得"自己记了这些事，
    # 才可能在你说"办完了/改时间"时真的去调 update_schedule（光靠 list_schedules 她想不起来用）
    schedule_note = None
    if (
        session is not None
        and not game_turn
        and feature_flags.enabled("schedule")
    ):
        try:
            from backend.services import schedule_service  # noqa: PLC0415

            schedule_note = schedule_service.upcoming_for_prompt(session, friend.id)
        except Exception as exc:  # noqa: BLE001 —— 日程清单出问题绝不许拖累这轮
            logger.warning("取日程清单失败（不影响这轮）：{}", exc)
    # v5.0（P3 / D250）：这个朋友正在玩游戏 → 把「当前状态 + 最近事件 + 最近一起玩过」摆给她
    from backend.game.manager import get_manager  # noqa: PLC0415 —— 懒导入，避免循环

    game_manager = get_manager()
    game_active = (
        game_manager.active_for(friend.id) if feature_flags.enabled("game") else False
    )
    cheat_active = game_manager.cheats_active_for(friend.id) if game_active else False
    game_note = ""
    recent_games_note = ""
    if game_active:
        try:
            game_note = await game_manager.note_for_prompt(friend.id)
        except Exception as exc:  # noqa: BLE001 —— 游戏状态出问题绝不许拖累聊天
            logger.warning("取游戏状态失败（不影响这轮）：{}", exc)
    try:
        recent_games_note = game_manager.recent_games_note(friend.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("取最近游戏摘要失败（不影响这轮）：{}", exc)
    # v3.46（D231）：向量命中（语义召回）。模型没加载好就先跳过 —— 回填任务在**后台线程**里
    # 负责首次加载（可能要几十秒，含首次下载），这里绝不在对话线程里触发加载。
    vector_hits: list[Memory] = []
    if session is not None and user_input and not game_turn:
        try:
            from backend.services import embedding_service  # noqa: PLC0415

            if embedding_service.ready():
                vector_hits = embedding_service.vector_hits(session, friend.id, user_input)
        except Exception as exc:  # noqa: BLE001 —— 向量这条挂了也必须能聊天
            logger.warning("向量召回失败（本轮只用关键词）：{}", exc)
    style_profile_note = ""
    style_feedback_reminder = None
    if (
        session is not None
        and not game_turn
        and feature_flags.enabled("speech_style")
    ):
        try:
            from backend.services import style_profile_service  # noqa: PLC0415

            style_profile_note = style_profile_service.prompt_note(session, friend.id)
            if style_profile_service.looks_like_explicit_feedback(user_input):
                from backend.agent import context as context_module  # noqa: PLC0415

                style_feedback_reminder = context_module.STYLE_FEEDBACK_REMINDER
        except Exception as exc:  # noqa: BLE001 —— 档案坏了不能拖垮聊天
            logger.warning("读取说话方式档案失败（本轮跳过）：{}", exc)
    messages = build_messages(
        friend,
        history=history,
        user_input=user_input,
        # v3.44（D229）：把本轮用户输入当"关键词"，命中她记忆里的短词/专有名词/数字就自动注入
        focus_text=user_input,
        # v3.46（D231）：向量命中的记忆（语义召回），跟关键词命中一起去重注入
        extra_memories=vector_hits,
        memories=memories,
        now=now,
        # 工具清单按 mode 过滤（T2.0-8）：本轮的 mode 由 friend.current_mode 决定；
        # 中途 switch_mode 不改这一轮的清单（下一轮自然生效），免得同一轮里工具表跳来跳去
        tool_lines=tools_module.prompt_lines(
            friend.current_mode or "normal",
            game_active=game_active,
            cheat_active=cheat_active,
            enabled_features=enabled_features,
        ),
        enabled_features=enabled_features,
        previous_mode=previous_mode,
        user_image=user_image,
        inline_images=inline_images,
        image_notes=image_notes,
        user_image_note=user_image_note,
        knowledge_hits=knowledge_hits,
        document_index=document_index,
        screen_note=screen_note,
        thought_note=thought_note,
        schedule_note=schedule_note,
        game_note=game_note,
        recent_games_note=recent_games_note,
        portrait_emotions=portrait_emotions,
        character_state=character_state,
        episode_note=episode_note,
        life_note=life_note,
        space_note=space_note,
        pacing_note=pacing_note,
        expression_note=expression_note,
        style_profile_note=style_profile_note,
        style_feedback_reminder=style_feedback_reminder,
        reply_shape_note=reply_shape_note,
    )

    # v4.3（D249）：温度**按朋友**取 —— 她设了自己的就用她的，没设就是全局默认 0.7
    base_kwargs: dict[str, Any] = {
        "temperature": temperature_for(friend),
        "usage_purpose": usage_purpose,
        # OpenCode 等网关需要稳定会话 ID；普通 provider 会忽略/弹出它。
        "session_id": friend.id,
    }
    capabilities = provider.config.capabilities
    if deep_thinking:
        # D33 实测：开思考链不用换模型名，加 reasoning_effort 即可
        if capabilities.reasoning:
            base_kwargs["reasoning_effort"] = "high"
        else:
            logger.info("当前模型不支持思考链，忽略 deep_thinking 开关（D23）")
    # v3.8（D169）：这个模型声明了原生 function calling → 把工具清单**原生**给它
    # （比让它手写 <tool>{...}</tool> 可靠得多；提示词协议照旧留着兜底，D14）
    if capabilities.native_tool_calls:
        native_specs = tools_module.native_tools(
            friend.current_mode or "normal",
            game_active=game_active,
            cheat_active=cheat_active,
            enabled_features=enabled_features,
        )
        if native_specs:
            base_kwargs["tools"] = native_specs

    started = time.monotonic()
    first_token_at: float | None = None
    usage: dict[str, Any] = {}
    usage_total: dict[str, int] = {}
    provider_calls = 0
    stop_reason: str | None = None
    all_tool_calls: list[dict[str, Any]] = []
    executed: set[str] = set()  # 整轮对话里已经执行过的调用（跨轮去重）
    # D281：同一个工具连续用不同参数撞同一个错误 → 不再让它无限重试。
    last_tool_errors: dict[str, str] = {}
    said_count = 0
    # 2026-09-27（用户拍板）：`look_screen` **一轮对话最多看一次**（能力要有、但要控量；
    # 不做时间冷却 —— 他判断需要看就随时能看，只是同一轮里别反复看）。
    screen_looks = 0
    rounds = 0
    final_round = False
    mood: str | None = None
    # D104：已经发出去的正文（跨轮累积）——工具轮的重复护栏拿它比对
    emitted: list[str] = []
    recoveries = 0  # v4.1（D246）：空回复 / 截断的兜底重试次数

    while True:
        parser = ToolBlockParser()
        # 每轮都剥掉可能重复出现的状态块；只有第一轮的状态块会被采用。
        state_parser = StateBlockParser() if character_state is not None else None
        segment_parser = SegmentParser() if character_state is not None else None
        # D440：白名单跟着朋友当前模型走，用户自定义标签才能被摘出来并生效。
        mood_parser = MoodTagParser(portrait_emotions)
        # v3.31（D216）：模型可能学历史里的样子、在回复开头也写个时间戳 —— 顺手剥掉
        stamp_stripper = LineStampStripper()
        round_text: list[str] = []
        round_calls: list[dict[str, Any]] = []
        # D104：工具轮（rounds > 0）先攒着，出去重护栏再发 —— 被拦下的内容用户根本看不到。
        # 第一轮照旧边流边发（打字机的体感就在这）。
        buffered = rounds > 0
        kwargs = dict(base_kwargs)
        kwargs["max_tokens"] = MAX_TOKENS_TOOL if not final_round else MAX_TOKENS_DEFAULT
        if max_tokens_cap is not None:
            kwargs["max_tokens"] = min(int(kwargs["max_tokens"]), max_tokens_cap)
        provider_calls += 1

        def feed_visible(text: str) -> list[dict[str, Any]]:
            """把可见正文喂给既有解析链，返回该转发的事件。"""
            out: list[dict[str, Any]] = []
            for parsed in parser.feed(
                stamp_stripper.feed(mood_parser.feed(text))
            ):
                item = _to_event(parsed)
                if item["type"] == "content":
                    round_text.append(item["delta"])
                    if not buffered:
                        out.append(item)
                else:
                    round_calls.append(item)
                    out.append(item)
            for found in mood_parser.take_new_moods():
                out.append({"type": "mood", "mood": found["mood"], "at": found["at"]})
            return out

        # 建完上下文先结束读事务：首字可能要十几秒，别让 SQLite 的写锁被它一直占着（D197）
        if session is not None and session.in_transaction():
            session.commit()

        async for event in provider.chat_stream(messages, **kwargs):
            kind = event.get("type")
            if kind == "content":
                raw_delta = event.get("delta") or ""
                state_events = (
                    state_parser.feed(raw_delta)
                    if state_parser is not None
                    else [{"type": "content", "delta": raw_delta}]
                )
                for state_event in state_events:
                    if state_event["type"] == "state_delta":
                        # 隐藏状态块：不进入正文；由 SSE 层落库。
                        if rounds == 0:
                            yield state_event
                        else:
                            logger.debug("工具轮重复输出 state_delta，已忽略")
                        continue
                    segment_events = (
                        segment_parser.feed(state_event["delta"])
                        if segment_parser is not None
                        else [{"type": "content", "delta": state_event["delta"]}]
                    )
                    for segment_event in segment_events:
                        if segment_event["type"] == "segment_break":
                            round_text.append("\n")
                            yield {"type": "segment_break"}
                            continue
                        if first_token_at is None and segment_event["delta"]:
                            first_token_at = time.monotonic()
                        for item in feed_visible(segment_event["delta"]):
                            yield item
            elif kind == "reasoning":
                delta = event.get("delta") or ""
                if delta:
                    # 思考链只转发，不累积、不落库（D23）
                    yield {"type": "reasoning", "delta": delta}
            elif kind == "tool_call":
                # 原生协议（M3 不用，缺口 G1）——仍然归一化后转发
                item = normalize_native_tool_call(event)
                round_calls.append(item)
                yield item
            elif kind == "done":
                usage = event.get("usage") or {}
                usage_total = usage_service.merge_usage(usage_total, usage)
                stop_reason = event.get("stop_reason")

        state_tail = state_parser.flush() if state_parser is not None else []
        tail_text = ""
        for state_event in state_tail:
            if state_event["type"] == "state_delta":
                if rounds == 0:
                    yield state_event
                else:
                    logger.debug("工具轮重复输出 state_delta，已忽略")
                continue
            tail_text += state_event["delta"]
        segment_tail = (
            [*segment_parser.feed(tail_text), *segment_parser.flush()]
            if segment_parser is not None
            else [{"type": "content", "delta": tail_text}]
        )
        for segment_event in segment_tail:
            if segment_event["type"] == "segment_break":
                round_text.append("\n")
                yield {"type": "segment_break"}
                continue
            if segment_event["delta"]:
                for item in feed_visible(segment_event["delta"]):
                    yield item
        tail = stamp_stripper.feed(mood_parser.flush()) + stamp_stripper.flush()
        for parsed in [*parser.feed(tail), *parser.flush()]:
            item = _to_event(parsed)
            if item["type"] == "content":
                round_text.append(item["delta"])
                if not buffered:
                    yield item
            else:
                round_calls.append(item)
                yield item
        for found in mood_parser.take_new_moods():
            yield {"type": "mood", "mood": found["mood"], "at": found["at"]}

        if mood_parser.mood:
            mood = mood_parser.mood

        # ---- D104 重复护栏 ----
        round_all = "".join(round_text)
        if buffered and round_all.strip():
            cleaned = strip_repeats(round_all, "\n".join(emitted)).strip()
            if not cleaned:
                logger.warning("重复护栏：工具轮整轮都是说过的内容，已拦下 {} 字", len(round_all))
            elif cleaned != round_all.strip():
                logger.warning(
                    "重复护栏：工具轮拦下 {} 字重复内容",
                    len(round_all) - len(cleaned),
                )
            if cleaned:
                yield {"type": "content", "delta": cleaned}
            round_text = [cleaned]
        if round_all.strip():
            emitted.append(round_all.strip())

        if final_round:
            # 收尾轮已经不给工具了；就算它还是吐了工具块，也**只当正文**、不再执行、不再循环。
            # （少了这个 break 就是死循环 —— 踩过一次）
            if round_calls:
                logger.warning("收尾轮仍输出了 {} 个工具调用，已忽略", len(round_calls))
            break

        if not round_calls:
            # v4.1（D246）：思考链吃光预算 / 模型只思考不说话 / 被 max_tokens 截断 → 兜底重试
            round_visible = "".join(round_text).strip()
            if recoveries < MAX_RECOVERIES:
                if not round_visible:
                    recoveries += 1
                    base_kwargs.pop("reasoning_effort", None)  # 关思考，把预算全留给正文
                    messages.append({"role": "user", "content": EMPTY_RECOVERY_HINT})
                    logger.warning("这一轮一个字都没吐出来，关掉思考链重试（第 {} 次）", recoveries)
                    continue
                if stop_reason == "length":
                    recoveries += 1
                    base_kwargs.pop("reasoning_effort", None)
                    messages.append({"role": "assistant", "content": round_visible})
                    messages.append({"role": "user", "content": LENGTH_CONTINUE_HINT})
                    logger.warning("这一轮被长度截断，让它接着说完（第 {} 次）", recoveries)
                    continue
            break

        # ---- 执行工具 ----
        rounds += 1
        # 模型偶尔会把同一个工具块吐两遍（实测：add_reminder 连着两次 → 建了两条重复提醒）。
        # 同一轮里**完全一样**的调用只执行一次 —— 两个操作结果一样，重放没有意义。
        unique_calls: list[dict[str, Any]] = []
        for call in round_calls:
            fingerprint = json.dumps(
                [call.get("name"), call.get("arguments")], sort_keys=True, ensure_ascii=False
            )
            if fingerprint in executed:
                logger.warning("重复的工具调用，已跳过：{}", call.get("name"))
                continue
            executed.add(fingerprint)
            unique_calls.append(call)

        if not unique_calls:
            # 本轮全是重复调用 → 直接逼它用自然语言收尾，别再空转
            messages.append({"role": "user", "content": FINAL_ROUND_HINT})
            final_round = True
            continue

        logger.info("第 {} 轮工具调用：{}", rounds, [c.get("name") for c in unique_calls])
        # v2.1（D99）：走异步版 —— 写操作会先推审批事件、等用户点头（等在线程里，不堵循环）
        results = []
        repeat_failure = False
        for call in unique_calls:
            if call.get("name") == "game_say" and said_count >= max(
                1, int(max_game_says)
            ):
                logger.warning(
                    "同一轮 game_say 超过 {} 次，已跳过（避免刷屏）",
                    max(1, int(max_game_says)),
                )
                continue
            result = await tools_module.execute_async(
                session,
                friend.id,
                call,
                allow_image_lookup=inline_images,
                # look_screen：这一轮已经看过就不再给看（第二次会回一句人话，不截图）
                allow_screen_lookup=screen_looks == 0,
                friend_name=friend.name,
            )
            if call.get("name") == "look_screen" and result.get("ok"):
                screen_looks += 1
            if call.get("name") == "game_say" and result.get("ok"):
                said_count += 1
            tool_name = str(call.get("name") or "")
            if result.get("ok"):
                last_tool_errors.pop(tool_name, None)
            else:
                error_text = str(result.get("error") or "")
                if error_text and last_tool_errors.get(tool_name) == error_text:
                    repeat_failure = True
                else:
                    last_tool_errors[tool_name] = error_text
            results.append(result)
        session.commit()  # 立刻落库：别让"它已经记住了"因为断开而回滚
        # 图片类结果：把 data URL 摘出来（1MB 级的字符串，**不能进 DB / 日志 / 事件**），
        # 等会儿单独作为多模态图片附在回灌消息里（D82 第三层）
        attached_images: list[str] = []
        for result in results:
            payload = result.get("result")
            if isinstance(payload, dict):
                url = payload.pop("_image_data_url", None)
                if isinstance(url, str) and url:
                    attached_images.append(url)
        all_tool_calls.extend(results)
        for result in results:
            payload: dict[str, Any] = {
                "id": result.get("id"),
                "name": result.get("name"),
                "args": result.get("args") or {},
                "ok": result.get("ok"),
            }
            if result.get("ok"):
                payload["result"] = result.get("result")
            else:
                payload["error"] = result.get("error")
            yield {"type": "tool_result", **payload}

        if repeat_failure:
            logger.warning(
                "同一工具连续相同失败，停止重试：{}",
                last_tool_errors.get(str(unique_calls[-1].get("name") or ""), ""),
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "同一个工具调用已经用同样的参数失败两次。不要再原样重试；"
                        "换一种写法，或者直接用自然语言说明这一步做不了。"
                    ),
                }
            )
            final_round = True
            continue

        text = "".join(round_text).strip()
        if text:
            messages.append({"role": "assistant", "content": text})

        if rounds >= max_tool_rounds:
            # 兜底轮：不再给工具，让它自然收尾（LLM协议§5）
            logger.warning("工具调用达到上限 {} 轮，进入收尾轮", max_tool_rounds)
            final_round = True
            messages.append({"role": "user", "content": FINAL_ROUND_HINT})
        else:
            follow_up = (
                # v3.6（D157）：加一句"这不是给你回复的内容" —— 实测有的模型会把这条说明
                # 当成正文来回味（把"系统提示我上面那条已经说过了"写进回复里，很难看）。
                "[系统]（下面这些只是交接说明，不是让你回复的内容，别复述它）"
                "你上面那条已经说过了，接着往下说，别重复。\n"
                + tools_module.format_results(results)
            )
            if attached_images:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": follow_up},
                            *[
                                {"type": "image_url", "image_url": {"url": url}}
                                for url in attached_images
                            ],
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": follow_up})

    # D363：模型有时收到了“这是说话方式反馈”的提醒，却仍只口头答应、不调工具。
    # 这时用保守规则补一条，避免“说过了但档案永远为空”。
    if style_feedback_reminder and session is not None:
        used_reflect = any(
            str(call.get("name") or "") == "reflect_speech" and call.get("ok")
            for call in all_tool_calls
        )
        if not used_reflect:
            try:
                from backend.db import session_scope  # noqa: PLC0415
                from backend.services import style_profile_service  # noqa: PLC0415

                changes = style_profile_service.fallback_changes(user_input)
                if changes:
                    if session.in_transaction():
                        session.commit()
                    with session_scope() as fallback_session:
                        result = style_profile_service.apply_changes(
                            fallback_session,
                            friend.id,
                            changes,
                            summary="模型漏调 reflect_speech，按明确反馈自动补记",
                            user_text=user_input,
                            assistant_excerpt="\n".join(emitted)[:2000],
                            reason="fallback_explicit_feedback",
                        )
                    logger.info(
                        "说话方式反馈兜底写入：{} 条（{}）",
                        len(result["applied"]),
                        friend.name,
                    )
            except Exception as exc:  # noqa: BLE001 —— 兜底失败不能拖垮聊天
                logger.warning("说话方式反馈兜底失败：{}", exc)

    # 日志不记聊天明文（LLM协议§9 第 6 条）：只记耗时 / token / 工具
    # 日志分两段：首字（体感就在这）和总时长，并带上走的哪条路 —— 慢的时候一眼看出慢在哪
    usage_total["provider_calls"] = provider_calls
    logger.info(
        "LLM 完成 路径={} 首字={} 总={:.2f}s 模型请求={} 输入token={}（缓存命中 {}） "
        "输出token={}（推理 {}） 总token={} 工具调用={} 停止={}",
        getattr(provider, "last_path_label", "?"),
        f"{first_token_at - started:.2f}s" if first_token_at else "未出字",
        time.monotonic() - started,
        provider_calls,
        usage_total.get("input_tokens", 0),
        usage_total.get("cache_hit_tokens", 0),
        usage_total.get("output_tokens", 0),
        usage_total.get("reasoning_tokens", 0),
        usage_total.get("total_tokens", 0),
        len(all_tool_calls),
        stop_reason,
    )
    yield {
        "type": "done",
        "usage": usage_total,
        "stop_reason": stop_reason,
        "tool_calls": all_tool_calls,
        "mood": mood,
    }
