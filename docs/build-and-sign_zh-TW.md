---
layout: default
title: 自行編譯打包與簽章
description: 從原始碼在自己的機器上建置 MSI，並用自己的憑證簽章
---

[← 回說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/) ·
[English](https://jasoncheng7115.github.io/jt-snmpd/build-and-sign.html) | **繁體中文**

# 自行編譯打包與簽章

發布的 MSI 目前未簽章。在有啟用 WDAC 或 AppLocker 強制模式的環境裡，這不是小麻煩而是一道牆：
沒有發行者的檔案，任何發行者規則都比對不到。
[程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)
那一頁講的是雜湊規則，做得到，但每次升級都要重做一次。

這一頁講的是另一條路，如果貴單位本來就有內部 PKI，這條路更好：
從公開的原始碼自行建置安裝檔，用自己的程式碼簽章憑證簽名，
以「自己簽發的檔案」派送出去。這樣有兩個好處：
現有的發行者規則直接適用，每個版本不必再做額外設定；
而且不再是信任別人編譯出來的執行檔，是信任自己讀得到的原始碼與自己跑的建置。

---

## 1. 建置機器需要什麼

**安裝檔把需要的東西全部包在裡面**，安裝過程不需要網路。**建置過程不是**：
它會下載 Python 套件與 WiX 工具組。請在連得到 PyPI 與 NuGet 的機器上建置，
或指向內部鏡像站，再把做好的 MSI 送到派送用的位置。

| 項目 | 版本 | 說明 |
|---|---|---|
| Windows x64 | 10 / 11 / Server 2019 以上 | 與發行版本的建置環境一致（`windows-latest`）|
| Python | **3.12** | 發行版本所用的版本。其他 3.x 也建得起來，但打包進去的 PyInstaller 執行環境就不是實測過的那一組 |
| Python 套件 | `pysnmp==7.1.29`、`pywin32`、`pyinstaller` | pysnmp 的版本釘在 `pyproject.toml`，不是隨手寫的：本專案預先算好的 BER 編碼是對著 pyasn1 的實際輸出比對的 |
| .NET SDK | 8 以上 | WiX v5 以 dotnet 全域工具的形式執行 |
| WiX | **5.x**，Util 與 UI 擴充套件為 **5.0.2** | 不指定版本時 `wix extension add` 會裝到 7.x，建置會以 `WIX6101` 失敗 |
| `signtool.exe` | Windows SDK | 隨 Windows SDK 的簽章元件安裝，Visual Studio 建置工具裡也有 |
| Git | 不限 | 選用，只用來把 commit 記進 `BUILDINFO.txt` |

```powershell
# CLI，建置機器上做一次即可
python -m pip install --upgrade pip
python -m pip install pysnmp==7.1.29 pywin32 pyinstaller pytest

dotnet tool install --global wix --version 5.*
$env:PATH = "$env:USERPROFILE\.dotnet\tools;$env:PATH"
wix extension add -g WixToolset.Util.wixext/5.0.2
wix extension add -g WixToolset.UI.wixext/5.0.2
wix extension list -g
```

---

## 2. 取得原始碼，切到標籤

請從標籤建置，不要從分支的最新 commit 建置。標籤才是發行版本當初建置的那個點，
檔名裡的版本號指的也是它。

```powershell
# CLI
git clone https://github.com/jasoncheng7115/jt-snmpd.git
cd jt-snmpd
git checkout v1.0.0
```

不要去改 `deploy/version.py`。執行檔、MSI 的 `ProductVersion`、檔名、
以及 `jtAgentVersion` 這個 OID，版本都是從那裡讀的；
而那個 OID 存在的理由正是回答「升級了幾百台，哪幾台沒升成功？」。
回報的版本與檔案本身對不上，這個問題就答不出來了。

### 先跑測試

大約二十秒，跟發版閘門跑的是同一套。環境有問題的話會在這裡出現，
而不是到客戶的機器上才出現。

```powershell
# CLI
python -m pytest tests\ -q
```

---

## 3. 建置執行檔

```powershell
# CLI，在 repo 根目錄執行
$py = (Get-Command python).Source
.\packaging\build-exe.ps1 -Python $py -Source deploy\jt_agent.py
```

產出是 `build\jt-snmpd\jt-snmpd.exe` 以及 `_internal\`，
也就是 PyInstaller 的 one-folder 執行環境。
腳本不會因為 PyInstaller 回傳 0 就當作成功：它會檢查**產出比原始碼新**，
並且實際執行 `jt-snmpd.exe --selftest`，不通過就不算數。
這兩道閘門的由來是本專案曾經三次做出「綠燈的建置」，
裡面裝的卻是上一版的程式碼，掛著新的版本號出貨。

---

## 4. 先簽執行檔，再打包

**順序很重要。** MSI 打包的是建置當下 `build\jt-snmpd\` 裡的東西。
打包完才簽，得到的是一個「已簽章的 MSI，裡面裝著未簽章的服務執行檔」，
而服務啟動時 WDAC 檢查的正是後者。

```powershell
# CLI，憑證放在電腦存放區，用主體名稱挑選
signtool sign /n "貴單位名稱" /fd SHA256 `
  /tr http://timestamp.digicert.com /td SHA256 `
  .\build\jt-snmpd\jt-snmpd.exe
```

若使用 PFX 檔，把 `/n "貴單位名稱"` 換成 `/f your-code-signing.pfx /p <密碼>`。
建議優先使用放在電腦存放區、HSM 或智慧卡裡的憑證，
而不是把 PFX 放在磁碟上、密碼打在命令列裡，後者會留在 PowerShell 的歷程記錄中。

### 如果 WDAC 也強制檢查 DLL

WDAC 的預設設定連 DLL 一起檢查，而 `_internal\` 裡放的是 CPython 執行環境、
pysnmp 與 pywin32 的擴充模組，都是 `.dll` 與 `.pyd`。
其中有些本來就由原發行者簽過章，例如 CPython 執行環境由 Python Software Foundation 簽署；
有些沒有，pywin32 的擴充模組就是。整包一起簽，答案才不會取決於是哪一種：

```powershell
# CLI
Get-ChildItem .\build\jt-snmpd -Recurse -Include *.exe,*.dll,*.pyd |
  ForEach-Object { signtool sign /n "貴單位名稱" /fd SHA256 `
      /tr http://timestamp.digicert.com /td SHA256 $_.FullName }
```

對已經有有效簽章的檔案重新簽署會直接取代原簽章，在這裡沒有副作用。

### 如果 PowerShell 執行原則是由群組原則設定的

安裝程式是以
`powershell.exe -ExecutionPolicy Bypass -File msi-configure.ps1`
執行設定腳本的。在一般機器上這個參數就夠了，但**由群組原則設定的執行原則優先於命令列參數**。
在強制 `AllSigned` 的網域裡，這個腳本會被拒絕執行，安裝會在自訂動作那一步失敗。
把它一起簽：

```powershell
# CLI
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Select-Object -First 1
Set-AuthenticodeSignature -FilePath .\packaging\msi-configure.ps1 `
  -Certificate $cert -TimestampServer http://timestamp.digicert.com
```

這會在檔案結尾附上一段簽章區塊，所以它的 SHA-256 就跟 repo 裡的不一樣了。
這是正常的，第 7 節會說明哪一部分還是應該對得起來。

### 關於時間戳記

`/tr` 會為簽章加上一個受信任的時間副署，憑證到期之後簽章還能繼續驗證，靠的就是它。
沒有時間戳記的話，憑證到期那一天，簽過的每一個檔案都會驗不過，
而那些機器可能已經在維護合約的第好幾年了。
如果建置機器連不到公開的時間戳記服務，請改用內部的 RFC 3161 服務，不要乾脆不做副署。

---

## 5. 建置 MSI

```powershell
# CLI
$env:PATH = "$env:USERPROFILE\.dotnet\tools;$env:PATH"
.\packaging\build-msi.ps1
```

版本號從 `deploy\version.py` 讀，不需要傳參數。
產出是 `dist\jt-snmpd-1.1.3-x64.msi` 與它的 `.sha256`，
另外在 `dist\releases\1.0.0\` 留一份，旁邊放 `BUILDINFO.txt`：

```
product   jt-snmpd
version   1.0.0
built     <這次建置的日期時間>
builder   <機器名稱> / <帳號>
commit    <所建標籤的短 commit>
sha256    <MSI 的雜湊>
files     <檔案數>
size      <大小>

-- source fingerprints (first 16 hex of SHA-256) --
configure <packaging\msi-configure.ps1 的 SHA-256 前 16 碼>
wxs       <packaging\wix\jt-snmpd.wxs 的 SHA-256 前 16 碼>
agent     <deploy\jt_agent.py 的 SHA-256 前 16 碼>
```

這個檔案要留著。一年半以後要回答「某個單位手上那個 MSI 裡的設定腳本是哪一版」，
靠的就是它。這個問題在本專案已經被問過一次：
當時同一台機器上有兩份 `msi-configure.ps1`，建置用的是沒人在改的那一份，
而建置本身完全正常。

---

## 6. 簽 MSI

```powershell
# CLI
signtool sign /n "貴單位名稱" /fd SHA256 `
  /tr http://timestamp.digicert.com /td SHA256 `
  .\dist\jt-snmpd-1.1.3-x64.msi
```

簽章會改寫檔案，所以第 5 節產生的 `.sha256` 已經不再對應它。
請重新產生一份，並且比照發布頁的做法，在內部散布這個值：

```powershell
# CLI
$msi = ".\dist\jt-snmpd-1.1.3-x64.msi"
"$((Get-FileHash $msi -Algorithm SHA256).Hash.ToLower())  $(Split-Path $msi -Leaf)" |
  Set-Content "$msi.sha256" -Encoding ascii
```

---

## 7. 驗證建置結果

```powershell
# CLI
signtool verify /pa /v .\dist\jt-snmpd-1.1.3-x64.msi
Get-AuthenticodeSignature .\build\jt-snmpd\jt-snmpd.exe | Format-List Status, SignerCertificate
```

`Status` 必須是 `Valid`。做完第 4 節之後執行檔卻顯示 `NotSigned`，
通常表示 MSI 是在簽章之前建的，打包進去的是未簽章的那一份。

要確認建出來的東西真的來自你讀過的原始碼，而不是中途換掉的東西，
把 `BUILDINFO.txt` 裡的指紋跟簽出來的工作目錄比對：

```powershell
# CLI
(Get-FileHash .\deploy\jt_agent.py            -Algorithm SHA256).Hash.Substring(0,16)
(Get-FileHash .\packaging\wix\jt-snmpd.wxs    -Algorithm SHA256).Hash.Substring(0,16)
```

`agent` 與 `wxs` 兩行必須相符。如果第 4 節簽了 `msi-configure.ps1`，
`configure` 那一行就不會相符，請在簽章之前先比對，或改為驗證它的簽章。

---

## 8. 派送憑證，然後改用發行者規則

簽章要能發揮作用，前提是用戶端信任那張憑證。用群組原則派送：

```
電腦設定 → Windows 設定 → 安全性設定 → 公開金鑰原則
  → 受信任的發行者                  <- 貴單位的程式碼簽章憑證
  → 受信任的根憑證授權單位          <- 若由內部 CA 簽發，一併匯入
  → 中繼憑證授權單位                <- 憑證鏈中的簽發 CA
```

派送之後：

- UAC 提示會在已驗證的橫幅上顯示貴單位名稱，而不是在黃色橫幅上顯示「不明」的發行者。
- SmartScreen 不會介入。
- **WDAC 與 AppLocker 的發行者規則可以直接適用**，這才是重點。
  發行者規則跨版本都有效；它取代掉的雜湊規則則是每出一個新版本就要重新產生一次，
  而合併後的原則裡若還寫著上一版，新版的服務會啟動不了。

```powershell
# CLI，以發行者而非雜湊建立 WDAC 規則
New-CIPolicy -Level Publisher -FilePath .\jt-snmpd.xml `
  -ScanPath 'C:\Program Files\jt-snmpd' -UserPEs
Merge-CIPolicy -PolicyPaths .\existing.xml,.\jt-snmpd.xml -OutputFilePath .\merged.xml
```

---

## 9. 哪些東西無法重現，直說

**自行建置的 MSI 不會與發布的那一個位元組相同，SHA-256 也不會一樣。**
這是工具鏈的特性，不代表哪裡出了問題：

- PyInstaller 會把建置時間寫進它產生的 PE 標頭，所以同樣的原始碼建兩次就不一樣。
- Windows Installer 每次建置都會蓋上新的 package code，
  product code 也是由 WiX 產生，並沒有寫死。
- 簽章本身就會改寫執行檔與 MSI。

所以「跟發布版本比對雜湊」這條路對你是不存在的，也不該有人說得好像存在。
可以驗證的是輸入而不是輸出：原始碼是公開的、切在標籤上，
測試就是發版閘門，`BUILDINFO.txt` 又對三個決定安裝行為的檔案留了指紋。
如果貴單位的要求裡包含位元組層級的可重現建置，
請把它當成尚未解決的項目看待，不要當成已經解決。

---

## 10. 與官方發行版本並存

自行建置的版本與官方版本共用同一個 **UpgradeCode**，這是刻意的：
兩邊都可以就地升級對方，並把 `%ProgramData%\jt-snmpd\` 一起帶過去，
其中包含 `state\index-map.json`。這個檔案掉了，
LibreNMS 會刪掉所有連接埠重新探索，歷史圖表跟著一起沒。

由此帶來的後果值得寫清楚：如果有人在跑著自簽版本的機器上安裝官方的未簽章 MSI，
它會順利升級，並且悄悄地把一個已簽章的安裝換成未簽章的。
兩個習慣可以避免這件事：

1. **每個要採用的版本都重新建置並重新簽章。** 追蹤發行版本、建置標籤、簽章、
   放到自己的檔案共享。只要跳過一次，「這次就先用官方的 MSI 吧」的壓力就會把前面的工都抵銷掉。
2. **沿用上游的版本號。** 不要自己重新編號。
   `jtAgentVersion` 回報的就是它，全網段走一次 SNMP 就知道哪一台在哪個版本，
   這件事的價值高過一套自訂的編號規則。

---

## 相關文件

- [說明文件首頁](https://jasoncheng7115.github.io/jt-snmpd/)
- [程式碼簽章](https://jasoncheng7115.github.io/jt-snmpd/code-signing_zh-TW.html)
- [命名與路徑](https://jasoncheng7115.github.io/jt-snmpd/naming-and-paths_zh-TW.html)
- [安全性評估](https://jasoncheng7115.github.io/jt-snmpd/attack-surface_zh-TW.html)
- [發版檢查清單](https://jasoncheng7115.github.io/jt-snmpd/release-checklist_zh-TW.html)
