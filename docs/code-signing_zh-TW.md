---
layout: default
title: 程式碼簽章（繁體中文）
description: The installer is unsigned - what you will see, and how to handle it
---

[← 回到說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/code-signing.html) | **繁體中文**

# 本安裝檔未經程式碼簽章

MSI 以及它所安裝的執行檔，都**沒有 Authenticode 簽章**。目前沒有申請憑證的計畫，
因此這是每一個版本都會有的狀態，不是暫時的缺口。

Authenticode 簽章做兩件事：證明檔案來自某個具名的發行者，以及證明位元組在發行後
未被竄改。本專案以**每個版本都公布 SHA-256** 回答後者，前者則沒有回答。
以下說明安裝時實際會看到什麼，以及該怎麼處理。

---

## 1. 實際會看到的畫面

| 情境 | 出現什麼 | 該怎麼做 |
|---|---|---|
| 用瀏覽器下載 | Edge 或 Chrome 可能提示這個檔案「不常被下載」 | 保留檔案，接著驗證 SHA-256（§2） |
| 點兩下開啟 MSI | Microsoft Defender SmartScreen：**「Windows 已保護您的電腦」** | 雜湊核對無誤後，選**其他資訊 → 仍要執行** |
| 使用者帳戶控制（UAC）提示 | 發行者顯示為**不明**，且是黃色橫幅而非已驗證的藍色橫幅 | 正常現象。確認正在提升權限的就是剛才驗證過的那個檔案 |
| 在已提升權限的主控台執行 `msiexec /qn` | 完全不會有提示 —— SmartScreen 只攔截互動式啟動 | 不需處理 |
| 以 GPO 派送軟體 | 沒有任何提示 —— 安裝以 SYSTEM 身分執行，沒有互動工作階段 | 不需處理。把 MSI 放在內部共用資料夾（§3） |
| 已啟用 WDAC 或 AppLocker | **會被封鎖。** 未簽章的檔案無法以發行者規則放行 | 加入雜湊規則（§4），或自行簽章（§5） |
| Microsoft Defender | PyInstaller 的產物有被啟發式誤判的紀錄 | 若被隔離，送出樣本並暫時加入排除項目（§6） |

以上都不是安裝程式的錯誤，而是 Windows 正確地表達「我無法確認這個檔案是誰做的」。

---

## 2. 驗證下載的檔案 —— 這一步最重要

雜湊就是簽章原本會在安裝時提供的完整性檢查。每個版本都會在 MSI 旁附上
`<msi 檔名>.sha256`。

```powershell
# CLI，在同時放著兩個檔案的資料夾中執行
Get-FileHash .\jt-snmpd-0.9.2-x64.msi -Algorithm SHA256
Get-Content  .\jt-snmpd-0.9.2-x64.msi.sha256
```

兩個值必須相同（不分大小寫）。**不相同就停下來** —— 不要安裝，回到發行頁面重新下載。

`.sha256` 請直接從 GitHub 的發行頁面取得，不要用鏡像站或別人轉寄的副本。
跟著被保護的檔案一起傳過來的雜湊，證明不了任何事。

---

## 3. 清除 Mark of the Web（網路標記）

透過瀏覽器下載的檔案，會在替代資料串流中帶一個區域標記，SmartScreen 就是被它觸發的。
確認雜湊無誤之後，把它清掉：

```powershell
# CLI
Unblock-File .\jt-snmpd-0.9.2-x64.msi
```

圖形介面的做法是在檔案上按右鍵 → **內容** → 勾選「一般」分頁最下方的**解除封鎖**。

把 MSI 複製到內部檔案共用資料夾再從那裡安裝，就完全不會有這個標記 ——
這也是 GPO 派送從來不會遇到它的原因。

---

## 4. WDAC 與 AppLocker：加入雜湊規則

在強制執行 Windows Defender 應用程式控制（WDAC）或 AppLocker 的環境中，
未簽章的檔案無法以發行者放行 —— 因為根本沒有發行者。
**檔案雜湊規則**是受支援的替代做法，而且它其實更嚴格：只放行你核准過的那些位元組，
其餘一律不放行。

需要涵蓋兩個檔案：MSI 本身，以及它安裝的服務執行檔。

```powershell
# CLI，規則必須涵蓋的路徑
.\jt-snmpd-0.9.2-x64.msi
C:\Program Files\jt-snmpd\jt-snmpd.exe
```

WDAC 可以從安裝後的資料夾產生原則片段，再併入既有原則：

```powershell
# CLI
New-CIPolicy -Level Hash -FilePath .\jt-snmpd.xml `
  -ScanPath 'C:\Program Files\jt-snmpd' -UserPEs
Merge-CIPolicy -PolicyPaths .\existing.xml,.\jt-snmpd.xml -OutputFilePath .\merged.xml
```

因為規則綁在雜湊上，**每次升級都必須重新產生**。請把這件事納入升級程序，
不要事後補做：併入的原則若還寫著上一版，新版服務會啟動不起來。

---

## 5. 自行簽章

如果貴單位有內部 PKI 並具備程式碼簽章範本 —— 政府機關與醫院相當常見 ——
用自己的憑證簽這個 MSI，比公開簽章更有用。這麼做會讓檔案符合你既有的
WDAC 與 AppLocker 發行者規則，而且 UAC 提示上出現的是貴單位的名稱，
對第一線人員而言，這比第三方的名字更有意義。

```powershell
# CLI
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /f your-code-signing.pfx /p <密碼> `
  .\jt-snmpd-0.9.2-x64.msi

signtool verify /pa /v .\jt-snmpd-0.9.2-x64.msi
```

請在簽章**之前**先核對公布的 SHA-256。簽章會覆寫檔案，之後公布的雜湊就不再適用，
請自行記錄簽章後檔案的雜湊供內部存查。

---

## 6. 如果 Defender 把檔案隔離

PyInstaller 產生的執行檔會週期性地被啟發式規則標記，而不是被病毒碼比對命中。
發生時：

1. 先驗證 SHA-256，確保你要辯護的是原本打算安裝的那個檔案。
2. 到
   [Microsoft Security Intelligence](https://www.microsoft.com/en-us/wdsi/filesubmission)
   以疑似誤判送件。通常數日內處理完成，修正會送達所有 Defender。
3. 過渡期間可對 `C:\Program Files\jt-snmpd\` 加入路徑排除項目 ——
   並在送件處理完成後移除，因為對一個目錄長期排除本身就是弱點。

不要以關閉即時保護作為因應方式。

---

## 7. 判斷這是否可以接受

有可能不可以接受，而那是合理的結論。可以一起衡量的幾點：

- **原始碼公開，且版本由原始碼建置而成。** 發行版本由 GitHub Actions 從一個
  帶標籤的 commit 建置，產出每個檔案的工作流程都留在 Actions 記錄中可供查閱。
- **雜湊鏈是完整的**：從發行頁面一路到安裝後的檔案，前提是雜湊要從發行頁面取得。
- **缺的是發行者身分**，而這件事再怎麼比對雜湊都補不上。如果貴單位的控制項要求
  具名且有憑證背書的發行者，§5 才是能滿足要求的途徑。
- **支援自行建置。** 若不願信任任何二進位檔，`packaging/build-msi.ps1`
  可在本機產生相同的 MSI。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [資安檢測工具鏈與報告](https://jasoncheng7115.github.io/jt-snmpd/security-scanning_zh-TW.html)
