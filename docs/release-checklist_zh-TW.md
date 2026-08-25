---
layout: default
title: 發版檢查清單（繁體中文）
description: Release checklist - what must be true before anything is pushed
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/release-checklist.html) | **繁體中文**

# 發版與推送公開 repo 的檢查清單

公開 repo：<https://github.com/jasoncheng7115/jt-snmpd>

> **推上 GitHub 之後無法收回。** GitHub 會保留 fork、快取與 Git 歷史；
> 事後刪除只是把它從畫面上拿掉，內容仍可取得。因此每一項都要在推送**之前**完成。

---

## 0. 一次性：公開 repo 的歷史起點

本專案的開發歷史裡含有不對外的內部文件。**即使現在把它們移除，
推送既有歷史仍會把內容帶上去。** 因此公開 repo 以**全新歷史**起始：

```bash
# 在乾淨的暫存目錄產生只含公開內容的 repo
python3 tools/prepare-public-repo.py /tmp/jt-snmpd-public
cd /tmp/jt-snmpd-public
git init -b main
git add -A
git commit -m "jt-snmpd v<版本>"
git remote add origin git@github.com:jasoncheng7115/jt-snmpd.git
git push -u origin main
```

本機的開發 repo 保留完整歷史，不受影響。

---

## 釘住的東西

有三樣東西是釘死的，而且每一樣都是外部審閱指出來之後才釘的：

- **`requirements-build.txt`** —— 建置會安裝的每一個 Python 套件。agent 是預先算好
  BER 位元組的，pysnmp 與 pyasn1 的版本直接決定送出去的位元組；沒注意到的升級只要
  改了一個長度編碼，回應就會超過 1400 位元組上限，而在 LibreNMS 那端的症狀是
  間歇性抓不到資料，不是報錯。
- **Pillow 釘在產生精靈美術的那一版。** CI 會重新產生那些 BMP 並比對雜湊，
  換一個編碼器就會讓這項檢查以一個與這次發版無關的理由失敗。
- **每一個 GitHub Action 都釘 commit SHA。** 標籤是可以被作者重新指向的移動指標，
  commit 不行。標籤保留在後面的註解裡，讓這個釘子還讀得懂。

要更新其中任何一樣，都是一次有 CI 背書的刻意提交，而不是因為建置換了一天早上跑。

## 1. 每次推送前必做

### 1.1 個資 / 機密掃描

```bash
python3 tools/check-privacy.py
python3 tools/check-terminology.py
```

第二支檢查台灣用語。這個專案已經有十幾個詞是靠人讀完成品才發現的，
那是最貴的發現地點。一個詞是先在這裡出過錯，才會被列進清單，
所以有發現就是真的有問題。完整清單用 `python3 tools/check-terminology.py --list` 看，
不抄在這裡：把禁用詞逐一列出來的文件，自己就會被這支工具挑出來。

**有 `HIGH` 就不要推。** 掃描範圍是「git 實際會推上去的檔案」
（已追蹤 + 未被忽略的未追蹤檔），不是整個工作目錄。

**要在開發 repo 裡跑，不能只在公開 repo 裡跑。** 真正的憑證放在
`tools/.privacy-secrets`（未追蹤，且不會同步到公開 repo），
掃描器會逐字比對那份清單。公開 repo 沒有那個檔案，
所以在那裡跑的時候，它會直接印出「known secrets: NONE」告訴你這一項沒有在檢查 ——
這行字要看，它跟「什麼都沒找到」長得很像，意思完全不同。

| 等級 | 意義 | 處理 |
|---|---|---|
| `HIGH` | 私鑰、密碼、community 字串、MAC 位址、API 憑證、未審閱的圖片 | **必須修正**，不得推送 |
| `MED` | IP 位址、序號、電子郵件、UNC 路徑 | 逐項確認是文件用範例還是現場資料 |
| `LOW` | 主機名稱、內部網域、使用者路徑 | 專案擁有者已決定主機名稱可公開；仍應知道帶出了什麼 |

確認安全的項目可加入 `tools/privacy-allowlist.txt`，**每一條都要寫明理由**。
沒有理由的例外，久了就會變成「把所有警告關掉」的地方。

### 1.2 圖片人工審閱

正規表示式讀不到像素。**每一張新增或修改過的圖片都必須被人實際看過。**

實際踩過：為 README 拍的連接埠對照截圖，把 LibreNMS 畫出來的 SNMP 鄰居
一併帶了出去，`host-101-ipmi`、`vas1`、`dc2`、`router-003`、`ap-112`、`nas4`，
外加四組 MAC 位址。那等於公開內網拓撲圖。

檢查每張圖有沒有：

- [ ] MAC 位址（截圖腳本會自動改寫成 `xx:xx:xx:xx:xx:xx`，但要確認真的生效）
- [ ] 內網 IP（自己網段的位址，而非 `192.0.2.0/24` 這類文件用保留範圍）
- [ ] 硬體序號、授權金鑰、community 字串
- [ ] 鄰居裝置名稱（LibreNMS 的連接埠頁會顯示 SNMP / LLDP 鄰居）
- [ ] 使用者姓名、帳號、電子郵件
- [ ] 瀏覽器分頁列與書籤列（截圖時用無痕視窗或 headless）

確認後：

```bash
python3 tools/check-privacy.py --update-images
```

圖片內容一改動，雜湊就對不上，掃描會擋下推送，強迫重新審閱。

### 1.3 測試與版本

- [ ] `.venv/bin/python -m pytest tests/ -q` 全綠
- [ ] `deploy/version.py` 的版本已更新
- [ ] `CHANGELOG.md`（英文）與 `CHANGELOG_zh-TW.md`（繁中）**兩份都已更新**
- [ ] README 兩份的版本號一致
- [ ] `tests/lifecycle.ps1` 在實機跑過且 `LIFECYCLE_RESULT=PASS`

### 1.4 安裝檔

- [ ] MSI 已建置並歸檔至 `dist/releases/<版本>/`
- [ ] `BUILDINFO.txt` 的來源指紋（configure / wxs / agent）與 repo 一致
- [ ] **MSI 本身不進 git**（`.gitignore` 已排除），改由 GitHub Release 附加
- [ ] 附上 SHA-256

---

## 2. 不公開的內容

`.gitignore` 已排除，但每次仍應確認沒有被 `git add -f` 之類的動作繞過：

| 項目 | 原因 |
|---|---|
| 內部規格書 | 不對外 |
| 內部工作筆記 | 含正式環境的位址與作業紀律 |
| `reports/` | 掃描報告，含本機路徑 |
| `*.log`、`logs/` | agent 記錄檔會寫入介面名稱、磁碟型號與序號 |
| `state/`、`index-map.json`、`engine.json` | 執行時期狀態，含網路卡 LUID 與 engineID |
| `*.walk`、`*.snmpwalk` | 實機的原始 walk 輸出 |
| `dist/`、`build/`、`*.msi` | 建置產物 |
| `.env`、`*.pem`、`*.key`、`*.pfx` | 憑證與金鑰 |

---

## 3. 關於「agent 會回報什麼」與「repo 裡有什麼」

這兩件事要分清楚：

- **agent 本身會回報真實序號、真實介面名稱、真實 IP。** 那是它的工作，
  現場要換哪一顆磁碟、哪一條記憶體時，序號才是找得到的依據。
  這些資料停留在**客戶自己的監控系統**內。
- **公開 repo 裡不該有任何一台真實主機的資料。** 文件裡的位址一律使用
  RFC 5737 的保留範圍（`192.0.2.0/24`、`198.51.100.0/24`），序號以 `****` 取代。

換句話說，遮蔽是為了「這份文件會公開」，不是因為那些資料本身不該被採集。

---

## 4. 快速指令

```bash
# 完整檢查
.venv/bin/python -m pytest tests/ -q && python3 tools/check-privacy.py

# 只看會被推上去的檔案清單
git ls-files; git ls-files --others --exclude-standard

# 確認某個檔案是否被忽略（注意：對**已追蹤**的檔案無效，需先 git rm --cached）
git check-ignore -v <路徑>
```

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)
- [資安檢測工具鏈與報告](https://jasoncheng7115.github.io/jt-snmpd/security-scanning_zh-TW.html)
