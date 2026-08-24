"""jt-snmpd 版本資訊 —— **單一來源**。

為什麼要獨立成檔：版本號原本硬編碼在 jt_agent.py，而 MSI 版本由建置腳本
的參數決定，兩者從未連動。實測後果是 MSI 已升到 0.1.6，
但 SNMP 的 jtAgentVersion 仍回報 0.1.0-dev——
而那個 OID 存在的唯一理由，就是「升級數百台後一次 walk 得知哪台沒升成功」。
版本對不上，這個功能等於沒有。

建置流程會讀這裡的 VERSION，同時用於：
  - PyInstaller 產物內嵌的 jtAgentVersion
  - MSI 的 ProductVersion 與檔名
  - 發佈包檔名
"""

VERSION = "0.9.1"
BUILD_DATE = "2026-08-24"
