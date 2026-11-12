#!/usr/bin/env python3
"""生成按需下载的 minecraft-data 单版本资源包（D476）。

一个包只包含指定 Minecraft 正式版需要的数据目录，zip 根目录是
`minecraft-data/`。主程序下载后解压到
`user_data/minecraft/versions/<version>/`，再用它替换 Node 的
`require('minecraft-data')`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_BASE = "https://www.yumeshio.cn/uploads/yumefriend/minecraft"
DEFAULT_MC_DATA_VERSION = "3.117.0"
LICENSE_SOURCE = ROOT / "adapters" / "minecraft" / "third_party" / "minecraft-data.LICENSE.txt"


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in re.findall(r"\d+", value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"压缩包越界：{member.name}")
        bundle.extractall(destination)


def copy_tree(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"缺少资源目录：{source}")
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _pack_versions(paths: dict, tested: set[str]) -> list[tuple[str, str]]:
    pc = paths.get("pc") or {}
    candidates = [
        name for name in pc if re.fullmatch(r"\d+(?:\.\d+)+", str(name))
    ]
    if "1.8.8" not in candidates and "1.8" in pc:
        candidates.append("1.8.8")
    selected = []
    for name in candidates:
        if version_key(name) < version_key("1.8.8"):
            continue
        selected.append((name, "1.8" if name == "1.8.8" else name))
    selected.sort(key=lambda item: version_key(item[0]), reverse=True)
    return selected


def build_one(
    *,
    package_root: Path,
    data_dir: Path,
    paths: dict,
    version: str,
    data_version: str,
    protocol: int | None,
    stable: bool,
    out_dir: Path,
    public_base: str,
    work: Path,
) -> dict:
    mapped = version
    if version == "1.8.8" and "1.8.8" not in paths["pc"] and "1.8" in paths["pc"]:
        mapped = "1.8"
    entry_paths = paths["pc"][mapped]

    pack_root = work / f"minecraft-data-{version}"
    if pack_root.exists():
        shutil.rmtree(pack_root)
    package_dir = pack_root / "minecraft-data"
    package_dir.mkdir(parents=True)

    for name in ("index.js", "data.js", "package.json", "README.md", "index.d.ts"):
        source = package_root / name
        if source.is_file():
            shutil.copy2(source, package_dir / name)
    for name in ("lib",):
        copy_tree(package_root / name, package_dir / name)
    copy_tree(data_dir / "schemas", package_dir / "minecraft-data" / "schemas")
    copy_tree(
        data_dir / "data" / "pc" / "common",
        package_dir / "minecraft-data" / "data" / "pc" / "common",
    )
    copy_tree(
        data_dir / "data" / "bedrock" / "common",
        package_dir / "minecraft-data" / "data" / "bedrock" / "common",
    )

    keep = {"pc/common", "bedrock/common"}
    for rel in entry_paths.values():
        rel = str(rel).replace("\\", "/").strip("/")
        if rel.startswith("pc/"):
            keep.add(rel)
    for rel in sorted(keep):
        copy_tree(data_dir / "data" / rel, package_dir / "minecraft-data" / "data" / rel)

    pack_paths = {"pc": {version: entry_paths}}
    (package_dir / "minecraft-data" / "data" / "dataPaths.json").write_text(
        json.dumps(pack_paths, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if LICENSE_SOURCE.is_file():
        shutil.copy2(LICENSE_SOURCE, package_dir / "LICENSE")
    (package_dir / "_yumefriend_pack.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "version": version,
                "protocol": protocol,
                "minecraft_data_version": data_version,
                "stable": stable,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    zip_name = f"minecraft-data-{version}.zip"
    zip_path = out_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for file in sorted(package_dir.rglob("*")):
            if file.is_file():
                bundle.write(file, file.relative_to(pack_root).as_posix())

    return {
        "version": version,
        "protocol": protocol,
        "url": f"{public_base.rstrip('/')}/{zip_name}",
        "sha256": sha256_file(zip_path),
        "size": zip_path.stat().st_size,
        "tier": "stable" if stable else "compatible",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minecraft-data-version", default=DEFAULT_MC_DATA_VERSION)
    parser.add_argument("--package-tgz", type=Path)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist" / "minecraft_versions")
    parser.add_argument("--public-base", default=DEFAULT_PUBLIC_BASE)
    parser.add_argument(
        "--write-seed",
        action="store_true",
        help="把清单同时写入 assets/minecraft/versions.json",
    )
    args = parser.parse_args()

    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="yf-mc-packs-"))
    try:
        tgz = args.package_tgz
        if tgz is None:
            npm_dir = work / "npm"
            npm_dir.mkdir()
            npm_cmd = shutil.which("npm.cmd") or shutil.which("npm")
            if not npm_cmd:
                raise SystemExit("找不到 npm；请先安装 Node 或改用 --package-tgz")
            subprocess.run(
                [
                    npm_cmd,
                    "pack",
                    f"minecraft-data@{args.minecraft_data_version}",
                    "--silent",
                    "--pack-destination",
                    str(npm_dir),
                ],
                check=True,
                cwd=str(work),
            )
            archives = sorted(npm_dir.glob("minecraft-data-*.tgz"))
            if not archives:
                raise SystemExit("npm pack 没有生成 minecraft-data tgz")
            tgz = archives[0]
        tgz = Path(tgz).resolve()
        extract = work / "extract"
        extract.mkdir()
        safe_extract_tar(tgz, extract)
        package_root = extract / "package"
        data_dir = package_root / "minecraft-data"
        paths = json.loads((data_dir / "data" / "dataPaths.json").read_text(encoding="utf-8"))
        protocol_versions = json.loads(
            (data_dir / "data" / "pc" / "common" / "protocolVersions.json").read_text(
                encoding="utf-8"
            )
        )
        protocols = {
            str(item.get("minecraftVersion")): item.get("version")
            for item in protocol_versions
            if item.get("minecraftVersion") and isinstance(item.get("version"), int)
        }
        mineflayer = json.loads(
            (
                ROOT
                / "adapters"
                / "minecraft"
                / "node_modules"
                / "mineflayer"
                / "package.json"
            ).read_text(encoding="utf-8")
        )
        mineflayer_version = str(mineflayer.get("version") or "")
        tested = set()
        version_js = (
            ROOT / "adapters" / "minecraft" / "node_modules" / "mineflayer" / "lib" / "version.js"
        )
        match = re.search(
            r"const testedVersions = \[(.*?)\]",
            version_js.read_text(encoding="utf-8"),
            re.S,
        )
        if match:
            tested = set(re.findall(r"'([^']+)'", match.group(1)))

        entries = []
        for version, _mapped in _pack_versions(paths, tested):
            entry = build_one(
                package_root=package_root,
                data_dir=data_dir,
                paths=paths,
                version=version,
                data_version=args.minecraft_data_version,
                protocol=protocols.get(version),
                stable=version in tested,
                out_dir=out_dir,
                public_base=args.public_base,
                work=work,
            )
            entries.append(entry)
            print(
                f"{version:>8}  {entry['tier']:<10} "
                f"{entry['size'] / 1024 / 1024:6.2f} MB  {entry['protocol']}",
                flush=True,
            )

        manifest = {
            "schema": 1,
            "minecraft_data_version": args.minecraft_data_version,
            "mineflayer_version": mineflayer_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "versions": entries,
        }
        manifest_path = out_dir / "versions.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if args.write_seed:
            seed = ROOT / "assets" / "minecraft" / "versions.json"
            seed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest_path, seed)
        print(f"\nMANIFEST={manifest_path}")
        print(f"VERSIONS={len(entries)}")
        print(f"TOTAL_MB={sum(item['size'] for item in entries) / 1024 / 1024:.1f}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
