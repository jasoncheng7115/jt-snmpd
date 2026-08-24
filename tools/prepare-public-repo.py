#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""產生只含公開內容的 repo 目錄，供推上 GitHub。

**為什麼不直接推現有的 repo**

開發歷史裡含有 `spec.md`（內部規格書）。Git 的歷史是內容定址的——
即使現在把檔案移除並提交，**推送既有歷史仍會把內容一起帶上去**，
而 GitHub 保留 fork 與快取，事後刪除只是把它從畫面上拿掉。

重寫歷史（filter-repo 之類）能做到，但一次沒做乾淨就洩漏了，
而且無法驗證「真的沒有殘留」。以全新歷史起始則是可驗證的：
**這個目錄裡有什麼，推上去的就是什麼。**

本機的開發 repo 保留完整歷史，不受影響。

用法::

    python3 tools/prepare-public-repo.py /tmp/jt-snmpd-public
    cd /tmp/jt-snmpd-public && python3 tools/check-privacy.py   # 再掃一次
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 明確不公開的項目。`.gitignore` 已涵蓋大部分，這裡是第二道防線——
# `.gitignore` 對**已追蹤**的檔案無效，而 spec.md 與 CLAUDE.md 正好都已被追蹤。
NEVER_PUBLISH = {
    "spec.md",              # 內部規格書
    "CLAUDE.md",            # 內部工作筆記，含正式環境位址與作業紀律
    "upt.b64",
}
NEVER_PUBLISH_DIRS = {"reports", "state", "logs", ".venv", "build", "dist",
                      "__pycache__", ".pytest_cache", ".git"}
NEVER_PUBLISH_SUFFIX = {".log", ".msi", ".exe", ".pem", ".key", ".pfx",
                        ".walk", ".snmpwalk", ".rrd"}

# 這些目錄要保留，但只留 README（說明用途，產物不進版控）
KEEP_README_ONLY = {"build", "dist"}


def should_skip(rel: Path) -> str | None:
    if rel.name in NEVER_PUBLISH:
        return f"明確排除：{rel.name}"
    if rel.suffix.lower() in NEVER_PUBLISH_SUFFIX:
        return f"副檔名排除：{rel.suffix}"
    for part in rel.parts:
        if part in NEVER_PUBLISH_DIRS:
            if part in KEEP_README_ONLY and rel.name == "README.md":
                return None
            return f"目錄排除：{part}/"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    dest = Path(sys.argv[1]).resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"目的地非空：{dest}\n請先清空，避免混入舊內容。", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    # 以 git 的視角列出檔案：已追蹤 + 未被忽略的未追蹤檔。
    # 直接走檔案系統會把 .venv 之類一併帶進來。
    files: set[str] = set()
    for cmd in (["git", "ls-files"],
                ["git", "ls-files", "--others", "--exclude-standard"]):
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout
        files.update(f for f in out.splitlines() if f.strip())

    copied, skipped = 0, []
    for rel_str in sorted(files):
        rel = Path(rel_str)
        src = ROOT / rel
        if not src.is_file():
            continue
        reason = should_skip(rel)
        if reason:
            skipped.append((rel_str, reason))
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1

    print(f"已複製 {copied} 個檔案 → {dest}")
    if skipped:
        print(f"\n排除 {len(skipped)} 個：")
        for rel_str, reason in skipped:
            print(f"  {rel_str:52} {reason}")

    print("\n下一步：")
    print(f"  cd {dest}")
    print("  python3 tools/check-privacy.py      # 在公開內容上再掃一次")
    print("  git init -b main && git add -A")
    print("  git commit -m 'jt-snmpd v<版本>'")
    print("  git remote add origin git@github.com:jasoncheng7115/jt-snmpd.git")
    print("  git push -u origin main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
