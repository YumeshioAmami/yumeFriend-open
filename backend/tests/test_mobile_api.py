"""v3.0 / B12：「手机端」那页的后端（检测 Tailscale / 配 serve / 二维码 / APK）。

盯四件事：

1. **没装 Tailscale** → 状态说清"没装 + 去哪儿下"，不是异常
2. 装了没登录 / 登录了 → 域名、serve 配没配，都读得出来（拿假 CLI 输出喂它）
3. **一键配 serve** 会真的去跑那条命令（参数对不对），失败给人话
4. 二维码 / APK：没准备好就 400/404 + 人话；准备好了就真给东西
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.services import mesh_service


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "mobile.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _fake_cli(monkeypatch, *, state="Running", dns="my-pc.tail1234.ts.net", serve_ok=True):
    """假装 `tailscale` 就在那儿、输出也可控（不碰真机器）。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(mesh_service, "installed", lambda: True)
    monkeypatch.setattr(mesh_service, "exe", lambda: Path("C:/Program Files/Tailscale/tailscale.exe"))

    def _run(args, timeout=20.0):  # noqa: ANN001, ARG001
        calls.append(args)
        if args[:2] == ["status", "--json"]:
            return 0, json.dumps(
                {
                    "BackendState": state,
                    "Self": {"DNSName": dns + ".", "TailscaleIPs": ["100.64.0.1"]},
                }
            )
        if args[:2] == ["serve", "status"]:
            # 真输出的形状（判据是"有没有转发到我们的端口"）
            return (
                0,
                (
                    "https://my-pc.tail1234.ts.net (tailnet only)\n"
                    f"|-- / proxy http://127.0.0.1:{config.PORT}"
                    if serve_ok
                    else "No serve config"
                ),
            )
        if args[:1] == ["serve"]:
            return (0, "available on the tailnet") if serve_ok else (1, "not logged in")
        return 1, "?"

    monkeypatch.setattr(mesh_service, "_run", _run)
    return calls


# ---------- 状态 ----------


def test_status_says_not_installed_with_a_link(client, monkeypatch):
    monkeypatch.setattr(mesh_service, "installed", lambda: False)
    monkeypatch.setattr(mesh_service, "exe", lambda: None)
    # 这台机器上可能真有可用的局域网转发地址；本测试只看“没装组网软件”的提示。
    monkeypatch.setattr(mesh_service, "phone_urls", lambda _port=None: [])

    body = client.get("/api/mobile/status").json()

    assert body["installed"] is False
    assert "没装" in body["hint"]
    assert body["download_url"].startswith("https://tailscale.com/")
    assert body["serve_url"] is None


def test_status_reads_dns_and_serve(client, monkeypatch):
    _fake_cli(monkeypatch)

    body = client.get("/api/mobile/status").json()

    assert body["installed"] and body["logged_in"]
    assert body["dns_name"] == "my-pc.tail1234.ts.net"
    assert body["serve_url"] == "https://my-pc.tail1234.ts.net/m/"
    assert body["serve_configured"] is True


def test_status_when_logged_out(client, monkeypatch):
    _fake_cli(monkeypatch, state="NeedsLogin", dns="")
    monkeypatch.setattr(mesh_service, "phone_urls", lambda _port=None: [])

    body = client.get("/api/mobile/status").json()

    assert body["logged_in"] is False
    assert "登录" in body["hint"] or "Connect" in body["hint"]


# ---------- 一键配好 ----------


def test_configure_serve_runs_the_right_command(client, monkeypatch):
    calls = _fake_cli(monkeypatch)

    body = client.post("/api/mobile/serve").json()

    assert body["ok"] is True
    assert any(call[:1] == ["serve"] and call[1] == "--bg" for call in calls)
    # 参数必须是"转发到本机后端"这一条
    assert any("127.0.0.1" in arg for call in calls for arg in call)
    assert body["status"]["serve_url"].endswith("/m/")


def test_configure_serve_failure_is_a_human_line(client, monkeypatch):
    _fake_cli(monkeypatch, serve_ok=False)

    body = client.post("/api/mobile/serve").json()

    assert body["ok"] is False
    assert "登录" in body["hint"]


def test_configure_serve_without_tailscale(client, monkeypatch):
    monkeypatch.setattr(mesh_service, "installed", lambda: False)

    body = client.post("/api/mobile/serve").json()

    assert body["ok"] is False and "还没装" in body["hint"]


# ---------- 二维码 / APK ----------


def test_qr_needs_some_address_at_all(client, monkeypatch):
    """一条地址都没有（没组网软件、也没探到转发口）→ 400 + 人话。"""
    monkeypatch.setattr(mesh_service, "installed", lambda: False)
    monkeypatch.setattr(mesh_service, "phone_urls", lambda port=None: [])
    monkeypatch.setattr(mesh_service, "_local_lan_urls", lambda port: [])

    response = client.get("/api/mobile/qr.png")

    assert response.status_code == 400
    assert "转发口" in response.json()["error"]["message"]


def test_qr_is_a_real_png_of_our_deep_link(client, monkeypatch):
    _fake_cli(monkeypatch)

    response = client.get("/api/mobile/qr.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    # 深链格式：App 的 onNewIntent 认的就是它
    assert mesh_service.deep_link("https://x.ts.net/m/").startswith("yumefriend://open?url=")


def test_apk_endpoint_finds_the_built_apk(client, monkeypatch, tmp_path):
    fake = tmp_path / "dist"
    fake.mkdir()
    apk = fake / "yumeFriend-1.0.0-android.apk"
    apk.write_bytes(b"PK\x03\x04fake")
    newer = fake / "yumeFriend-7.18.0-android.apk"
    newer.write_bytes(b"PK\x03\x04newer")
    fake.joinpath("yumeFriend-7.9.0-android.apk").write_bytes(b"PK\x03\x04old")
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)

    head = client.get("/api/mobile/status").json()
    assert head["apk_ready"] is True and head["apk_name"] == newer.name

    response = client.get("/api/mobile/apk")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.android.package-archive"


def test_apk_endpoint_says_how_to_build_it(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)  # 空目录：没有 apk

    response = client.get("/api/mobile/apk")

    assert response.status_code == 404
    assert "build_apk" in response.json()["error"]["message"]

# ---------- v3.4（D150）：局域网地址 + "手机没上报地址"的诊断 ----------


def test_lan_urls_only_keeps_real_lan():
    """只留"局域网"那张网卡：Tailscale / 公网 / 别的 VPN / link-local / IPv6 全部排除。"""
    addrs = [
        "123.139.56.67:41641",   # 公网出口
        "10.138.9.243:41641",    # 真·局域网 ✓
        "26.6.92.245:41641",     # Radmin VPN（虚拟网卡）
        "100.70.28.48:41641",    # Tailscale 自己
        "169.254.10.10:41641",   # link-local
        "127.0.0.1:41641",
        "[240c:c28f::1]:41641",  # IPv6
        "192.168.1.7:41641",     # 家用局域网 ✓
    ]
    assert mesh_service._lan_urls(addrs, 8123) == [
        "http://10.138.9.243:8123/m/",
        "http://192.168.1.7:8123/m/",
    ]


def test_qr_lan_kind_falls_back_to_the_phone_address(client, monkeypatch):
    """同一个 Wi-Fi 那条拿不到时**别给 400** —— 退回"手机该填的那条"（2026-10-01 / D225）。"""
    _fake_cli(monkeypatch)
    monkeypatch.setattr(mesh_service, "_local_lan_urls", lambda port: [])
    monkeypatch.setattr(
        mesh_service, "phone_urls", lambda port=None: [{"url": "http://172.16.2.196:8899/m/", "ok": True}]
    )

    r = client.get("/api/mobile/qr.png?kind=lan")

    assert r.status_code == 200


def test_status_and_qr_carry_the_phone_address(client, monkeypatch):
    """2026-10-01（D225）：**手机该填的地址**（组网网卡 IP + 电脑上的转发口）要进 status，
    默认那一张二维码就是它 —— 因为 `lan_urls`（:8001）那条后端只绑 127.0.0.1，根本不通。"""
    _fake_cli(monkeypatch)
    monkeypatch.setattr(
        mesh_service,
        "phone_urls",
        lambda port=None: [
            {"url": "http://172.16.2.196:8899/m/", "ok": True},
            {"url": "http://10.138.9.243:8899/m/", "ok": False},
        ],
    )

    state = client.get("/api/mobile/status").json()

    assert state["phone_urls"][0] == {"url": "http://172.16.2.196:8899/m/", "ok": True}
    assert "172.16.2.196:8899" in state["hint"]  # 有能用的就直接当家
    assert client.get("/api/mobile/qr.png").status_code == 200


def test_status_and_qr_carry_the_lan_address(client, monkeypatch):
    _fake_cli(monkeypatch)
    monkeypatch.setattr(
        mesh_service, "_local_lan_urls", lambda port: [f"http://10.0.0.5:{port}/m/"]
    )
    state = client.get("/api/mobile/status").json()
    assert state["lan_urls"] == [f"http://10.0.0.5:{config.PORT}/m/"]
    assert state["warnings"] == []
    r = client.get("/api/mobile/qr.png?kind=lan")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_path_hint_tells_you_what_to_do_on_the_phone(client, monkeypatch):
    """没直连（走中继）时，要直接说清"去手机上做什么"。

    ⚠️ 判据一律用 `tailscale ping` 的实测结果 —— `Peer.Addrs` 实测不可靠
    （2026-09-23：直连 13ms 时它依然是 null）。
    """
    monkeypatch.setattr(mesh_service, "installed", lambda: True)
    monkeypatch.setattr(mesh_service, "exe", lambda: Path("C:/t/tailscale.exe"))

    def _run(args, timeout=20.0):  # noqa: ANN001, ARG001
        if args[:2] == ["status", "--json"]:
            return 0, json.dumps(
                {
                    "BackendState": "Running",
                    "Self": {
                        "DNSName": "pc.tail.ts.net.",
                        "TailscaleIPs": ["100.64.0.1"],
                        "Addrs": ["10.0.0.9:41641", "26.6.92.245:41641"],
                    },
                    "Peer": {
                        "p1": {
                            "DNSName": "phone.tail.ts.net.",
                            "HostName": "phone",
                            "OS": "android",
                            "Online": True,
                            "Relay": "nue",
                            "Addrs": [],
                        }
                    },
                }
            )
        if args[:2] == ["serve", "status"]:
            return 0, f"https://pc.tail.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:{config.PORT}"
        return 1, "?"

    monkeypatch.setattr(mesh_service, "_run", _run)
    monkeypatch.setattr(
        mesh_service, "_probe_path", lambda target: (False, "via DERP(nue) in 400ms")
    )

    state = client.get("/api/mobile/status").json()
    assert state["peers"][0]["direct"] is False
    assert "没直连" in state["path_hint"]
    assert "手机端组网软件" in state["path_hint"]
    assert state["lan_urls"] == [f"http://10.0.0.9:{config.PORT}/m/"]
    # 顺手抓出"别的 VPN 网卡"（26.x）
    assert any("Radmin" in w for w in state["warnings"])
