# 第三方资产与依赖声明

本文件列出仓库中包含的第三方资产和依赖。项目代码的 Apache-2.0 许可证不覆盖
这些资产；每一个资产都以自己的许可证为准。

## 资产

| 资产 | 来源 / 作者 | 许可证 |
|---|---|---|
| Vivi / AvatarSample_E VRM | VRoid / pixiv | CC0-1.0 |
| FirstPersonHand.glb | 从 Vivi 右手派生 | CC0-1.0 |
| Quaternius 动画库 | Quaternius | CC0-1.0 |
| Mood sounds | yumeFriend generated | CC0-1.0 |

## 依赖

Python 和 JavaScript 依赖各自保留其许可证。发布前请根据 `backend/requirements.txt`、
`frontend/package.json` 和锁文件生成完整清单。

## 不包含

本仓库不包含游戏本体数据、未授权模型、来源不明的音效、Live2D Cubism Core
或带个人信息的资源。Live2D 运行时由用户阅读并同意 Live2D 官方许可协议后，
通过应用内入口从 Live2D 官方地址下载到本机。
