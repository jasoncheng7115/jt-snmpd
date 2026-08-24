"""個資掃描工具本身的行為。

**為什麼要測工具**

掃描器的失效模式很特別：它不會噴錯，它會說「未發現問題」。

實測踩過——在剛用 `prepare-public-repo.py` 產生、還沒 `git init` 的目錄裡執行，
兩個 `git ls-files` 都以退出碼 128 失敗，例外被吞掉，檔案清單是空的，
於是印出「掃描範圍：0 個檔案」與「未發現問題」。

**一個永遠說安全的掃描器，比沒有掃描器更危險**，因為它讓人以為檢查過了。
這個檔案把「取不到清單就必須中止」釘死。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
SCANNER = TOOLS / "check-privacy.py"
SRC = SCANNER.read_text(encoding="utf-8")


def _func(name: str) -> str:
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    pytest.fail(f"找不到 {name}")


def test_scanner_exists_and_is_executable():
    assert SCANNER.exists()
    if sys.platform != "win32":
        # Windows 沒有 POSIX 執行位元，git 也不保留它——在那裡驗這個
        # 只會得到與程式碼無關的失敗。
        assert SCANNER.stat().st_mode & 0o111, "掃描器應可直接執行"


def test_empty_file_list_aborts_rather_than_passing():
    """核心斷言：取不到檔案清單時必須中止，不可回報「未發現問題」。"""
    body = _func("tracked_files")
    assert "if not files:" in body, "缺少空清單的檢查"
    assert "SystemExit" in body, "空清單必須中止執行"


def test_abort_message_explains_the_fix():
    """錯誤訊息要能讓人知道下一步做什麼，否則只會被當成雜訊繞過。"""
    body = _func("tracked_files")
    assert "git init" in body, "訊息應說明如何修正"


def test_scanner_aborts_in_a_non_git_directory(tmp_path):
    """實際跑一次：在非 git 目錄下必須以非零碼結束。"""
    (tmp_path / "tools").mkdir()
    for f in ("check-privacy.py", "privacy-allowlist.txt"):
        src = TOOLS / f
        if src.exists():
            (tmp_path / "tools" / f).write_text(src.read_text(encoding="utf-8"),
                                                encoding="utf-8")
    # Force UTF-8 both ways. Windows consoles default to a legacy code page, so
    # without this the child's non-ASCII output comes back backslash-escaped and
    # the assertion below compares against something that can never match.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    r = subprocess.run([sys.executable, str(tmp_path / "tools" / "check-privacy.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=tmp_path, env=env)
    assert r.returncode != 0, "非 git 目錄下不得回報成功"
    assert "掃描中止" in (r.stderr + r.stdout), \
        f"stdout={r.stdout[:200]!r} stderr={r.stderr[:200]!r}"


def test_high_severity_blocks_the_push():
    body = _func("main")
    assert "if n_high:" in body and "return 1" in body, "有 HIGH 必須以非零碼結束"


def test_images_require_review_not_pattern_matching():
    """正規表示式讀不到像素。圖片走「人工審閱 + 雜湊」。"""
    body = _func("check_binaries")
    assert "image-unreviewed" in body and "image-changed" in body
    assert "sha256" in body, "必須以雜湊偵測圖片變動"


def test_allowlist_entries_require_a_reason():
    """允許清單是刻意做成需要理由的——沒有理由的例外，久了就會變成
    「把所有警告關掉」的地方。"""
    al = TOOLS / "privacy-allowlist.txt"
    assert al.exists()
    text = al.read_text(encoding="utf-8")
    comment_lines = [l for l in text.splitlines() if l.strip().startswith("#")]
    rule_lines = [l for l in text.splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    assert len(comment_lines) >= len(rule_lines), (
        "註解行數應不少於規則行數——每條例外都要有理由")


def test_ip_rule_ignores_oids():
    """OID 的點分數字格式與 IPv4 完全相同（`1.3.6.1.2.1` 前四段是合法 IPv4）。
    分不開就會被幾千個 OID 淹沒，掃描結果變成沒人看的雜訊。"""
    sys.path.insert(0, str(TOOLS))
    import importlib.util
    spec = importlib.util.spec_from_file_location("cp", SCANNER)
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    assert not cp._looks_like_real_ip("1.3.6.1")
    assert not cp._looks_like_real_ip("0.0.0.0")
    assert not cp._looks_like_real_ip("192.0.2.10"), "文件保留範圍不算洩漏"
    assert cp._looks_like_real_ip("192.168.1.68")
    assert cp._looks_like_real_ip("172.16.5.4")
