# yumeFriend

一个本地优先、可扩展的 AI 朋友 / 角色扮演框架。

yumeFriend 在自己的电脑上运行，聊天、记忆、人格和媒体默认保存在本机。
它支持独立人格、OpenAI 兼容模型、3D 小世界和可选的语音 / 图片 / Live2D 能力。

## 现在能做什么

- 创建有独立人格的朋友，聊天、记忆和切换 NORMAL / ROLEPLAY / TOOL 模式；
- 接入任何 OpenAI 兼容的聊天模型；
- 本地保存聊天、记忆、人格、设置和媒体文件；
- 3D 小世界：房间、自由活动、触区、衣橱和房间内对话；
- 可选的语音、图片和角色表现能力。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

cd frontend
npm install
npm run build
cd ..

.\start_yumeFriend.bat
```

默认后端只监听 `127.0.0.1:8001`。不要把它直接暴露到公网。

## 数据在哪里

聊天记录、记忆、人格、图片、语音缓存、模型设置和 API Key 默认保存在本机的
`user_data/` 目录。这个目录不属于源码，也不应该提交到 Git。

API Key 目前在本机 SQLite 中明文保存。请自行保护电脑账户和备份文件。

## 隐私与边界

- 本项目不内置遥测；
- 只有你主动配置或触发的能力才会访问第三方模型、语音、搜索、更新或同步服务；
- 第三方服务如何处理数据，由对应服务商决定；
- 本仓库只包含普通技术示例；请遵守适用法律法规和托管平台规则。

详见 `PRIVACY.md`、`TERMS_OF_USE.md` 和 `CONTENT_POLICY.md`。

## 参与

第一阶段只接受 issue，不接受代码 PR。请先阅读 `CONTRIBUTING.md`。

## 许可证

项目代码采用 Apache-2.0。第三方模型、动画、依赖和 SDK 各自保留自己的许可证，
详见 `THIRD_PARTY_NOTICES.md`。

本项目与 pixiv / VRoid、Minecraft 或其他第三方厂商没有隶属关系。
