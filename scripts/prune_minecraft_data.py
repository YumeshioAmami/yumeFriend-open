# -*- coding: utf-8 -*-
"""裁剪 minecraft-data：只保留指定 Java 版本 + 它们引用的共享目录（P4 / Q43）。

背景：`minecraft-data` 完整包 428.7 MB —— bedrock 331.8 MB（我们不用）+ pc 96.8 MB
（73 个版本）。我们只做 Java，留几个常用版本即可。

用法：
    python scripts/prune_minecraft_data.py              # 只算不改（dry-run）
    python scripts/prune_minecraft_data.py --apply      # 真删
    python scripts/prune_minecraft_data.py --versions 1.20.1,1.21.4 --apply

⚠️ `npm install` 会把完整数据装回来 → **每次打包前都要跑一次**（build_desktop.ps1 已接）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = (
    ROOT / "adapters" / "minecraft" / "node_modules"
    / "minecraft-data" / "minecraft-data" / "data"
)

# 常用 Java 版本（1.20.1 是用户当前在玩的；其余覆盖近几个大版本）
DEFAULT_VERSIONS = [
    "1.19.4",
    "1.20.1",
    "1.20.4",
    "1.20.6",
    "1.21.1",
    "1.21.4",
    "1.21.8",
    "26.1",
]


def _size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _mb(value: int) -> str:
    return f"{value / 1024 / 1024:.2f} MB"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的删（不加只算）")
    parser.add_argument(
        "--versions",
        default=",".join(DEFAULT_VERSIONS),
        help="逗号分隔的 Java 版本（默认常用 8 个）",
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help="不保留任何版本数据（发布包使用按需下载数据包）",
    )
    args = parser.parse_args()

    # 安全阀：只允许在 minecraft-data 的 data 目录里动手
    if DATA_DIR.name != "data" or DATA_DIR.parent.name != "minecraft-data":
        print(f"✗ 路径不对，拒绝执行：{DATA_DIR}", file=sys.stderr)
        return 2
    data_paths_file = DATA_DIR / "dataPaths.json"
    if not data_paths_file.is_file():
        print(f"✗ 找不到 {data_paths_file}（先 npm install）", file=sys.stderr)
        return 2

    versions = (
        []
        if args.empty
        else [item.strip() for item in args.versions.split(",") if item.strip()]
    )
    paths = json.loads(data_paths_file.read_text(encoding="utf-8"))
    pc = paths.get("pc") or {}
    missing = [version for version in versions if version not in pc]
    if missing:
        print(f"✗ 这些版本没有数据：{missing}", file=sys.stderr)
        return 2

    keep_dirs: set[str] = set()
    for version in versions:
        for rel in pc[version].values():
            rel = str(rel).replace("\\", "/").strip("/")
            if rel.startswith("pc/"):
                keep_dirs.add(rel)
    # `minecraft-data/lib/supportsFeature.js` 会**无条件** require 这两个文件
    # （即使我们只做 Java）——小（几百 KB），必须留。
    keep_dirs.add("pc/common")
    keep_dirs.add("bedrock/common")

    before = _size(DATA_DIR)
    kept_size = sum(_size(DATA_DIR / rel) for rel in keep_dirs)
    print(f"保留版本（{len(versions)}）：{', '.join(versions) or '无（按需下载）'}")
    print(f"引用目录（{len(keep_dirs)}）：{', '.join(sorted(keep_dirs))}")
    print(f"裁剪前：{_mb(before)}｜保留部分：{_mb(kept_size)}")

    if not args.apply:
        print("\n（dry-run：什么都没删；加 --apply 才真的删）")
        return 0

    # 1) 删掉没被引用的 pc 子目录
    pc_root = DATA_DIR / "pc"
    for child in pc_root.iterdir():
        if child.is_dir() and f"pc/{child.name}" not in keep_dirs:
            shutil.rmtree(child)

    # 2) bedrock 只留 common（supportsFeature 要用），其余删掉
    bedrock_root = DATA_DIR / "bedrock"
    if bedrock_root.exists():
        for child in bedrock_root.iterdir():
            if child.is_dir() and f"bedrock/{child.name}" not in keep_dirs:
                shutil.rmtree(child)

    # 3) 重写 dataPaths.json：只留保留版本（其它 editions 也去掉）
    new_paths = {"pc": {version: pc[version] for version in versions}}
    data_paths_file.write_text(
        json.dumps(new_paths, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    after = _size(DATA_DIR)
    print(f"\n✅ 裁剪完成：{_mb(before)} → {_mb(after)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
