#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推上公開 repo 之前的個資／機密掃描。

**為什麼需要這支程式**

本專案的開發環境是一個真實的內網——那正是它的價值，量測數字都來自實機。
但同一件事也代表：量測結果、記錄檔、截圖、掃描報告裡到處都是主機名稱、
IP、MAC、硬體序號、community 字串。推上 GitHub 之後這些**無法收回**：
GitHub 會保留 fork、快取與 Git 歷史，事後刪除只是把它從畫面上拿掉。

實際踩過的例子：為 README 拍的連接埠對照截圖裡，LibreNMS 把 SNMP 鄰居
一起畫了出來——`host-101-ipmi`、`vas1`、`dc2`、`router-003.<內部網域>`、
`ap-112`、`nas4`。那等於把整張內網拓撲圖公開。grep 抓不到，因為那是像素。

**這支程式檢查什麼**

1. **文字檔**：以正規表示式找 IP、MAC、內部網域、序號、憑證、community。
2. **二進位檔（圖片為主）**：無法用正規表示式檢查，改用「人工審閱 + 雜湊」——
   每張圖必須列在 `docs/images/REVIEWED.md` 並附 SHA-256。圖片一改動雜湊就
   對不上，掃描直接擋下，強迫重新審閱。

**掃描範圍**是「git 實際會推上去的檔案」（已追蹤 + 未被忽略的未追蹤檔），
不是整個工作目錄——否則 `.venv` 會淹沒所有結果。

用法::

    python3 tools/check-privacy.py            # 掃描，有 HIGH 就 exit 1
    python3 tools/check-privacy.py --update-images   # 重新產生圖片審閱清單
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "tools" / "privacy-allowlist.txt"
IMAGE_MANIFEST = ROOT / "docs" / "images" / "REVIEWED.md"

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
                   ".pdf", ".msi", ".exe", ".dll", ".zip", ".7z", ".ttf", ".otf"}

HIGH, MED, LOW = "HIGH", "MED", "LOW"


# --- 規則 -------------------------------------------------------------------
# 每條規則：(嚴重度, 名稱, 正規表示式, 說明)
#
# IPv4 特別麻煩：OID 長得跟 IP 一模一樣（`1.3.6.1.2.1` 的前四段是合法 IPv4）。
# 因此比對後還要走 `_looks_like_real_ip()` 再判一次。
RULES: list[tuple[str, str, re.Pattern, str]] = [
    (HIGH, "private-key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
     "私鑰絕不可進版控"),
    (HIGH, "password", re.compile(
        r"""(?ix)\b(?:password|passwd|pwd|secret)\s*[=:]\s*["']?[^\s"'{}$<>]{4,}"""),
     "明文密碼"),
    # 只抓「命令列／設定檔裡的實際值」，不抓程式碼中的變數指派。
    # `community = v2c.apiMessage.get_community(msg)` 與 `COMMUNITY = "bench"`
    # 都不是機密，前者是取值、後者是測試常數——第一版把兩者都報成 HIGH，
    # 那種雜訊會讓人開始無視掃描結果，比不掃還糟。
    (HIGH, "community", re.compile(
        r"""(?x)
        \bCOMMUNITY=                     # 命令列／MSI 屬性形式，無空白
        # 佔位字不算洩漏。比對不分大小寫——第一版只擋大寫 YOUR，
        # 於是文件裡的 `your-community` 被報成 HIGH。
        (?![<$%{]|(?i:public|your|change|example|placeholder|xxx)\b)
        ["']? ([A-Za-z0-9_-]{2,})
        """),
     "SNMP community 字串（等同密碼）"),
    (HIGH, "mac-address", re.compile(
        r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"),
     "MAC 位址全球唯一，可識別特定硬體與廠商"),
    (HIGH, "api-token", re.compile(
        r"""(?ix)\b(?:api[_-]?key|access[_-]?token|bearer)\s*[=:]\s*["']?[A-Za-z0-9_\-]{16,}"""),
     "API 憑證"),
    (MED, "ipv4", re.compile(
        r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
     "IP 位址（內網位址會洩漏網段規劃）"),
    (MED, "ipv6", re.compile(
        r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b"),
     "IPv6 位址"),
    # 主機名稱：專案擁有者已決定可以公開，因此列為 LOW（僅供知悉，不擋推送）。
    # 保留規則而非刪除，是為了讓每次推送前仍看得到「這次帶出去了哪些名稱」——
    # 決定可以公開，不等於不需要知道公開了什麼。
    (LOW, "windows-hostname", re.compile(
        r"\b(?:DESKTOP|LAPTOP|WIN)-[A-Z0-9]{7}\b"),
     "Windows 主機名稱（已決定可公開）"),
    (LOW, "internal-domain", re.compile(
        r"\b[A-Za-z0-9][A-Za-z0-9-]*\.(?:local|lan|internal|intranet|corp|home\.arpa)\b"),
     "內部網域名稱（已決定可公開）"),
    # 只抓「序號的值」，不抓程式碼中的欄位名稱。
    # `SerialNumberOffset`、`serial_number` 這類識別字不是序號本身——
    # 第一版把它們全報成 MED，雜訊會蓋掉真正的發現。
    # 因此要求 serial 與值之間有明確的分隔（冒號、等號、空白 + 引號）。
    (MED, "hardware-serial", re.compile(
        r"""(?x)
        \b(?:[Ss]erial(?:\s+[Nn](?:umber|o\.?))?|S/N)\b
        \s*[:=#]?\s*
        ["']? (?![A-Za-z]*Offset\b|[A-Za-z]*Length\b)
        ([A-Z0-9]{6,24})\b
        """),
     "硬體序號可追溯到保固與資產紀錄"),
    (MED, "email", re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "電子郵件位址"),
    (MED, "unc-path", re.compile(r"\\\\[A-Za-z0-9_.-]{2,}\\[A-Za-z0-9_$.-]+"),
     "UNC 路徑會洩漏檔案伺服器名稱"),
    (LOW, "user-profile-path", re.compile(
        r"(?i)[A-Z]:\\Users\\(?!Public\b|<|%)[A-Za-z0-9._-]+"),
     "使用者設定檔路徑含帳號名稱"),
]

# 這些是文件用的保留位址，出現在範例裡完全正常（RFC 5737 / RFC 3849 / RFC 7042）
DOC_IPV4_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")
DOC_IPV6_PREFIX = "2001:db8"
DOC_MAC_PREFIX = "00:00:5e:00:53"


def _looks_like_real_ip(text: str) -> bool:
    """排除 OID、版本號等長得像 IP 的東西。

    `1.3.6.1.2.1.25` 的前四段是合法 IPv4；`0.0.0.0` 與 `255.255.255.255`
    是通配位址不算洩漏。判斷依據是「每段 0-255」加上「不是更長的點分數字串的一部分」。
    """
    parts = text.split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return False
    if text in ("0.0.0.0", "255.255.255.255", "127.0.0.1", "1.1.1.1", "8.8.8.8"):
        return False
    if text.startswith(DOC_IPV4_PREFIXES):
        return False
    # OID 常見前置碼
    if parts[0] in ("0", "1", "2") and parts[1] in ("0", "1", "2", "3", "4", "5", "6"):
        return False
    return True


def load_allowlist() -> list[re.Pattern]:
    """允許清單：每行一個正規表示式，`#` 開頭是註解。

    允許清單是**刻意做成需要理由的**——每一條都應該在旁邊寫清楚為什麼安全，
    否則久了就會變成「把所有警告都關掉」的地方。
    """
    if not ALLOWLIST.exists():
        return []
    out = []
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                out.append(re.compile(line))
            except re.error as exc:
                print(f"允許清單語法錯誤：{line}  ({exc})", file=sys.stderr)
    return out


def tracked_files() -> list[Path]:
    """git 實際會推上去的檔案：已追蹤 + 未被忽略的未追蹤檔。"""
    files: set[str] = set()
    failures = []
    for cmd in (["git", "ls-files"],
                ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            failures.append(f"{' '.join(cmd)}: {exc}")
            continue
        files.update(f for f in out.splitlines() if f.strip())

    # **絕不能無聲地掃 0 個檔案。** 在還沒 git init 的目錄裡，兩個 git 指令都會
    # 失敗，若把例外吞掉就會得到「未發現問題」——一個永遠說安全的掃描器，
    # 比沒有掃描器更危險，因為它讓人以為檢查過了。實測踩過這個情況。
    if not files:
        detail = "\n  ".join(failures) if failures else "（git 回報 0 個檔案）"
        raise SystemExit(
            f"無法取得檔案清單，掃描中止：\n  {detail}\n"
            f"目錄：{ROOT}\n"
            "若這是剛產生的公開 repo，請先 `git init -b main && git add -A` 再掃描。")
    return sorted((ROOT / f) for f in files if (ROOT / f).is_file())


def scan_text(path: Path, allow: list[re.Pattern]) -> list[tuple]:
    """掃描一個文字檔。回傳 [(嚴重度, 規則, 行號, 內容片段, 說明)]。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []

    findings = []
    lines = text.splitlines()
    for sev, name, pattern, why in RULES:
        for m in pattern.finditer(text):
            hit = m.group(0)
            if name == "ipv4" and not _looks_like_real_ip(hit):
                continue
            if name == "ipv6":
                low = hit.lower()
                if low.startswith(DOC_IPV6_PREFIX) or low in ("::1",) or ":" not in hit:
                    continue
                # OID 或時間戳不會有兩個以上的冒號分段字母，這裡再保守一點
                if not re.search(r"[a-fA-F]", hit):
                    continue
            if name == "mac-address" and hit.lower().startswith(DOC_MAC_PREFIX):
                continue
            line_no = text.count("\n", 0, m.start()) + 1
            context = lines[line_no - 1].strip() if line_no <= len(lines) else hit
            if any(a.search(context) or a.search(hit) for a in allow):
                continue
            findings.append((sev, name, line_no, hit, context[:110], why))
    return findings


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_reviewed() -> dict[str, str]:
    """讀取已人工審閱的圖片清單（路徑 → SHA-256）。"""
    if not IMAGE_MANIFEST.exists():
        return {}
    out = {}
    for line in IMAGE_MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def check_binaries(files: list[Path]) -> list[tuple]:
    """二進位檔（主要是截圖）走「人工審閱 + 雜湊」。

    正規表示式讀不到像素。唯一可靠的做法是要求每張圖被人看過並登記雜湊；
    圖片一改動雜湊就對不上，掃描直接擋下。
    """
    reviewed = load_reviewed()
    problems = []
    for f in files:
        if f.suffix.lower() not in BINARY_SUFFIXES:
            continue
        rel = str(f.relative_to(ROOT))
        digest = sha256(f)
        if rel not in reviewed:
            problems.append((HIGH, "image-unreviewed", rel, digest,
                             "未經人工審閱。圖片可能含 MAC、鄰居主機名稱、序號——"
                             "正規表示式看不到像素"))
        elif reviewed[rel] != digest:
            problems.append((HIGH, "image-changed", rel, digest,
                             f"內容已變動（登記為 {reviewed[rel][:12]}…），需重新審閱"))
    return problems


def update_manifest(files: list[Path]) -> None:
    rows = []
    for f in sorted(files):
        if f.suffix.lower() in BINARY_SUFFIXES:
            rows.append((str(f.relative_to(ROOT)), sha256(f)))
    body = [
        "# 圖片人工審閱紀錄",
        "",
        "正規表示式讀不到像素。README 的截圖曾經把 LibreNMS 畫出來的 SNMP 鄰居",
        "一併帶了出去——MAC 位址、內部主機名稱、IPv6 位址，等於公開內網拓撲。",
        "",
        "**每一張圖在加入或更新後都必須被人實際看過**，確認沒有：",
        "",
        "- MAC 位址",
        "- 真實主機名稱與內部網域",
        "- 內網 IP（自己網段的位址，而非文件用保留位址）",
        "- 硬體序號、授權金鑰、community 字串",
        "- 鄰居裝置名稱（LibreNMS 的連接埠頁會顯示 SNMP/LLDP 鄰居）",
        "- 使用者姓名與帳號",
        "",
        "確認後執行 `python3 tools/check-privacy.py --update-images` 更新下表。",
        "雜湊對不上時掃描會擋下推送，強迫重新審閱。",
        "",
        "| 檔案 | SHA-256 |",
        "|---|---|",
    ]
    body += [f"| `{p}` | `{h}` |" for p, h in rows]
    IMAGE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    IMAGE_MANIFEST.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"已更新 {IMAGE_MANIFEST.relative_to(ROOT)}（{len(rows)} 張圖）")


def main() -> int:
    ap = argparse.ArgumentParser(description="推上公開 repo 前的個資／機密掃描")
    ap.add_argument("--update-images", action="store_true",
                    help="重新產生圖片審閱清單（只在實際看過每張圖之後才用）")
    args = ap.parse_args()

    files = tracked_files()
    if args.update_images:
        update_manifest(files)
        return 0

    allow = load_allowlist()
    text_hits: dict[str, list] = {}
    for f in files:
        if f.suffix.lower() in BINARY_SUFFIXES:
            continue
        hits = scan_text(f, allow)
        if hits:
            text_hits[str(f.relative_to(ROOT))] = hits
    bin_hits = check_binaries(files)

    print(f"掃描範圍：{len(files)} 個檔案（git 實際會推上去的）\n")

    n_high = n_med = n_low = 0
    for rel, hits in sorted(text_hits.items()):
        print(f"── {rel}")
        for sev, name, line_no, hit, context, why in sorted(hits, key=lambda h: h[2]):
            n_high += sev == HIGH
            n_med += sev == MED
            n_low += sev == LOW
            print(f"   [{sev:4}] {name:18} 第 {line_no} 行  {hit!r}")
            print(f"          {context}")
        print()

    if bin_hits:
        print("── 圖片／二進位檔")
        for sev, name, rel, digest, why in bin_hits:
            n_high += 1
            print(f"   [{sev:4}] {name:18} {rel}")
            print(f"          {why}")
            print(f"          SHA-256 {digest}")
        print()

    print(f"結果：HIGH={n_high}  MED={n_med}  LOW={n_low}")
    if n_high:
        print("\n有 HIGH 等級的發現，**請勿推送**。")
        print("修正後重跑；確認無誤的項目可加入 tools/privacy-allowlist.txt（要寫理由）。")
        return 1
    if n_med or n_low:
        print("\n沒有 HIGH，但仍請逐項確認 MED／LOW 是否為文件用範例。")
    else:
        print("\n未發現問題。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
