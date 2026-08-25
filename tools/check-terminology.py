#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flag wording that is not Taiwanese usage.

The customers are Taiwanese government agencies and hospitals. Documentation
that reads as though it were translated from mainland Chinese usage undermines
the rest of the work, and it has had to be corrected by hand a dozen times in
this project -- the table below is the list, and every one of them was caught by
a person reading the finished page, which is the most expensive place to catch
it.

The prose deliberately does not enumerate them. A document that lists the words
it forbids is one this tool then flags, and the first draft of both this
docstring and the release checklist did exactly that.

This does not judge style, only vocabulary, and it is deliberately conservative:
a word goes in the list once it has actually been wrong here, so that a finding
means something. Words that are legitimate in both variants are not listed --
`項目` is fine for an item in a list, `文件` is fine when it means a document.

Usage::

    python3 tools/check-terminology.py            # scan; exits 1 on any finding
    python3 tools/check-terminology.py --list     # print the table
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# term -> (preferred, note). The note says why, because "wrong" is not useful
# to whoever has to fix it.
TERMS: dict[str, tuple[str, str]] = {
    "靜默":   ("無訊息 / 悄悄", "無人值守安裝用「無訊息」，與微軟正體中文文件一致"),
    "優化":   ("最佳化 / 調校", ""),
    "進程":   ("處理程序", ""),
    "行程":   ("處理程序", ""),
    "冗餘":   ("多餘", "「冗餘」在台灣多指備援，語意會偏掉"),
    "過濾":   ("篩選", "filter driver 是篩選器驅動程式"),
    "實例":   ("執行個體", ""),
    "持久化": ("保存", ""),
    "隧道":   ("通道", ""),
    "孤兒":   ("失去對應", ""),
    "剝除":   ("縮減", "權限縮減，不是權限剝除"),
    "阻塞":   ("受阻", ""),
    "安裝包": ("安裝檔", ""),
    "熱區":   ("溫度區", "ACPI thermal zone"),
    "迭代":   ("改版 / 反覆修改", ""),
    "自包含": ("把需要的東西全部包在裡面 / 完整一包", ""),
    "匹配":   ("相符 / 吻合", ""),
    "真機":   ("實機", ""),
    "網絡":   ("網路", ""),
    "信息":   ("資訊", ""),
    "默認":   ("預設", ""),
    "服務器": ("伺服器", ""),
    "端口":   ("連接埠", ""),
    "線程":   ("執行緒", ""),
    "緩存":   ("快取", ""),
    "隊列":   ("佇列", ""),
    "數據":   ("資料", ""),
    "內存":   ("記憶體", ""),
    "磁盤":   ("磁碟", ""),
    "軟件":   ("軟體", ""),
    "硬件":   ("硬體", ""),
    "支持":   ("支援", ""),
    "用戶":   ("使用者", ""),
    "激活":   ("啟用", ""),
    "字符串": ("字串", ""),
    "內核":   ("核心", ""),
    "補丁":   ("修補程式", ""),
    "卸載":   ("解除安裝", ""),
    "登錄帳": ("登入帳", "登入是動作，登錄檔是 registry"),
    "註冊表": ("登錄檔", ""),
    "命令行": ("命令列", ""),
    "集群":   ("叢集", ""),
    "調用":   ("呼叫", ""),
    "返回":   ("回傳", ""),
    "崩潰":   ("當掉 / 損毀", ""),
    "質量":   ("品質", ""),
    "視頻":   ("影片", ""),
    "音頻":   ("音訊", ""),
}

# Phrases that legitimately contain a listed term. 用戶端 is the standard
# Taiwanese rendering of "client" -- it is 用戶 on its own that should be
# 使用者.
ALLOWED_PHRASES = (
    "用戶端",
)

# A line that names a word in order to reject it is not using it. Both
# changelogs record every one of these corrections, and a checker that flags its
# own changelog is the style guide that violates itself by listing the words it
# forbids.
def _is_being_rejected(context: str, term: str) -> bool:
    """`context` is the surrounding text, not the line.

    A wrapped changelog entry puts "rather than" at the end of one line and the
    word it rejects at the start of the next, so a line-by-line test misses it
    and flags the sentence that exists to say the word is wrong.
    """
    flat = " ".join(context.split())
    return any(marker in flat for marker in (
        f"非{term}", f"不是{term}", f"不用{term}", f"rather than {term}",
        f"not {term}", f"（{term}", f"（非{term}",
    ))

SUFFIXES = {".md", ".html", ".yml", ".yaml", ".py", ".ps1", ".wxs", ".json", ".txt"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
             "build", "dist", "reports"}
# Not published, and not ours to rewrite: spec.md is the original specification
# as it was written, CLAUDE.md is internal notes, phase0-findings.md is a record
# of what was true at the time. Correcting wording in any of the three would
# falsify a record without improving anything a customer reads.
SKIP_FILES = {
    "phase0-findings.md", "spec.md", "CLAUDE.md",
    # This file. The table below has to contain every word it rejects, so
    # scanning it can only ever produce one finding per entry. That is the whole
    # of the exemption: nothing else here is excused, and the prose above is
    # written so it would pass on its own.
    "check-terminology.py",
}


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split("\n")
    except (subprocess.CalledProcessError, FileNotFoundError):
        out = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]
    files = []
    for rel in out:
        if not rel:
            continue
        p = ROOT / rel
        if p.suffix.lower() not in SUFFIXES or p.name in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_file():
            files.append(p)
    return sorted(files)


def scan(path: Path) -> list[tuple[int, str, str, str, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    hits = []
    for term, (better, note) in TERMS.items():
        for m in re.finditer(re.escape(term), text):
            line_no = text.count("\n", 0, m.start()) + 1
            line = text.splitlines()[line_no - 1] if line_no <= text.count("\n") + 1 else term
            if any(ph in line and term in ph for ph in ALLOWED_PHRASES):
                continue
            window = text[max(0, m.start() - 140):m.end() + 140]
            if _is_being_rejected(window, term):
                continue
            hits.append((line_no, term, better, note, line.strip()[:100]))
    return sorted(hits)


def main() -> int:
    ap = argparse.ArgumentParser(description="Flag wording that is not Taiwanese usage")
    ap.add_argument("--list", action="store_true", help="print the table and exit")
    args = ap.parse_args()

    if args.list:
        for term, (better, note) in sorted(TERMS.items()):
            print(f"  {term:8} -> {better}" + (f"   ({note})" if note else ""))
        return 0

    files = tracked_files()
    total = 0
    for f in files:
        hits = scan(f)
        if not hits:
            continue
        print(f"\n── {f.relative_to(ROOT)}")
        for line_no, term, better, note, line in hits:
            total += 1
            print(f"   line {line_no:<5} 「{term}」-> {better}")
            if note:
                print(f"        {note}")
            print(f"        {line}")

    print(f"\nscope: {len(files)} files    findings: {total}")
    if total:
        print("\nThese read as translated rather than written. Fix them, or if a "
              "hit is legitimate\nin context, add the surrounding phrase to "
              "ALLOWED_PHRASES with the reason.")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
