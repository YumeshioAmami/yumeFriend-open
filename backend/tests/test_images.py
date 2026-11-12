"""T2.0-6 看图：上传 → 转正 → 内联进上下文 → 老图降级成占位（D82）。"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.agent.context import build_messages, image_placeholder
from backend.agent import tools as tools_module
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.services import image_service, message_service
from backend.services import image_note_service
from backend.timefmt import local_stamp
from backend.models import Message


def _png(width: int = 64, height: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class FakeProvider:
    """假模型：把收到的 messages 记下来（给"图真的塞进去了吗"用断言）。"""

    def __init__(self):
        self.calls: list[list[dict]] = []
        # ⚠️ loop 会**就地扩展**传给 provider 的 messages 列表（后面几轮直接 append 进去），
        # 所以只存引用的话，"第 1 轮"看到的是最终形态。这里另存一份浅拷贝给断言用。
        self.snapshots: list[list[dict]] = []
        self.config = ProviderConfig(
            name="fake",
            base_url="http://fake.local/v1",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        self.snapshots.append([dict(m) for m in messages])
        yield {"type": "content", "delta": "看到了。"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "images.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "UPLOAD_PENDING_DIR", tmp_path / "uploads" / "pending")
    monkeypatch.setattr(config, "TTS_CACHE_DIR", tmp_path / "tts_cache")
    app = create_app()
    with TestClient(app) as client:
        # 配好「聊天」，「视觉」默认跟随聊天 → 看得见图（D79/D83）
        client.patch(
            "/api/settings",
            json={
                "purposes": {
                    "chat": {
                        # ⚠️ 别用会走 DNS 的假域名：定时任务（补漏）可能真去连它
                        # （实测 fake.local 被解析到某个外部服务器，回了个 502）。
                        # 9 端口永远是"拒绝连接"，快、且不出网。
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "fake-model",
                        "api_key": "sk-fake",
                    },
                    "vision": {"follow_chat": True},
                }
            },
        )
        # P4 的图片索引是 best-effort 后台任务 —— 单测里别让它真去联网（专门有用例测它）
        monkeypatch.setattr(image_note_service, "spawn_describe", lambda *_a, **_k: None)
        yield app, client, tmp_path


def _upload(client, payload: bytes | None = None) -> dict:
    files = {"file": ("图片.png", payload or _png(), "image/png")}
    response = client.post("/api/uploads/image", files=files)
    assert response.status_code == 200, response.text
    return response.json()


def _make_friend(client) -> str:
    return client.post("/api/friends", json={"name": "阿月", "personality": "话不多"}).json()["id"]


# ---------- 上传 / 取图 ----------


def test_upload_lands_in_pending_and_is_served(app_client):
    _app, client, tmp_path = app_client
    info = _upload(client)
    assert len(info["id"]) == 32
    assert (info["width"], info["height"]) == (64, 64)
    assert info["url"] == f"/api/images/{info['id']}"
    assert list((tmp_path / "uploads" / "pending").glob("*.png")), "应该先落在 pending"

    got = client.get(info["url"])
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"


def test_upload_rejects_non_image_and_big_files(app_client):
    _app, client, _tmp = app_client
    bad = client.post("/api/uploads/image", files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "IMAGE_UNSUPPORTED"

    huge = client.post(
        "/api/uploads/image",
        files={"file": ("big.png", b"x" * (10 * 1024 * 1024 + 1), "image/png")},
    )
    assert huge.status_code == 400
    assert huge.json()["error"]["code"] == "IMAGE_TOO_LARGE"


def test_big_image_is_scaled_down(app_client):
    _app, client, _tmp = app_client
    info = _upload(client, _png(3000, 1200))
    assert max(info["width"], info["height"]) == 2048
    assert info["width"] / info["height"] == pytest.approx(3000 / 1200, rel=0.02)


def test_image_lookup_refuses_traversal(app_client):
    _app, _client, _tmp = app_client
    assert image_service.path_for("../../user_data/yumefriend.db") is None
    assert image_service.path_for("not-hex") is None
    assert image_service.abs_path("../../etc/passwd") is None


def test_cleanup_removes_stale_pending(app_client):
    _app, client, tmp_path = app_client
    info = _upload(client)
    path = next((tmp_path / "uploads" / "pending").glob(f"{info['id']}.*"))
    import os
    import time

    old = time.time() - 25 * 3600
    os.utime(path, (old, old))
    assert image_service.cleanup_pending() == 1
    assert not path.exists()


# ---------- 发消息带图 ----------


def test_send_message_with_image_promotes_and_numbers_it(app_client):
    app, client, tmp_path = app_client
    provider = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    friend_id = _make_friend(client)

    first = _upload(client)
    response = client.post(
        f"/api/friends/{friend_id}/messages",
        json={"content": "你看这个", "image_id": first["id"]},
    )
    assert response.status_code == 200, response.text
    assert not list((tmp_path / "uploads" / "pending").glob(f"{first['id']}.*")), "该转正了"
    assert list((tmp_path / "images").glob(f"{first['id']}.*"))

    with db.session_scope() as session:
        user_message = message_service.recent(session, friend_id, limit=10)[0]
        assert user_message.image_no == 1
        assert user_message.image_path.startswith("images/")
        assert image_service.abs_path(user_message.image_path) is not None

    # 第二张 → 编号 2
    second = _upload(client)
    client.post(
        f"/api/friends/{friend_id}/messages",
        json={"content": "", "image_id": second["id"]},  # 只发图不说话
    )
    with db.session_scope() as session:
        rows = [m for m in message_service.recent(session, friend_id, limit=10) if m.image_no]
        assert [m.image_no for m in rows] == [1, 2]  # recent() 是时间正序（老的在前）

    listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
    images = [m for m in listed if m["image_url"]]
    # 接口返回时间正序（老的在前）→ 最后一条是最新那张
    assert [m["image_no"] for m in images] == [1, 2]
    assert images[-1]["image_url"] == f"/api/images/{second['id']}"
    assert images[0]["image_url"] == f"/api/images/{first['id']}"


def test_send_message_with_unknown_image_is_400(app_client):
    _app, client, _tmp = app_client
    friend_id = _make_friend(client)
    response = client.post(
        f"/api/friends/{friend_id}/messages",
        json={"content": "看图", "image_id": "a" * 32},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMAGE_NOT_FOUND"


def test_empty_content_without_image_is_rejected(app_client):
    _app, client, _tmp = app_client
    friend_id = _make_friend(client)
    assert client.post(f"/api/friends/{friend_id}/messages", json={"content": ""}).status_code == 422


def test_the_model_actually_receives_the_image(app_client):
    """D82 第 1 层：**当轮必带** —— 模型收到的 user 消息里真的有一张 data URL 图。"""
    app, client, _tmp = app_client
    provider = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    friend_id = _make_friend(client)
    info = _upload(client)

    client.post(
        f"/api/friends/{friend_id}/messages",
        json={"content": "你看这个", "image_id": info["id"]},
    )

    messages = provider.snapshots[0]
    last = messages[-1]
    assert last["role"] == "user"
    parts = last["content"]
    assert isinstance(parts, list), "带图那轮应该是多模态 content parts"
    assert parts[0]["type"] == "text" and "给你发了一张图片" in parts[0]["text"]
    assert parts[0]["text"].startswith("[TA 给你发了一张图片]")
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_separate_vision_model_is_not_inlined_into_chat(app_client):
    """配了**独立**视觉模型（没有 follow_chat）→ 图不塞进 chat，改成"转述"或"看不到"（D83/D135）。

    这里专测**视觉模型连不上**的兜底：照样不塞图、照样如实说看不到
    （"转述真的进了上下文"由 `test_vision_forward.py` 盯）。
    """
    app, client, _tmp = app_client
    provider = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    client.patch(
        "/api/settings",
        json={
            "purposes": {
                "vision": {
                    "follow_chat": False,
                    # ⚠️ 别用假域名：转述会真去打这个地址（踩过 vision.local 的坑）。
                    # 9 端口永远是"拒绝连接" → 快、且不出网。
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "vl-model",
                    "api_key": "sk-v",
                }
            }
        },
    )
    friend_id = _make_friend(client)
    info = _upload(client)

    client.post(
        f"/api/friends/{friend_id}/messages",
        json={"content": "这张图里有什么？", "image_id": info["id"]},
    )

    last = provider.snapshots[0][-1]
    assert isinstance(last["content"], str), "独立视觉模型场景不该往 chat 塞图片 parts"
    assert "看不到图片内容" in last["content"]


# ---------- 上下文组装（直接测 build_messages）----------


def _friend():
    class _F:
        name = "阿月"
        personality = "话不多"
        custom_instructions = ""
        current_mode = "normal"
        current_role = None
        avatar_description = None

    return _F()


def _history_item(role: str, content: str, image_path=None, image_no=None, created_at=None) -> dict:
    return {
        "role": role,
        "content": content,
        "image_path": image_path,
        "image_no": image_no,
        "created_at": created_at or "2026-09-19T10:00:00+00:00",
    }


def test_recent_history_images_are_inlined_old_ones_become_placeholders(app_client):
    _app, client, tmp_path = app_client
    info = _upload(client)  # 落到 pending，data_url 也能读
    rel = f"uploads/pending/{info['id']}.png"

    # 造 8 条历史：第 1 条带图（最老），第 7 条带图（较新，**必须是我发的** ——
    # D149 之后她自己那条消息上的图永远 inline 不了，那条不归这个窗口管）
    history: list[dict] = []
    for index in range(8):
        has_image = index in (0, 6)
        history.append(
            _history_item(
                "user" if index % 2 == 0 else "assistant",
                f"第{index}条",
                rel if has_image else None,
                index + 1 if has_image else None,
            )
        )

    messages = build_messages(_friend(), history=history, user_input="在吗", history_limit=20)
    # 第 0 条（最老）→ 占位；第 6 条（在最近 6 条窗口里）→ inline
    oldest = messages[1]
    assert isinstance(oldest["content"], str)
    assert "[图片#1 · " in oldest["content"]  # 本地时间那部分见下面专门用例
    newest = messages[7]
    assert isinstance(newest["content"], list)
    assert newest["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_history_images_stay_placeholders_when_this_turn_has_a_new_image(app_client):
    """v3.10（T3.10-1）：**当轮带图** → 历史里的旧图不再 inline（模型会看串）。

    实测：新图是纯绿方块，旧图是白底红圆 —— 两张同时在上下文里时她会答"白底、中间一个绿圆"
    （把上一张的底子混进来）。旧图降级成占位后 4/4 都对。
    但当轮**没**图时不能改：得让"接着说刚才那张图"还能用。
    """
    _app, client, _tmp = app_client
    info = _upload(client)
    rel = f"uploads/pending/{info['id']}.png"
    history = [
        _history_item("user", "老图", rel, 1),
        _history_item("assistant", "看到了。", None, None),
    ]

    # 当轮没图 → 窗口内的老图照旧 inline
    plain = build_messages(_friend(), history=history, user_input="那它是什么颜色？", history_limit=20)
    assert isinstance(plain[1]["content"], list), "当轮没图时，老图要 inline（接着说那张图要用）"

    # 当轮带了新图 → 老图降级成占位
    fresh = build_messages(
        _friend(), history=history, user_input="又发了一张", user_image=rel, history_limit=20
    )
    assert isinstance(fresh[1]["content"], str)
    assert "[图片#1" in fresh[1]["content"], "当轮带图时，老图应该只剩占位"
    # 当轮这张还是真图（当轮必带，D82）
    assert isinstance(fresh[-1]["content"], list)


def test_text_only_model_gets_a_placeholder_not_an_image(app_client):
    _app, client, _tmp = app_client
    info = _upload(client)
    rel = f"uploads/pending/{info['id']}.png"
    history = [_history_item("user", "老图", rel, 3)]

    messages = build_messages(
        _friend(), history=history, user_input="看图", user_image=rel, inline_images=False
    )
    assert "（这个模型看不到图）" in messages[1]["content"]
    last = messages[-1]
    assert isinstance(last["content"], str)
    assert "看不到图片内容" in last["content"]


def test_image_placeholder_keeps_number_local_time_and_note():
    """P1/P4：占位符要三样 —— 编号（look_image 的钥匙）、**本地时间含时分**、一句内容索引。"""
    stamp = image_placeholder(12, "2026-09-19T10:00:00+00:00")
    assert stamp.startswith("[图片#12 · ")
    # 本地时间（含时分）—— 同一个瞬间换个时区写法，出来的必须是同一个串
    assert local_stamp("2026-09-19T10:00:00+00:00") == local_stamp("2026-09-19T18:00:00+08:00")
    assert stamp == f"[图片#12 · {local_stamp('2026-09-19T10:00:00+00:00')}]"
    # 有内容索引就带上（"那张猫的图"靠它找）
    with_note = image_placeholder(12, "2026-09-19T10:00:00+00:00", "一只橘猫趴在键盘上")
    assert "一只橘猫趴在键盘上" in with_note and with_note.endswith("]")
    # 解析不了的脏时间 → 退回旧的切法，不炸
    assert image_placeholder(3, "坏时间").startswith("[图片#3 · ")


def test_history_placeholder_carries_the_stored_image_note(app_client):
    """P4：那条消息上存了索引 → 占位符里就带上它（她照着就能对上"你说的那张图"）。"""
    _app, client, _tmp = app_client
    info = _upload(client)
    rel = f"uploads/pending/{info['id']}.png"
    # 让这张图**落到占位那一段**（窗口内最近 6 条才 inline，所以前面垫 6 条）
    history = [_history_item("user", "看这个", rel, 2)]
    history += [_history_item("assistant" if i % 2 else "user", f"后来的第{i}条") for i in range(6)]
    history[0]["image_note"] = "一只白底红圆的图"

    messages = build_messages(_friend(), history=history, user_input="在吗", history_limit=20)
    chip = messages[1]["content"]
    assert "[图片#2 · " in chip and "一只白底红圆的图" in chip
    assert image_placeholder(None, None) == "[图片#? · ?]"


# ---------- T2.0-6b：look_image（看旧图）----------


def _send_with_image(client, friend_id: str, image_id: str, content: str = "图") -> None:
    assert (
        client.post(
            f"/api/friends/{friend_id}/messages",
            json={"content": content, "image_id": image_id},
        ).status_code
        == 200
    )


def test_look_image_tool_returns_the_picture(app_client):
    _app, client, _tmp = app_client
    friend_id = _make_friend(client)
    info = _upload(client)
    _send_with_image(client, friend_id, info["id"])

    with db.session_scope() as session:
        record = tools_module.execute(session, friend_id, {"name": "look_image", "arguments": {"no": 1}})
    assert record["ok"] is True, record
    assert record["result"]["no"] == 1
    assert record["result"]["_image_data_url"].startswith("data:image/png;base64,")

    # 回灌文本里**不能**出现那串 data URL（1MB 级，只该作为图片附上）
    text = tools_module.format_results([record])
    assert "data:image" not in text
    assert "_image_data_url" not in text
    assert '"no": 1' in text


def test_look_image_reports_bad_numbers_and_missing_files(app_client, tmp_path):
    _app, client, _tmp = app_client
    friend_id = _make_friend(client)
    info = _upload(client)
    _send_with_image(client, friend_id, info["id"])

    with db.session_scope() as session:
        wrong = tools_module.execute(session, friend_id, {"name": "look_image", "arguments": {"no": 99}})
        # P2：**不给编号 = 最近一张**（她最常说"你刚发的那张"）
        latest = tools_module.execute(session, friend_id, {"name": "look_image", "arguments": {}})
        # 关键词找不到 → 人话（别编）
        miss = tools_module.execute(
            session, friend_id, {"name": "look_image", "arguments": {"query": "火星上的猫"}}
        )
    assert wrong["ok"] is False and "第 99 张" in wrong["error"]
    assert latest["ok"] is True and latest["result"]["no"] == 1
    assert miss["ok"] is False and "对得上" in miss["error"]

    # 文件被删了 → 人话，不是 500
    with db.session_scope() as session:
        message = message_service.recent(session, friend_id, limit=5)[0]
        image_service.abs_path(message.image_path).unlink()
    with db.session_scope() as session:
        gone = tools_module.execute(session, friend_id, {"name": "look_image", "arguments": {"no": 1}})
    assert gone["ok"] is False and "文件不在了" in gone["error"]


def test_look_image_finds_by_keyword_and_list_images_lists_the_album(app_client):
    """P3/P4：按内容找图 —— 图片索引存下来之后，`look_image(query=…)` 和 `list_images` 都能用。

    用户报的原话："模型想找图片但是找不到，因为图片只有编号没有日期，他不知道去哪里找。"
    """
    _app, client, _tmp = app_client
    friend_id = _make_friend(client)
    info = _upload(client)
    _send_with_image(client, friend_id, info["id"], content="看这个")

    # 先让"图片索引"落下来（平时是后台任务，这里直接调）
    with db.session_scope() as session:
        message = message_service.recent(session, friend_id, limit=5)[0]
        message_id = message.id
        message.image_note = "一只白底红圆的图"
        session.flush()

    with db.session_scope() as session:
        by_word = tools_module.execute(
            session, friend_id, {"name": "look_image", "arguments": {"query": "红圆"}}
        )
        album = tools_module.execute(session, friend_id, {"name": "list_images", "arguments": {}})
    assert by_word["ok"] is True and by_word["result"]["no"] == 1
    assert by_word["result"]["内容"] == "一只白底红圆的图"
    assert album["ok"] is True
    assert album["result"]["count"] == 1


async def test_image_note_service_stores_a_one_line_index(app_client, monkeypatch):
    """P4：发图之后（后台）给这张图存一句索引 —— 存进去了，占位符 / 相册 / 找图才能用。"""
    _app, client, _tmp = app_client
    friend_id = _make_friend(client)
    info = _upload(client)
    _send_with_image(client, friend_id, info["id"])

    class _FakeProvider:
        async def chat_stream(self, messages, **kwargs):  # noqa: ANN001, ANN003
            yield {"type": "content", "delta": "一只橘猫趴在键盘上\n"}
            yield {"type": "done"}

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        image_note_service, "create_provider_for_purpose", lambda *_a, **_k: _FakeProvider()
    )
    with db.session_scope() as session:
        message_id = message_service.recent(session, friend_id, limit=5)[0].id

    assert await image_note_service.describe_and_store(message_id) is True
    with db.session_scope() as session:
        note = session.get(Message, message_id).image_note
    assert note == "一只橘猫趴在键盘上"  # 换行被压平了


def test_look_image_is_refused_for_a_model_that_cannot_see(app_client):
    """看不了图的模型：别让它拿到一张自己看不了的图在那儿瞎猜。"""
    _app, client, _tmp = app_client
    friend_id = _make_friend(client)
    info = _upload(client)
    _send_with_image(client, friend_id, info["id"])

    with db.session_scope() as session:
        record = tools_module.execute(
            session,
            friend_id,
            {"name": "look_image", "arguments": {"no": 1}},
            allow_image_lookup=False,
        )
    assert record["ok"] is False
    assert "看不了图" in record["error"]


class _LookImageProvider(FakeProvider):
    """第一轮吐一个 look_image 工具块；第二轮把"她看到的东西"说出来。"""

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        self.snapshots.append([dict(m) for m in messages])
        if len(self.calls) == 1:
            yield {
                "type": "content",
                "delta": '<tool>{"name":"look_image","arguments":{"no":1}}</tool>',
            }
        else:
            yield {"type": "content", "delta": "看到了，是红圆和蓝三角。"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


def test_loop_feeds_the_old_image_back_to_the_model(app_client):
    """老图只剩占位时，她调 look_image → 那条图**真的被附进下一轮**（D82 第三层）。"""
    app, client, _tmp = app_client
    friend_id = _make_friend(client)
    info = _upload(client)
    _send_with_image(client, friend_id, info["id"], "第一张图")

    # 再聊 7 句，把那张图挤出"最近 6 条"的 inline 窗口
    quiet = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: quiet)
    for index in range(7):
        client.post(f"/api/friends/{friend_id}/messages", json={"content": f"闲聊 {index}"})

    looker = _LookImageProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: looker)
    response = client.post(f"/api/friends/{friend_id}/messages", json={"content": "第一张图里有什么？"})
    assert response.status_code == 200, response.text

    first_round = "".join(str(m.get("content")) for m in looker.snapshots[0])
    assert "[图片#1" in first_round, "老图应该只剩占位（编号要在）"
    assert "data:image" not in first_round, "老图不该再 inline（那是 6 条生命期之外）"

    second_round = looker.snapshots[1]
    multimodal = [m for m in second_round if isinstance(m.get("content"), list)]
    assert multimodal, "look_image 之后要有带图的多模态消息"
    parts = multimodal[-1]["content"]
    assert parts[0]["type"] == "text" and "工具执行结果" in parts[0]["text"]
    assert any(p.get("type") == "image_url" for p in parts), "图要真的附上"

    # data URL 不许落库（tools_used）
    with db.session_scope() as session:
        rows = message_service.recent(session, friend_id, limit=30)
    with_tools = [m.tools_used for m in rows if m.tools_used]
    assert with_tools, "这轮应该有工具记录"
    assert all("data:image" not in json.dumps(t) for t in with_tools)
