# 圖片人工審閱紀錄

正規表示式讀不到像素。README 的截圖曾經把 LibreNMS 畫出來的 SNMP 鄰居
一併帶了出去——MAC 位址、內部主機名稱、IPv6 位址，等於公開內網拓撲。

**每一張圖在加入或更新後都必須被人實際看過**，確認沒有：

- MAC 位址
- 真實主機名稱與內部網域
- 內網 IP（自己網段的位址，而非文件用保留位址）
- 硬體序號、授權金鑰、community 字串
- 鄰居裝置名稱（LibreNMS 的連接埠頁會顯示 SNMP/LLDP 鄰居）
- 使用者姓名與帳號

確認後執行 `python3 tools/check-privacy.py --update-images` 更新下表。
雜湊對不上時掃描會擋下推送，強迫重新審閱。

| 檔案 | SHA-256 |
|---|---|
| `docs/brand/icon-128.png` | `95b9300a0ed8d7b9f56ddd623eb0905f02732ee7fd9c535d15b52504396b6199` |
| `docs/brand/icon-16.png` | `856df776f154f92fe9d848e41c2685c73283f9b7ab196f348590a9aa7858cd1f` |
| `docs/brand/icon-180.png` | `d234d3940241c45d60284785e8770a1c8db9d5c3ef723f2cd5d6038add6cbb40` |
| `docs/brand/icon-256.png` | `9e9d535f637107a6076b63e6b6e3ab813b35af4d7e3bc449f786fb654dd3cbc5` |
| `docs/brand/icon-32.png` | `041b48cf160abec9013d12a55eb50cf5a769c67a6cf2acdc9880abdf7bd359a3` |
| `docs/brand/icon-48.png` | `2b702ec4f45a62d40ca813e96874f934e9bb4f923f88e43a0552c2045d92a132` |
| `docs/brand/icon-512.png` | `4931f8579064d8454fb7f5c93121644a3767d4ee3ec7d5790dd6d148fb7008ef` |
| `docs/brand/icon-64.png` | `ee7ef7dd67c750406f251590f21fe5eb6460a245ef4ec2f5fdd0fbc752e05899` |
| `docs/brand/jt-snmpd.ico` | `44b5143e8ab2c3f6e780036088f663c50bb1df51ec2baba3ac997d519212ebbd` |
| `docs/images/memory-en.png` | `602a3797bccad64b369dd870ec29117cf38b88f7b972d78a9755a2550bbcdebf` |
| `docs/images/memory-zh-TW.png` | `10e0c3ae5666dc94f866d7566d6ada3d3485f7ca53dbeb4f0f774c45630daa1e` |
| `docs/images/ports-en.png` | `3668e17341f1e0baba92bad0fbb0fe1bbccda8b228589bd004b87338dd75ff86` |
| `docs/images/ports-zh-TW.png` | `88fea3208bf03a1573082bf2879970e7b91f17db527bd76fb46899771e755d78` |
| `docs/images/smart-en.png` | `85768b3cd98386538213f3b965e6244a975c8b61fe1837a0e81c5e76af37e8bd` |
| `docs/images/smart-zh-TW.png` | `cd2300a19ebdf2c27f0dc71896b3558cd71255dd5eb20ea2240878a03d7b40c7` |
| `docs/images/temperature-en.png` | `36f1001d21aa29ceed8cc67e9828660ed665ce80069d7d67dd19390376fc42ea` |
| `docs/images/temperature-zh-TW.png` | `c008033143d03b458b4e57b76e1623d3adf54a0c18d8f0b578a96839a4f1b8ca` |
