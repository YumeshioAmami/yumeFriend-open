"""T2.2-3 立绘（D102）：对齐 LingChat 的目录/命名，导入即用；表情标记摘出来存 mood。"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.agent.parser import MoodTagParser
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend
from backend.services import portrait_service, voice_reply_service


def _png(color=(200, 120, 90), size=(400, 600)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, (*color, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeProvider:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.snapshots: list[list[dict]] = []
        self.config = ProviderConfig(
            name="fake",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.snapshots.append([dict(m) for m in messages])
        for piece in self._reply.split("|"):  # 用 | 切模拟"被 chunk 切开"
            yield {"type": "content", "delta": piece}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "portraits.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PORTRAIT_DIR", tmp_path / "portraits")
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    app = create_app()
    with TestClient(app) as client:
        client.patch(
            "/api/settings",
            json={
                "purposes": {
                    "chat": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "fake-model",
                        "api_key": "sk-fake",
                    }
                }
            },
        )
        yield app, client, tmp_path


def _friend(client, name: str = "阿月") -> str:
    return client.post("/api/friends", json={"name": name, "personality": "话不多"}).json()["id"]


def _lingchat_dir(tmp_path) -> str:
    """造一个"像 LingChat 那样"的角色目录。"""
    folder = tmp_path / "DeepSeek" / "avatar"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "正常.png").write_bytes(_png((90, 120, 200)))
    (folder / "高兴.png").write_bytes(_png((240, 200, 90)))
    (folder / "害羞.webp").write_bytes(_png((230, 150, 180)))
    (folder / "头像.png").write_bytes(_png((40, 40, 40), size=(256, 256)))
    (folder / "说明.txt").write_text("不是图", encoding="utf-8")
    return str(folder)


# ---------- 扫描 / 导入 / 增删查 ----------


def test_scan_recognizes_lingchat_naming(app_client, tmp_path):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    body = client.post(
        f"/api/friends/{friend_id}/portraits/scan", json={"dir": _lingchat_dir(tmp_path)}
    ).json()
    emotions = [item["emotion"] for item in body["candidates"]]
    assert set(emotions) == {"害羞", "高兴", "正常"}, "情绪 = 文件名；跳过 头像.* 和非图片"
    assert client.post(
        f"/api/friends/{friend_id}/portraits/scan", json={"dir": str(tmp_path / "没有这个目录")}
    ).status_code == 400


def test_portrait_transform_roundtrip(app_client):
    """D110：立绘的缩放 / 位移按朋友存（沉浸模式里拖出来的）。"""
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    body = client.patch(
        f"/api/friends/{friend_id}",
        json={"portrait_transform": {"scale": 1.4, "x": 12, "y": -30}},
    ).json()
    assert body["portrait_transform"] == {"scale": 1.4, "x": 12.0, "y": -30.0}

    listed = client.get("/api/friends").json()["friends"]
    item = next(f for f in listed if f["id"] == friend_id)
    assert item["portrait_transform"]["scale"] == 1.4

    # 没设过 = None（界面用默认位置）
    other = _friend(client, name="老王")
    assert client.get(f"/api/friends/{other}").json()["portrait_transform"] is None


def test_scan_accepts_the_parent_folder_too(app_client, tmp_path):
    """D108：用户填「角色文件夹」（立绘躺在它的 avatar/ 里）也得能扫到。

    实测踩过：填上一层 → 一张都认不到，用户只看到"没扫上"。
    """
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    parent = Path(_lingchat_dir(tmp_path)).parent  # …/DeepSeek
    body = client.post(
        f"/api/friends/{friend_id}/portraits/scan", json={"dir": str(parent)}
    ).json()
    emotions = [item["emotion"] for item in body["candidates"]]
    assert set(emotions) == {"害羞", "高兴", "正常"}
    assert body["used_dir"].endswith("avatar"), "要把真正扫的那一层回给前端"


def test_import_copies_files_and_can_set_avatar(app_client, tmp_path):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    source = _lingchat_dir(tmp_path)
    result = client.post(
        f"/api/friends/{friend_id}/portraits/import",
        json={"dir": source, "use_avatar": True},
    ).json()
    assert set(result["imported"]) == {"害羞", "高兴", "正常"}
    assert result["avatar_set"] is True

    listed = client.get(f"/api/friends/{friend_id}/portraits").json()["portraits"]
    assert {item["emotion"] for item in listed} == {"害羞", "高兴", "正常"}
    assert all(item["size"] for item in listed)

    # 头像也用上了（朋友详情里有 avatar_url）
    assert client.get(f"/api/friends/{friend_id}").json()["avatar_url"]

    # 取图 + 删图
    served = client.get(f"/api/friends/{friend_id}/portraits/高兴")
    assert served.status_code == 200 and served.headers["content-type"].startswith("image/")
    assert client.delete(f"/api/friends/{friend_id}/portraits/高兴").status_code == 204
    assert "高兴" not in portrait_service.emotions(friend_id)
    assert client.get(f"/api/friends/{friend_id}/portraits/高兴").status_code == 404


def test_partial_import_and_traversal_guard(app_client, tmp_path):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    client.post(
        f"/api/friends/{friend_id}/portraits/import",
        json={"dir": _lingchat_dir(tmp_path), "emotions": ["正常"]},
    )
    assert portrait_service.emotions(friend_id) == ["正常"]
    assert portrait_service.file_for(friend_id, "../../../yumefriend.db") is None
    assert portrait_service.file_for(friend_id, "a/b") is None
    assert portrait_service.file_for(friend_id, "不存在") is None


def test_purging_a_friend_clears_portraits(app_client, tmp_path):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    client.post(
        f"/api/friends/{friend_id}/portraits/import", json={"dir": _lingchat_dir(tmp_path)}
    )
    assert (tmp_path / "portraits" / friend_id).is_dir()
    assert client.post(f"/api/friends/{friend_id}/purge").status_code == 204
    assert not (tmp_path / "portraits" / friend_id).exists()


# ---------- 表情标记 ----------


def test_mood_parser_strips_split_tags():
    parser = MoodTagParser()
    assert parser.feed("你好呀") == "你好呀"
    assert parser.feed("[立") == ""          # 半截标签先扣着
    # v3.6（D156）：合上以后**再等一下** —— 下一个字符可能是冒号（`[开心]：`），
    # 现在摘了冒号就会漏出去；所以 mood 要等下一个 chunk 才确定。
    assert parser.feed("绘:高兴]") == ""
    assert parser.feed("，今天不错") == "，今天不错"
    assert parser.mood == "高兴"
    # 再来一个（取最后一个）+ 中文冒号也认
    assert parser.feed("[立绘：害羞]") == ""
    assert parser.feed("。") == "。"          # 同样：等下一个 chunk（看有没有冒号）才定下来
    assert parser.mood == "害羞"
    assert parser.flush() == ""


def test_mood_parser_leaves_normal_brackets_alone():
    parser = MoodTagParser()
    assert parser.feed("[图片#12 · 09-19] 记下了") == "[图片#12 · 09-19] 记下了"
    assert parser.mood is None


def test_reply_with_mood_tag_stores_mood_and_hides_the_tag(app_client, tmp_path):
    """整轮串起来：她写 `[立绘:高兴]` → 正文里看不到、mood 存下来、system 里有可用表情。"""
    app, client, _tmp = app_client
    friend_id = _friend(client)
    client.post(
        f"/api/friends/{friend_id}/portraits/import", json={"dir": _lingchat_dir(tmp_path)}
    )
    provider = FakeProvider("今天挺好呀。|[立绘:高兴]")
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)

    response = client.post(f"/api/friends/{friend_id}/messages", json={"content": "今天怎么样"})
    assert response.status_code == 200
    system = provider.snapshots[0][0]["content"]
    assert "# 你的表情" in system and "高兴" in system and "正常" in system
    # D109：表情要**每条都写**（用户实测："不明确说，她几乎不换表情"）
    assert "每说完一个气泡 / 自然段" in system and "一段一个" in system
    assert "不要默认全用" in system or "不要因为省事" in system
    assert "不写也行" not in system

    listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
    assistant = [m for m in listed if m["role"] == "assistant"][-1]
    assert assistant["mood"] == "高兴"
    assert "[立绘" not in assistant["content"], "标记不能让用户看到"
    assert assistant["content"].startswith("今天挺好呀")


def test_new_friend_gets_default_portrait_and_mood_section(app_client):
    """D424：没导立绘的新朋友也有默认「正常」，所以表情块不再缺席。"""
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    detail = client.get(f"/api/friends/{friend_id}").json()
    assert "正常" in detail["portrait_emotions"]
    img = client.get(f"/api/friends/{friend_id}/portraits/正常")
    assert img.status_code == 200

    provider = FakeProvider("在呢。")
    app = _app
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    client.post(f"/api/friends/{friend_id}/messages", json={"content": "在吗"})
    assert "# 你的表情" in provider.snapshots[0][0]["content"]


def test_mood_parser_also_accepts_star_labels():
    """D113：模型实测爱把情绪写成 `**生气**：`（剧本式），也得认出来 + 摘掉。"""
    parser = MoodTagParser()
    visible = parser.feed("**生气**：你又在逗我？\n\n**害羞**：你、你这样看我")
    assert visible == "你又在逗我？\n\n你、你这样看我"
    assert parser.moods == ["生气", "害羞"]
    assert [item["mood"] for item in parser.take_new_moods()] == ["生气", "害羞"]
    assert parser.take_new_moods() == [], "取过就该清空"

    # 半截的（被 chunk 切开）不许漏：先扣着，等 `：` 到了再摘
    split = MoodTagParser()
    first = split.feed("**兴")
    assert first == "" and split.moods == []
    rest = split.feed("奋**：看到没！")
    assert rest == "看到没！" and split.moods == ["兴奋"]


def test_mood_parser_offsets_keep_multiple_labels_on_different_segments():
    """同一块里连续多个 `**情绪**：` 时，偏移不能被算成同一个点。

    2026-11-08 用户实测：沉浸模式第一句表情会变，后面几句全一样。
    根因是 MoodTagParser 在同一个 feed() 里先 `_remember()` 再 append 文本，
    多个标签全拿到 `self.emitted`（同一个偏移），后端按段对齐时只能落成同一个情绪。
    """
    from backend.api.messages import _align_moods

    parser = MoodTagParser()
    parser.feed("**高兴**：第一段。\n\n**生气**：第二段。\n\n**害羞**：第三段。")
    marks = parser.take_new_moods()
    assert [item["mood"] for item in marks] == ["高兴", "生气", "害羞"]
    assert [item["at"] for item in marks] == sorted(item["at"] for item in marks)
    assert len({item["at"] for item in marks}) == 3
    assert _align_moods(["第一段。", "第二段。", "第三段。"], marks) == [
        "高兴",
        "生气",
        "害羞",
    ]


def test_mood_parser_leaves_plain_text_alone():
    parser = MoodTagParser()
    text = "我今天有点生气，不过没关系。"
    assert parser.feed(text) == text
    assert parser.moods == []


def test_speakable_text_drops_mood_labels():
    """D113：语音也不能念出 `**生气**：` 这种标签（包括给老消息"念给我听"）。"""
    assert voice_reply_service.speakable_text("**生气**：你又在逗我？") == "你又在逗我？"
    assert voice_reply_service.speakable_text("（歪头）[立绘:高兴] 你好呀。") == "你好呀。"
    assert "生气" not in voice_reply_service.speakable_text("**生气**：你又在逗我？")
