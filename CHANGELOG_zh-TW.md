# 變更記錄

本專案的所有重大變更都記錄在此檔案。

格式依循 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，
版本號依循 [語意化版本](https://semver.org/lang/zh-TW/)。

English version: [CHANGELOG.md](CHANGELOG.md)

---

## [0.9.3] - 2026-08-24

### 修正

- **圖形介面安裝完全裝不起來。** 點兩下 MSI 之後，Welcome 頁面直接跳出
  「必須指定管理網段」而且沒有任何往下的路，但那個要求填管理網段的頁面
  就在兩個按鍵之後。原因是 Windows Installer 的一個順序特性：
  `LaunchConditions` 在 InstallUISequence 的最前面執行，早於任何對話框出現，
  所以一個依賴「精靈之後才會收集到的屬性」的啟動條件，在精靈安裝裡永遠不可能成立。
  條件現在對 `UILevel > 4` 豁免，那是完整精靈、也是唯一有頁面可以問的層級；
  其餘較安靜的層級（`/qn`、`/qb`、`/qr`）照樣中止，而設定頁本身也會擋住
  沒填就按下一步。

  這個問題現有測試一個都抓不到：WiX 原始碼合法、建置成功、`/qn` 安裝正常，
  而生命週期測試從頭到尾都用 `msiexec /qn`。所有閘門全綠，
  但操作人員最先用到的那條路徑是全壞的。
  `tests/test_msi_ui_gating.py` 現在把修法的形狀釘住，
  包含「設定頁沒填值就不能前進」與「設定腳本仍然 fail closed」。
- **`docs/naming-and-paths.md` 寫了三個不存在的檔名**：
  `config.yaml`（實際是 `config.json`）、`engine-state.json`（`engine.json`）、
  `ms-snmp-migration.json`（`ms-snmp-restore.json`）。
  現在的內容是磁碟上實際有的東西，規劃中但尚未存在的項目已標註。

---

## [0.9.4] - 2026-08-24

### 修正

- **設定頁面從來沒出現過。** 0.9.3 修好啟動條件之後精靈總算跑得動，
  卻從「Destination Folder」直接跳到「Ready to install」：那個要填管理網段與
  community 的頁面被完全跳過，安裝於是在設定階段失敗，因為根本沒有東西可以設定。
  `WixUI_InstallDir` 本身就在同一個按鈕上以 Order 4 發佈
  `NewDialog=VerifyReadyDlg`，而當多個 NewDialog 事件同時成立時，
  最後被處理的那個決定精靈往哪走。我們的路由發佈在 Order 3，每次都被蓋掉。
  現在改為 Order 5，並把內建那列的路徑驗證條件一起帶上，
  無效路徑仍然會進 InvalidDirDlg。這是直接從建出來的 MSI 讀 ControlEvent 表確認的，
  不是看原始碼推論的。
- **授權頁面顯示的是 Lorem ipsum 假文。** 沒有設定 `WixUILicenseRtf` 時
  WiX 會塞一份佔位文件，而佔位的 EULA 不是外觀問題：那是一份被當成使用條款呈現、
  內容卻什麼都沒說的文件，出現在一個實際採用 GPL-3.0-or-later 的軟體安裝程式裡。
  現在顯示的是本專案自己的 `LICENSE`，由 `packaging/make-ui-assets.py` 產生，
  兩者不會各自漂移。
- **精靈穿的是 WiX 的預設美術**，包括每一頁右上角那個紅色圖案。
  橫幅與側欄現在由 `docs/brand/icon-512.png` 以專案自己的色彩產生。
- **設定頁 Next 按鈕上的 `NewDialog` 發佈在 `SpawnDialog` 之前。**
  Windows Installer 會丟棄同一個控制項上 NewDialog 之後的所有事件，
  所以「請輸入管理網段」的提示能運作，只是因為兩個條件剛好互斥。
  現在提示先發佈、轉頁最後發佈。

- **精靈美術檔在修好任何東西之前先弄壞了建置。** `WixVariable` 的路徑是相對於
  工作目錄解析的，不是相對於 `.wxs`，所以裸檔名以三個 WIX0103
  「找不到 Binary 檔案」失敗。建置腳本現在以傳遞圖示的同一種方式傳入該目錄。

### 新增

- **每次推送都建 MSI，不再只有打標籤時才建。** 這個缺口正是「推了一個建不起來的
  標籤」的原因：`tests.yml` 只在 Linux 上跑，任何改動的第一次 Windows 建置
  都發生在發版當下。新的工作在 `windows-latest` 上建執行檔與 MSI，
  然後檢查三件原始碼看不出來的事：已提交的精靈美術檔是否仍與產生器的輸出一致、
  建出來的 MSI 是否真的會經過設定頁（直接讀它的 ControlEvent 表）、
  以及授權頁面是不是 WiX 的佔位文字。MSI 會保留 14 天供查驗。

`tests/test_msi_ui_gating.py` 四項全部涵蓋，每一條斷言都做過突變驗證：
把舊值放回去就會紅。

---

## [0.9.5] - 2026-08-24

### 修正

- **「保留內建 SNMP 服務」核取方塊與自己的標示相反。** Windows Installer 只要屬性
  非空就把核取方塊畫成已勾選，而該屬性的預設值是 `"0"`，那是一個非空字串。
  方塊因此顯示為**已勾選**，旁邊的標示說服務會被保留，但安裝程式接著就把它停用了。
  這是直接從 0.9.4 的 MSI 讀 Property 表確認的，不是看原始碼推論的。

  更糟的是第二層問題。取消勾選會把屬性清成 `""`，而只有打勾才會寫入 `"1"`，
  所以能到達的狀態是 `"0"`（顯示已勾、停用）、`""`（未勾、停用）、
  `"1"`（重新勾選、保留）。照標示使用這個方塊根本無法保留服務，
  必須先取消勾選再勾回來。屬性現在預設為空，未勾即停用、已勾即保留，
  而無訊息安裝的 `KEEPMSSNMP=1` 照樣有效。
- **標題列在精靈中途換了名字。** 我們自己那兩頁寫「jt-snmpd」，
  而 WixUI 的每一頁都寫「jt-snmpd Setup」，看起來像換了另一個程式接手。

### 新增

- **建置現在會檢查產物，不只是原始碼。** 最近三個安裝程式缺陷裡有兩個在 WiX
  原始碼上看不出來，但在建出來的 MSI 表格裡一目了然。CI 現在會讀它們：
  讀 ControlEvent 表確認精靈真的會經過設定頁、而且我們的路由順序壓過 WixUI 的；
  讀 Property 表確認那個選用的核取方塊預設未勾選。

---

## [0.9.6] - 2026-08-25

### 變更

- **現在全部都叫 `jt-snmpd`。** 產品名稱、安裝精靈標題、安裝目錄、資料目錄、
  服務顯示名稱與防火牆規則，原本寫的是「JT SNMP Agent」或「JT-SNMP」，
  而專案、repo 與服務名稱寫的是 `jt-snmpd`。把顯示名稱與技術識別分開是 Windows
  的常見慣例，但在這裡只造成誤會：使用者在 GitHub 上找到的是 jt-snmpd，
  在「應用程式與功能」看到的是另一個名字，磁碟上還有第三種寫法。

  `C:\Program Files\JT SNMP Agent\` 改為 `C:\Program Files\jt-snmpd\`，
  順帶消除路徑中的空白，也就消除了 unquoted service path 這一整類稽核發現。

  `C:\ProgramData\JT-SNMP\` 改為 `C:\ProgramData\jt-snmpd\`，
  而且**安裝程式會把既有目錄搬過去**。這一步不能省：`state\index-map.json`
  裡是 ifIndex 的配發結果，弄丟它，LibreNMS 會刪掉每一個 port 重新探索，
  歷史 RRD 一起失去對應；`state\ms-snmp-restore.json` 則是「內建 SNMP 服務
  被停用前長什麼樣」的唯一紀錄。搬移失敗時會退而複製並明白說出來，
  因為多一份目錄可以救，少一份不行。清除移除現在會清掉兩個位置，
  否則下一次安裝會把舊的又搬回來。

  `tests/test_data_dir_migration.py` 守著這件事，而且它立刻就發揮作用了：
  一次全庫取代把搬移的**來源路徑**也改掉了，讓它指向自己的目的地。
  那樣會照跑、找不到東西、回報成功，然後每一台升級過的機器都從空的
  狀態目錄開始。

### 修正

- **設定頁的說明文字撞到橫幅圖示。** 那個控制項比它能用的寬度多了 285 單位：
  橫幅圖是 370 個對話框單位寬，圖示大約佔掉最後 40 個，所以文字最多只能到
  325 單位左右，而它被允許延伸到 355。這是從算繪出來的對話框量的，
  文字距離圖示只剩 4 px，看起來就像跑到圖示底下。說明文字現在縮短也變窄了，
  而且 `tests/test_msi_ui_gating.py` 會讓任何碰到圖示的橫幅文字控制項失敗。

---

## [0.9.7] - 2026-08-25

### 修正

- **資料目錄的搬移永遠不會執行。** 0.9.6 為了配合改名而搬移資料目錄，
  並以「目的地不存在」作為條件。那個條件永遠不成立：這支腳本把自己的記錄檔
  寫在目的地底下，所以第一次 `Log` 呼叫就把它建出來了，早於檢查。
  每一次升級都跳過搬移、把舊目錄留在原地，然後 agent 從空的狀態目錄開始，
  ifIndex 對照表就此遺失，而那正是這個搬移要防的失效。

  這是用 RDP 跑一次真實升級發現的，不是看程式碼看出來的。記錄檔寫得很清楚：
  `[!] C:\ProgramData\JT-SNMP still exists alongside C:\ProgramData\jt-snmpd`，
  接著就是一份日期是今天的全新 `index-map.json`。

  搬移現在**逐項判斷**而不是整個目錄判斷，順帶讓它可以重複執行：
  做到一半的搬移下次會接著完成，而不是因為「看起來做過了」被跳過。
  目的地已存在的項目一律不覆蓋，因為重裝時那是使用中的資料。
  先前的記錄檔改放到 `logs\pre-0.9.6\` 保留而不是丟掉，
  舊目錄只有在裡面沒有任何值得保留的東西之後才移除。
  任何一項搬不過去就中止安裝，因為半份狀態目錄比一次會倒回的失敗安裝更糟。

- **兩個測試一直在檢查檔案的錯誤區段，而且因此通過。**
  `test_default_uninstall_keeps_data_dir` 錨定在一行早已被翻成英文的中文記錄字串上，
  `find` 回傳 -1，切片一路吃到檔尾，等於在對整個腳本做斷言；
  它還把 `$DATA_DIR` 當子字串比對，因而命中 `$DATA_DIR_OLD`。
  `test_installer_writes_json_without_a_bom` 則以「第一次提到 `config.json`」定位寫入處，
  而新的搬移區塊剛好排在它前面。兩者現在都錨定在真正要測的東西上，
  錨點一移動就會明白失敗。

---

## [未發行]

### 新增

- **專案網站加上圖形安裝的截圖**，五頁精靈全部呈現。設定頁上的值是文件用範例
  （`10.0.0.0/24`、`your-community`）：原始擷取用的是實際的管理網段與一個
  臨時 community，那兩個輸入值已被替換。只改動兩個欄位框內的文字，
  邊框、控制項狀態，以及任何描述安裝程式行為的部分都未更動。

- **一份手動移除文件**（`docs/manual-removal_zh-TW.md`），供安裝程式走不完時使用：
  安裝倒回、解除安裝回報成功但服務還在跑，或是「應用程式與功能」裡已經看不到
  這個產品但 UDP/161 還在回應。每一步都是安裝程式某個動作的手動版本，
  順序也刻意安排成能把內建 SNMP Service 還原回去：那份「原本是什麼狀態」的記錄
  就放在資料目錄裡，先刪目錄等於把唯一一份記錄丟掉。

- **程式碼簽章文件補上「手動信任」章節**，涵蓋單機做法（SmartScreen 的
  「仍要執行」、`Unblock-File`、在 Defender 中允許被隔離的檔案）與整個網域的做法
  （從內部共用資料夾派送，或用自己的憑證簽章後以群組原則推送到受信任的發行者，
  一次解決其餘所有提示）。
- **預設關閉的 OID 送出去到底有沒有用**，這次去查 LibreNMS 原始碼而不是用猜的。
  四類裡有三類（已安裝軟體、執行中處理程序、連線表）**在 LibreNMS 根本沒有取用端**，
  送出 2,727 個 OID 的弱點與連線資料，換不到任何一個頁面或圖表。
  只有 ARP 有人取用（`LibreNMS/Modules/ArpTable.php` → `ipv4_mac` → ARP 與 FDB 搜尋），
  而它早就實作好，在 `enable_arp_table` 後面。
- **對照文件補上 System 圖表與磁碟區標籤編碼**。Windows 只有三張 System 圖，
  Linux 有八張，差的五張來自 UCD-SNMP-MIB，而內建服務沒有實作那個 MIB。
  中文磁碟區標籤不是錦上添花而是真的會炸：pysnmp 遇到非 ASCII 會拋
  `PyAsn1UnicodeEncodeError`，整份快照建不起來。

### 變更

- **README 的安裝段還停在 MSI 之前的版本。** 它以 `install.ps1` 和一個發行版本
  早已不再提供的 ZIP 壓縮檔開頭，狀態表甚至把「供 GPO / Intune / SCCM 使用的 MSI」
  寫成「未實作」，而它從 0.9.0 起就已發版並通過生命週期驗證。
  兩份 README 現在都以下載 MSI 開頭，並涵蓋圖形介面、命令列、GPO 派送與解除安裝。
- **VACM 補上解釋**，不再只是狀態表裡的一個縮寫：它是 RFC 3415 的檢視型存取控制，
  用來限制某一組憑證看得到 OID 樹的哪幾段。

- **程式碼簽章是規劃中，不是放棄。** 日後規劃透過開源專案的憑證方案取得簽章；
  在那之前，文件寫清楚會看到什麼，以及如何安全地通過。
- **公開的檔案不再引用內部文件。** 所有指向內部規格書與內部工作筆記的引用，
  一律改成把內容直接寫出來。讀者跟不過去的引用，比沒有引用更糟。
  閘門 0 的查證報告不再公開，因為它整份是照著內部規格書的章節編號寫的。
- **`msiexec` 指令改成一行。** 那道指令沒幾個字，接續符號純粹是雜訊。
- **SMART 截圖裡的 Proxmox 麵包屑已移除。** 那是 LibreNMS 在對照組主機上探索到的
  不相干應用程式，放在 SMART 對照旁邊會被當成對照的一部分。

### 修正

- **程式碼簽章文件寫錯了安裝目錄。** 它寫成 `C:\Program Files\jt-snmpd\`，
  但 MSI 實際安裝到 `C:\Program Files\jt-snmpd\`，
  導致 WDAC 掃描路徑與 Defender 排除路徑兩處都是錯的。

- **`prepare-public-repo.py` 只保留 `dist/` 與 `build/` 裡的 `README.md`**，
  兩個目錄的繁中版 README 因此從來沒被發佈過。
- **更多用語修正**：權限縮減（非剝除）、受阻（非阻塞）、處理程序（非行程）、
  安裝檔（非安裝包）、溫度區（非熱區，並補上它到底是什麼的說明，
  因為直譯完全沒解釋到）、網頁標記（Mark of the Web）。
  絕對化的說法（絕不、永不）改為平實描述，中文也不再使用破折號。

### 新增

- **雙語說明文件。** 每一份公開文件現在都有英文（`docs/<名稱>.md`）與
  繁體中文（`docs/<名稱>_zh-TW.md`）兩版，每頁最上方有語言切換與回到說明文件
  首頁的連結，最下方有相關文件清單。在此之前，英文版 README 與英文版網站指向的
  文件只有中文，對大多數讀者而言等於是斷路。
- **`docs/code-signing_zh-TW.md`**（以及英文版），說明未簽章的安裝檔在安裝時
  實際會遇到什麼，SmartScreen 提示、UAC 對話框中顯示為「不明」的發行者、
  GPO 派送會看到什麼（什麼都不會看到）、WDAC 與 AppLocker 的行為，
  以及各自的處理方式：核對公布的 SHA-256、清除網頁標記、
  加入 WDAC 雜湊規則，以及自行以憑證簽章。
- **專案網站的安裝段加上下載連結與 GPO 說明。** 原本頁面上有一道 `msiexec`
  指令，卻沒有任何地方可以取得 MSI，也沒有說明同一道指令就是群組原則
  軟體派送所使用的形式。

### 變更

- **確定不申請程式碼簽章憑證。** 原本各處寫著「SignPath Foundation 申請中」，
  現在一律明確寫出安裝檔未簽章、這是長期狀態，以及該去哪裡看處理方式。
  沒有期限的「即將支援」比明確的「不做」更糟：它會讓部署者去等一個不會來的東西。
- **發行說明改為英文為主、中文輔助**，且每一行都是完整句子而非硬換行的片段，
  GitHub 會把發行說明當 Markdown 算繪，句中的換行就成了頁面上的換行。
- **「攻擊面分析」更名為「安全性評估」**，這才是這份文件的內容：
  實測的暴露面、緩解措施，以及未緩解項目的誠實清單。
- **量測資訊從引言區塊改為表格。** 引言區塊中的連續行在算繪時會併成同一段，
  三筆獨立資訊因此擠成一行，完全無法閱讀。
- `.github/workflows/release.yml` 轉為英文，接續 `deploy/` 與 `packaging/`
  已完成的註解與識別字轉換。

### 修正

- **非台灣用語**，依微軟正體中文用語更正：filter driver 應為
  篩選器驅動程式而非過濾驅動、tunnel 為通道、instance 為執行個體。
  詞與詞之間的全形斜線一律改為前後加半形空格的半形斜線，
  這才是台灣技術文件的寫法。

---

## [0.9.2] - 2026-08-24

### 新增

- **圖形安裝介面。** 雙擊 MSI 原本會直接無訊息安裝，使用者沒有機會填入管理網段
  與 community，安裝就以「無法決定 community」失敗。現在安裝路徑與確認畫面之間
  多了一頁設定，而且管理網段沒填就無法通過，空白代表 agent 只回應本機查詢，
  等於裝了卻沒有在監控。無訊息安裝不受影響：`/qn` 下 UI 序列不會執行，
  兩條路徑讀的是同一組屬性。

### 修正

- **「加入或移除程式」沒有圖示。** `ARPPRODUCTICON` 指向 Icon 表是文件寫的做法，
  但在這裡沒有作用，屬性在、Icon 表項目在，登錄檔的值就是空的；
  把 .ico 重新編碼成不含 PNG 壓縮項目也沒有差別。現在改為直接寫入
  `DisplayIcon` 指向已安裝的執行檔，而那個執行檔本來就內嵌了圖示。

- **`build-msi.ps1` 可能打包到過時的程式碼。** 它直接封裝 `build/` 裡現有的東西，
  不檢查新舊，於是一個沒有重新建置的修正，被包進帶著新版本號、新 SHA-256、
  獨立歸檔目錄的 MSI 裡。它也從來沒有檢查 WiX 的結束碼，因此建置失敗時會取到
  上一個 MSI 並以舊版本號回報成功。兩者現在都是閘門，
  並由 `tests/test_build_gates.py` 守著，這已經是同一類問題第三次發生：
  舊東西掛著新標籤出貨。

### 變更

- 在 LibreNMS 啟用 SMART 的說明改以網頁介面為主，齒輪圖示 → Settings →
  Discovery → Discovery Modules → `applications`，`lnms` 指令列為次要方法。

## [0.9.1] - 2026-08-24

### 修正

- **agent 從來沒有讀取過自己的設定檔。** 安裝程式收集了 community 與管理網段、
  驗證過、寫進 `config.json`。而 agent 宣告的 `CFG_PATH` 指向 `config.yaml`
，不同的檔案，而且兩個它都沒有打開過。每一次安裝跑的都是原始碼裡的預設值。

  那組預設值是 `community="mon2"` 與 `allowed_networks=("192.168.1.0/24",)`，
  正好就是開發實驗室用的值，這也是為什麼數個月的測試都沒發現。換成別的值安裝，
  loopback 健康檢查會用操作者的 community 查詢、agent 卻在另一個上面回應，
  檢查逾時，MSI 以 1603 回滾整筆交易。失敗是徹底的，卻仍然看不見，
  因為唯一能成功的那組設定，正是唯一被試過的那組。

  agent 現在在啟動時載入 `config.json`，而且是在進入點讀取任何設定**之前**：
  晚一步就等於同一個 bug，因為那些值是以參數傳入的，早就綁定完成了。
  兩個預設值都改為空：沒有 community 就拒絕服務而不是自己編一個，
  讀檔用 `utf-8-sig`，因為 PowerShell 與記事本都會寫 BOM。

  設定現在可以照文件一直以來暗示的方式修改：編輯
  `C:\ProgramData\jt-snmpd\config.json`，重新啟動服務。

- **未設定來源 ACL 時等於放行所有來源。** 前置閘門把空的網段清單當成「不過濾」。
  在安裝程式是設定檔唯一作者的時候，那個狀態到不了；但手動編輯設定檔現在是
  受支援的流程，一個被清空的清單會無聲地把 agent 暴露給整個網路。
  現在改為除 loopback 外一律拒絕，監控會明顯停掉，而不是安靜地過度分享。
  要刻意服務所有來源，請明確寫出 `0.0.0.0/0`。

### 新增

- **專案圖示**，一棵 OID 樹，因為物件識別碼的階層正是 SNMP 的本質。
  以單一線寬繪製，讓它在瀏覽器分頁與 Windows 服務清單的 16 px 下仍然可辨。
  它取代了原本的空白佔位圖示，那讓「加入或移除程式」裡的項目看起來像
  安裝到一半的東西。

- **持續整合**，測試在 Linux 與 Windows 上執行；打上標籤即建置 MSI 並發佈。
  失敗會以工作流程註記呈現，因為 GitHub 的執行日誌需要認證才讀得到，
  而「exit code 1」不構成診斷。Linux 端會安裝 net-snmp，並在之後確認
  協定正確性測試真的跑過，避免它們悄悄跳過。

## [0.9.0] - 2026-08-24

### 新增

- **磁碟健康狀態顯示於 LibreNMS**，SMART 應用程式現在會在每顆磁碟旁顯示
  `PhysicalDrive0 (OK)` / `(FAIL)` / `(Overheating)`。判定來自 ATA
  `SMART RETURN STATUS`（0xDA），也就是 `smartctl -H` 顯示的那一行，
  或 NVMe 的 critical warning 位元圖，**不從屬性推導**：重新配置磁區為 0
  不代表健康，韌體可能因為別的屬性跌破門檻而已在預測故障；反過來說少量
  重新配置磁區在某些型號上完全正常。磁碟完全不回答時（USB 橋接器通常不轉送
  SMART 命令）就不輸出該鍵，LibreNMS 顯示空白，而不是一個捏造的 `(OK)`。

- **`jtDiskHealthTable`** 置於私有 OID 子樹，每顆磁碟一個狀態值
  （ok / warning / critical / unknown），供需要直接告警的使用者取用。
  要注意的是，LibreNMS **裝置概觀頁**上的綠 / 紅燈必須在 LibreNMS 伺服器端
  新增探索定義才做得到，而本專案刻意不要求修改伺服器。

- **公開前的個資檢查工具**，`tools/check-privacy.py` 掃描的正是 git 會推上去
  的那些檔案，檢查金鑰、密碼、community 字串、MAC 位址、位址與序號；
  `docs/release-checklist_zh-TW.md` 記錄完整流程。圖片改用「人工審閱 + 雜湊」而非
  比對規則，因為正規表示式讀不到像素：第一批 README 截圖帶出了四組 MAC 位址
  與六個鄰居裝置名稱，那等於一張內網拓撲圖。

- **磁碟 SMART 透過 SNMP 提供（`NET-SNMP-EXTEND-MIB`）**，LibreNMS 讀 SMART 走它的
  `smart` 應用程式，而那個應用程式**完全透過 SNMP** 取得
  （`snmp_get nsExtendOutputFull."smart"`）。被監控端不需要安裝 LibreNMS agent、
  不需要 smartctl、不需要任何腳本。jt-snmpd 本來就以 IOCTL 直接讀到 SMART 屬性，
  現在把它序列化成該應用程式期望的 JSON。已在 Dell Latitude E5270 實機驗證：
  重新配置磁區 0、磨損平衡 4、UDMA CRC 錯誤 0、溫度 33°C、通電 491 小時，
  確實寫入 `app-smart-*.rrd`。

  內容是 `base64(gzip(json))`，這是 `json_app_get()` 明確支援的形式，而且是必要的：
  回應上限 1400 位元組且不分片，未壓縮的 JSON 在兩顆磁碟時就會超出。
  沒量到的屬性一律 `null`，不填 `0`；在「重新配置磁區」欄位填一個假的 0，
  讀起來的意思是「這顆磁碟很健康」。

  **這需要在 LibreNMS 啟用 `discovery_modules.applications`**，它預設是 `false`。
  沒啟用的話，extend 資料照樣供應，但不會有人來取。

- **磁碟最高溫度**（`max_temp`），LibreNMS 的 SMART 應用程式無論有沒有資料都會
  渲染一張「Max Temp(C)」面板，因此少了這個鍵，每一套安裝都會看到一張破圖。
  Windows 的儲存 API 給的是門檻值（warning、critical），不是「這輩子最高溫」，
  拿門檻值去填那條線是標錯標籤；因此 jt-snmpd 改記錄**自己實際觀測到**的最高溫，
  跨重新啟動保存，且只在最高溫真的上升時才寫檔，快照每五秒重建一次，
  每次都寫會是一天一萬七千次不必要的磁碟寫入。

- **對照截圖**置於 `docs/images/`，取自正式 LibreNMS，英文與台灣繁體中文各一套，
  皆為淺色主題：感測器、SMART、連接埠、記憶體，每組都以同一個頁面對照
  「使用內建 SNMP Service 的 Windows 10 主機」與「使用 jt-snmpd 的主機」。

- **ACPI 溫度區溫度**，不需核心驅動的系統 / 主機板溫度，以
  `advapi32!WmiOpenBlock` + `WmiQueryAllDataW`（WMI 資料區塊 API，不是 WMI COM，
  也不開子行程）讀取。實體機實測 25°C，臨界跳脫點 107°C；虛擬機回
  `ERROR_WMI_GUID_NOT_FOUND`，該感測器直接不出現。

  CPU 封裝溫度仍然做不到，而且會一直做不到：它需要存取 MSR，而那需要核心驅動。
  業界慣用的那個驅動（WinRing0）已列入 Microsoft 的易受攻擊驅動封鎖清單，
  在 HVCI/WDAC 下載不進去，那正是我們客戶的環境設定。

- **CPU 頻率感測器**（`entPhySensorType = hertz`，`mega` 刻度），只輸出一筆而非
  每個邏輯處理器一筆，因為 `CallNtPowerInformation` 回報的是封裝層級的 P-state，
  各核心數值相同。要注意 LibreNMS 目前會丟棄這類感測器：
  `entity-sensor.inc.php` 把 `hertz` 對應到類別 `freq`，但
  `LibreNMS/Enum/Sensor.php` 中合法的類別是 `frequency`。
  同一個缺陷也影響 `cisco-entity-sensor.inc.php` 與 `openbsd.inc.php`。
  我們的 OID 依 RFC 3433 是正確的，`snmpwalk` 查得到；等 LibreNMS 修正對照表後
  圖表就會出現。

- **電池狀態**放在私有 OID 子樹（電量百分比、市電狀態、預估可用時間），
  來源 `GetSystemPowerStatus`。刻意只放私有：LibreNMS 的 entity-sensor 對照表
  沒有 charge 或 percent，送成標準感測器不會有任何結果。

- **`SNMP-FRAMEWORK-MIB` engine 群組**（`snmpEngineID`、`snmpEngineBoots`、
  `snmpEngineTime`、`snmpEngineMaxMessageSize`），這修掉了一個原本會在每台主機
  開機滿 497 天後發出的假「Device rebooted」告警。`sysUpTime` 的型別是
  `TimeTicks`，在 2^32 個百分之一秒 ≈ 497.1 天必然回捲；這是 RFC 3418 規定的，
  Windows 內建 SNMP Service 一樣會回捲。能修的是後果：LibreNMS 取
  `max(sysUpTime/100, snmpEngineTime, hrSystemUptime/100)`，而 `windows.yaml`
  只停用了 `hrSystemUptime`。`snmpEngineTime` 以秒計、上限 2147483647
  （約 68 年），回捲發生後最大值仍持續上升，重開機判斷因此不會成立。

- **記錄檔輪替與 Windows 事件檢視器整合。** agent 的記錄檔原本沒有大小上限；
  快照重建持續失敗時每五秒一行，一天一萬七千行。數百台跑上數年，
  監控代理程式把它所監控主機的系統碟寫滿，是最不能接受的失效方式。
  錯誤現在同時進事件檢視器，現場人員第一個看的是那裡，
  而遠端診斷數百台時 `Get-WinEvent` 可以集中撈。

- **完整生命週期測試**（`tests/lifecycle.ps1`），安裝、升級、移除、重裝、
  PURGE 移除，共 40 項斷言，在實機上以打包好的 MSI 執行。

### 修正

- **agent 執行緒死亡後，服務仍回報 `Running`。** `SvcDoRun` 只等停止事件，
  因此啟動階段的任何失敗，綁定失敗、MIB 載入失敗、快照建置失敗，都會讓
  服務控制管理員回報一個健康的服務，而實際上沒有任何監聽器。
  服務控制管理員說 `Running`、監控系統說逾時，是現場最難查的狀態；
  而且這也代表已設定的三段式自動復原永遠不會觸發，因為程序根本沒有結束。

- **升級之後再移除，內建 SNMP Service 會永遠停在停用狀態。** 設定腳本每次執行
  都重讀內建服務的當下狀態並覆寫還原記錄。第一次安裝時讀到的是真實原狀；
  升級時該服務早已被上一次安裝停用，於是 `Disabled` 被當成原始設定寫回，
  解除安裝端的 `$orig -ne 'Disabled'` 判斷從此不會成立。
  安裝 → 移除可以正確還原，安裝 → 升級 → 移除不行，而升級正是這個產品的常態操作。

- **`PURGE=1` 沒有清掉資料目錄。** 自訂動作的記錄檔就放在它要刪除的目錄裡，
  刪完之後的兩行收尾訊息又把 `logs\` 重建了回來。刪除失敗也被
  `-ErrorAction SilentlyContinue` 吞掉並回報成功。

- **內建 SNMP 的停用是「假設」而非「確認」。** 群組原則或第三方管控可能擋下它；
  現在安裝程序會確認該服務確實已停止且已停用，不符就帶著說明失敗，
  而不是繼續走到一個看不出原因的健康檢查逾時。

- **`CallNtPowerInformation` 的緩衝區大小。** 原型用 `os.cpu_count()`，
  而那只反映呼叫端所屬的處理器群組；在超過 64 個邏輯處理器的機器上，
  核心會寫超出我們配置的範圍。現在改以
  `GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)` 決定大小。
  ctypes 正是 Python 記憶體安全性不再適用之處。

### 變更

- 磁碟感測器標籤保留 Windows 原生名稱（`PhysicalDrive0 Temp` 而非 `Drive0 Temp`）。
- SMART 屬性依讀到的 ID 完整保留，不再只留有名稱對照的那些。
  LibreNMS 需要 10、183、184、188、196、199，這幾個原本都沒有名稱。
- 所有來自韌體的緩衝區改以防禦性方式解析，並把解析與採集拆成純函式，
  以便在 Linux 上用敵意輸入測試。WMI 資料區塊裡的長度與偏移量都取自區塊自身；
  一個不合理的執行個體數，就是對一台我們承諾不會拖慢的主機發動自我阻斷服務。


### 新增

- **UCD-SNMP-MIB `systemStats`**，這是 LibreNMS 的 System 圖表群組真正的來源。
  Windows 主機先前只有三張圖（Processes、Users、Uptime），因為那些來自
  HOST-RESOURCES；Linux 裝置上其餘的 Detailed Processor Usage、Context Switches、
  Interrupts、I/O、Swap I/O 全部來自 UCD-SNMP-MIB。現以
  `NtQuerySystemInformation`（`SystemPerformanceInformation` 與逐 CPU 時間）供應，
  新增五張圖表

- **`hrFSTable`、`hrPartitionTable` 與 `ipRouteTable`**，在製作與內建 SNMP Service
  的對照表時發現這三張表我們真的沒有。檔案系統與分割來自 `GetVolumeInformationW`，
  路由來自 `GetIpForwardTable2`。它們都沒有「軟體清單 / 連線表」那類資訊揭露顧慮，
  故預設輸出
- **對照文件**（`docs/comparison-vs-builtin-snmp_zh-TW.md`）逐表量測 jt-snmpd 與
  仍使用內建 SNMP Service 的 Windows 10 主機，並為每一處「我們回報得更少」
  給出理由

- **MSI 安裝檔（WiX v5）**，這是群組原則派送的前提，GPO 軟體安裝只接受 MSI。
  已在 Windows 11 端對端驗證：無訊息安裝（`msiexec /qn`）、
  **直接安裝新版即完成升級**（0.1.0 → 0.1.1，「加入或移除程式」維持一筆，
  `index-map.json` 位元組完全相同，LibreNMS 不會重新 discovery）、
  解除安裝會還原內建 SNMP Service 並保留設定與狀態、以及重複安裝。
  loopback 健康檢查失敗時整個交易會倒回

- **README** 英文與台灣繁體中文雙檔，格式參照 jt-ipam
- **資安檢測工具鏈**寫入 `docs/security-scanning_zh-TW.md`，並產出第一份基線：
  Bandit HIGH=0、pip-audit 掃過 59 個相依無弱點、CycloneDX SBOM 已產出。
  ZAP 不適用，它是 web DAST，而本 agent 沒有 HTTP 介面；正確組合是
  SAST + SCA/SBOM + 協定層 fuzzing，加上 Windows 專屬檢查（Authenticode、
  unquoted service path、`sc qprivs`、`accesschk`、PrivescCheck）
- **三分支 `sysObjectID`**，以 `DsRoleGetPrimaryDomainInformation` 判定網域控制站。
  LibreNMS 靠第三分支呼叫 `getDatacenterVersion()`，先前 DC 會顯示錯誤的 Windows 版本
- **Windows Server 情境**整理進 `TEST_PLAN.md` §5.5，22 項，涵蓋版本與安裝型態、
  Server 特有資料來源、部署差異

- **IP 位址表**：`ipAddrTable`（RFC 1213）與 `ipAddressTable`（IP-MIB，IPv4 + IPv6），
  以 `GetUnicastIpAddressTable` 取得，供 LibreNMS 的 ipv4-addresses /
  ipv6-addresses 模組使用
- **鄰居快取**（`ipNetToPhysicalTable`，ARP 與 IPv6 ND），以 `GetIpNetTable2` 取得。
  **預設停用**：內網 ARP 表等同現成的橫向移動目標清單
- **磁碟溫度與健康度**（ENTITY-SENSOR-MIB `entPhySensorTable`），以
  `IOCTL_STORAGE_QUERY_PROPERTY` 搭配 `StorageDeviceTemperatureProperty`
  與 NVMe SMART health log 取得。刻意不使用 LibreHardwareMonitor，
  其 WinRing0 驅動已列入 Microsoft vulnerable driver blocklist，
  在 HVCI 端點會觸發 Defender

- **以 `GetPerformanceInfo` 補齊記憶體資訊**：除了 Physical 與 Virtual Memory，
  新增 **Cached Memory**、**Swap Space**（commit limit 中屬於分頁檔的部分，
  與 commit charge 是不同概念）以及核心分頁 / 非分頁集區。
  LibreNMS 上的記憶體池由 2 個增為 4 個
- **`hrStorageDescr` 讀取真實磁碟區標籤與序號**（`GetVolumeInformationW`），
  取代原本硬編碼的預留字串。非 ASCII 標籤（例如中文磁碟區名稱）以 UTF-8 編碼，
  並已透過 LibreNMS 端對端驗證

- **`sysContact` / `sysLocation` 設定來源**：ADMX 原則優先，其次沿用
  Windows 內建 SNMP Service 的既有登錄檔設定。
  客戶原本就在用內建 SNMP 時，換過來不必重新填寫，即使內建服務已停用，
  其登錄檔仍在，設定仍會自動沿用。`jtAgentConfigSource` 會回報實際生效的來源
- **`build/` 與 `dist/` 資料夾**：`build/` 放 PyInstaller one-folder 的執行檔產物，
  `dist/` 放對外交付的安裝檔（MSI 等）。兩者都只有 README 進版本控制

- **完整 `hrSystem`**：補上 `hrSystemProcesses`（LibreNMS System → Processes 圖的來源）、
  `hrSystemDate`（RFC 2579 DateAndTime 二進位格式，含時區）、
  `hrSystemInitialLoadDevice` / `hrSystemInitialLoadParameters`
- **網路協定統計**（LibreNMS Netstats 整組圖表）：`ip`、`icmp`、`tcp`、`udp` 四個群組，
  全部走 iphlpapi 的 `GetIpStatisticsEx` / `GetIcmpStatistics` /
  `GetTcpStatisticsEx` / `GetUdpStatisticsEx`，一次呼叫取得整組計數器
- **SNMPv2-MIB `snmp` 群組**：agent 自身的封包統計，同時作為前置解析閘門
  丟棄量的對外出口

- **完整 inventory**：
  - **ENTITY-MIB `entPhysicalTable`**（LibreNMS Inventory 頁），資料來自
    `GetSystemFirmwareTable('RSMB')` 解析 SMBIOS，不需 WMI、不需特權。
    涵蓋 Type 0 BIOS、Type 1 System、Type 2 Baseboard、Type 4 Processor、
    Type 17 Memory Device，以分段 index 配置
    （1000 system / 1100 mainboard / 2000+ CPU / 3000+ DIMM / 4000+ 磁碟）
  - **`hrDeviceTable` 全家族**（LibreNMS 設備頁）：處理器、網路介面、實體磁碟，
    搭配 `hrProcessorTable`、`hrNetworkTable`、`hrDiskStorageTable`。
    所有衍生表共用同一組 `hrDeviceIndex`
  - **實體磁碟 inventory**：以 `IOCTL_STORAGE_QUERY_PROPERTY` 取型號、序號、
    匯流排類型，`IOCTL_DISK_GET_DRIVE_GEOMETRY_EX` 取容量
  - 硬體 inventory 永久快取，SMBIOS 開機後不會變

- **前置解析閘門**：位於 pysnmp 之前的
  四道檢查，來源 IP 白名單、封包大小上限、每來源 token bucket 速率限制、
  外層 TLV 粗略合法性。被擋下的封包**完全不會進入 BER decoder**，
  因此深度巢狀、超長長度欄位、OID 放大等攻擊碰不到 pyasn1

- **自我健康 OID**：agent 的失效是無聲的，
  這組 OID 讓 LibreNMS 能監控 agent 本身。含版本、服務執行時間、RSS、
  執行緒與 handle 數、快照年齡與建立耗時、設定路徑、安全性警告摘要
- **`jtAgentCollectorTable`**：每個 collector 的狀態、上次成功時間、耗時、
  累計錯誤數與最後錯誤訊息
- **collector 健康追蹤**：所有 collector 經 `_collector()` 包裝，
  失敗時回傳 default 而非拋出，agent 不會因單一 collector 故障而垮掉

- **專案定名 `jt-snmpd`**，服務名稱、執行檔名、安裝路徑一併定案（`docs/naming-and-paths.md`）
- **snapshot + bisect 架構**：整份 MIB 為依 OID 排序的陣列，GET 用 `bisect_left`、
  GETNEXT 用 `bisect_right`，SNMP 協定正確性成為結構保證而非人工維護
- **wire 預編碼**：快照建立時預先產生 BER 位元組，回應組裝退化為位元組串接
- **IF-MIB**（ifTable + ifXTable，含 64-bit counters）、**HOST-RESOURCES**
  （hrStorage / hrProcessor / hrDevice）、**UCD-DISKIO**
- **介面篩選**：只輸出實體網路卡，排除 WFP 篩選器驅動程式、VPN 虛擬卡、通道、loopback
- **ifIndex 保存**：以 NET_LUID 為主鍵，避免重開機後 LibreNMS 重建 port 與無主的 RRD
- **Windows 服務**：PyInstaller one-folder 打包成 `jt-snmpd.exe`，
  以自身為服務主程式，開機自啟、LocalSystem、目標機零 Python 依賴
- **`--selftest` 建置閘門**：建置後實際初始化 SNMP engine 並建立快照，
  可攔截「exe 產出但缺資料檔」的情況
- **程序優先權降級**：服務以 `BELOW_NORMAL_PRIORITY_CLASS` 執行
- **建置腳本** `packaging/build-exe.ps1`：參數單一來源，含控制代碼釋放驗證與產物新鮮度檢查
- **測試**：BER 大小對照（540 例）、walk 正確性（20 例）、base OID 對照 RFC（10 例）

### 修正

- **UCD `systemStats` 的欄位編號是憑記憶寫的，而且寫錯了。**
  正確順序是 IOSent(57) / IOReceived(58) / Interrupts(59) / Contexts(60) /
  SwapIn(62) / SwapOut(63)，我卻把 SwapIn/SwapOut 排在前面，
  結果 context switches 被畫在 I/O 圖上。從 agent 端完全看不出異常，
  walk 成功、圖表有線、數字在動。唯一能發現的方法是用 MIB 解析輸出
  （`snmpwalk -m UCD-SNMP-MIB -O QUs`）。
  `tests/test_ucd_field_numbers.py` 已把每個欄位釘死在 MIB 名稱上

- **`ipRouteTable` 在多網路卡主機上產生重複 OID。** RFC 1213 以目的位址單獨當索引，
  但每張網路卡都會有自己的 224.0.0.0 多播與 255.255.255.255 廣播路由。
  在一台有七個位址的筆電上，這觸發了重複 OID 護欄而讓 agent 拒絕啟動，
  連帶使 MSI 的健康檢查失敗並倒回安裝。現已依目的位址去重，
  保留 metric 最小者（即實際會被選用的路由）。此問題在單網路卡機器上永遠不會出現

- **`hrSystemNumUsers` 原本固定回 1。** 在遠端桌面工作階段主機上這直接就是錯的
，一台可能有數十個使用者。改以 `WTSEnumerateSessions` 列舉實際工作階段，
  計入 Active 與 Disconnected（斷線的使用者仍在登入狀態、仍佔用資源）
- **NIC team 成員與 team 介面同時被輸出**，導致 LibreNMS 對同一份流量計算兩次。
  team 成員的 `ConnectionType` 為 `Passive`，現已排除

以下皆為實機部署過程中發現並修正的缺陷：

- **服務顯示 Running 但未綁定 socket**：pysnmp 的 `open_server_mode()` 必須在
  running event loop 內呼叫，否則 socket 從未真正綁定
- **64 位回傳值被截斷**：所有 Win32 呼叫未宣告 `argtypes`/`restype` 時，
  ctypes 預設以 `c_int` 處理，導致 C: 磁碟顯示 0 GB、uptime 超過 24.8 天溢位
- **pywin32 服務類別必須在模組層級**：定義在函式內會得到
  `AttributeError: module has no attribute`，且服務啟動失敗時無任何記錄
- **ifXTable OID 錯誤**：`1.3.6.1.31.1.1.1` 少了 `2.1`，整張表掛在無效分支。
  LibreNMS 的 `ifname: true` 依賴此表，錯誤時 Ports 頁缺名稱與 64-bit counters
- **非 ASCII OCTET STRING 編碼失敗**：pyasn1 預設以 latin-1 編碼字串，
  正體中文網路卡名（「乙太網路」）會直接拋出 `PyAsn1UnicodeEncodeError`
- **含空白路徑未加引號會被截斷**：預設安裝路徑 `%ProgramFiles%\jt-snmpd\`
  本身即含空白，未加引號時行程啟動失敗且無記錄
- **PowerShell 指令碼需 UTF-8 BOM**：Windows PowerShell 5.1 在無 BOM 時
  以系統 ANSI 代碼頁讀取 `.ps1`，中文註解會打斷語法剖析
- **建置產物新鮮度誤判**：僅以「exe 是否存在」判定建置成功，
  在建置失敗時會取到殘留的舊版本
- **已載入映像無法刪除**：Windows 對已載入為映像的 `.pyd`/`.dll` 回傳
  存取被拒，即使服務已停止並解除註冊。改以重新命名處理

### 效能

- MIB 層查詢：**8 µs/varbind**
- 回應組裝：**164 → 0.35 µs/varbind**（改用 wire 預編碼）
- 完整請求路徑：**18.3 µs/varbind**
- **主機影響**（7,000 倍於實際輪詢負載的壓力測試下）：
  固定工作負載退化自 **4.19% 降至 0.41%**（程序優先權降級後）
- 記憶體：1,406 次完整 walk 後 RSS 增加 0.12 MB，執行緒與 handle 數持平

### 已知限制

- 尚未驗證：多網路卡來源位址（測試環境為單網路卡）、HVCI/WDAC 端點、Authenticode 簽章
- 尚未實作：SNMPv3、前置解析閘門、VACM 預設集、自我健康 OID、MSI 安裝程式
