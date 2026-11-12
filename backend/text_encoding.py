"""文本文件的字节 → 文字（编码探测）。

**为什么要单独一个模块**：知识库（`knowledge_service`）和文件工具（`file_service`）都要读用户的
文件，两边**栽在同一个坑上**：

Windows 记事本存"Unicode" 存的是 **UTF-16LE + BOM**（前两字节 `ff fe`），而旧代码的候选编码表里
**没有 UTF-16**，最后落到**永远不会抛错**的 `latin-1`：

· 知识库那边 → 整份资料被"一个字节一个字"地解成乱码存进库（用户那份 40 万字的《传习录》全糊，
  朋友检索时"一个字都翻不到"）；
· 文件工具那边 → `_is_probably_text` 看到 UTF-16 里的 NUL 就判成"二进制"，直接拒读。

两条教训：**① "永不报错"的兜底编码必须排最后；② 认编码之前先把 UTF-16 摘出来**（它的 NUL 会骗过
"看有没有 NUL"这类朴素检测）。
"""

from __future__ import annotations


def decode_utf16(raw: bytes) -> str | None:
    """是 UTF-16 / UTF-32 就解出来，否则返回 None。"""
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw.decode("utf-32")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    # 没有 BOM 的 UTF-16：靠 NUL 的分布认（ASCII 字符在高字节留 0）
    sample = raw[:4096]
    if len(sample) >= 16 and sample.count(0) / len(sample) >= 0.12:
        even = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i] == 0)
        odd = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
        if even >= odd * 5 or odd >= even * 5:
            return raw.decode("utf-16-be" if even > odd else "utf-16-le")
    return None


def decode_text(raw: bytes) -> str:
    """字节 → 文字。顺序：UTF-16 → utf-8-sig（顺手吃 BOM）→ utf-8 → gb18030 → latin-1（保底）。"""
    decoded = decode_utf16(raw)
    if decoded is not None:
        return decoded.lstrip("\ufeff")
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def looks_like_text(raw: bytes) -> bool:
    """像文本吗（给"读文件"工具用）。⚠️ **不能只看 NUL**：UTF-16 的文字里全是 NUL。"""
    if decode_utf16(raw) is not None:
        return True
    if b"\x00" in raw[:2048]:
        return False
    for encoding in ("utf-8", "gb18030"):
        try:
            raw.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False
