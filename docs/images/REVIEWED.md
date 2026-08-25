# Image review record

A regular expression cannot read pixels. A README screenshot once carried
the SNMP neighbours LibreNMS had drawn into it: MAC addresses, internal host
names and IPv6 addresses, which together map the internal network.

**Every image has to be looked at by a person after it is added or changed**,
and confirmed to contain none of:

- MAC addresses
- Real host names and internal domains
- Internal addresses (your own ranges, as opposed to the documentation ranges)
- Hardware serial numbers, licence keys, community strings
- Neighbour device names (LibreNMS's ports page shows SNMP and LLDP neighbours)
- User names and account names

Once confirmed, run `python3 tools/check-privacy.py --update-images` to refresh
the table below. When a hash no longer matches, the scan blocks the push and
the review has to happen again.

| File | SHA-256 |
|---|---|
| `docs/brand/icon-128.png` | `95b9300a0ed8d7b9f56ddd623eb0905f02732ee7fd9c535d15b52504396b6199` |
| `docs/brand/icon-16.png` | `856df776f154f92fe9d848e41c2685c73283f9b7ab196f348590a9aa7858cd1f` |
| `docs/brand/icon-180.png` | `d234d3940241c45d60284785e8770a1c8db9d5c3ef723f2cd5d6038add6cbb40` |
| `docs/brand/icon-256.png` | `9e9d535f637107a6076b63e6b6e3ab813b35af4d7e3bc449f786fb654dd3cbc5` |
| `docs/brand/icon-32.png` | `041b48cf160abec9013d12a55eb50cf5a769c67a6cf2acdc9880abdf7bd359a3` |
| `docs/brand/icon-48.png` | `2b702ec4f45a62d40ca813e96874f934e9bb4f923f88e43a0552c2045d92a132` |
| `docs/brand/icon-512.png` | `4931f8579064d8454fb7f5c93121644a3767d4ee3ec7d5790dd6d148fb7008ef` |
| `docs/brand/icon-64.png` | `ee7ef7dd67c750406f251590f21fe5eb6460a245ef4ec2f5fdd0fbc752e05899` |
| `docs/brand/jt-snmpd.ico` | `dfcd3497f2f6745e14375a3472ca64b06658ffaf367477ac93d702608b3f5253` |
| `docs/images/install-1-welcome.png` | `b889b2cea18d29056929157e76d764229bcb3bb2765aedaa151375fe52855be4` |
| `docs/images/install-2-license.png` | `d9816320539558f179024661a41af9af3525327a5cf4a2b41504ae5ff60df3a0` |
| `docs/images/install-3-folder.png` | `3795394ce1b54a561625e1d2ce68061209ceb7f210fb9eb8210885a10a7e7d9e` |
| `docs/images/install-4-settings.png` | `0d99b7b52f19b4140897d0cf9d7f7df0c7ae89ba38980c83fef1013417961a3a` |
| `docs/images/install-5-ready.png` | `b4def2b38b7f944705344ca12bcf9f673227e67cbcadbb0b1c1d3b727780d786` |
| `docs/images/install-6-done.png` | `9d1426a41d836255180e3fccb0a1bd15a8e35ca47b896fb54b916cb13aaab62e` |
| `docs/images/memory-en.png` | `d21b851ee49e7b3912e03bab089e7eb73db690ed57dcc8da2fd28a7d07c085f5` |
| `docs/images/memory-zh-TW.png` | `caae5629033fa59c934d010348b066b947002547813c1c29b0f12cf23115e8bd` |
| `docs/images/ports-en.png` | `fdf6fe416f7ca517b1bfc05a908400ee2924044360fbd8bf3025100d3ef49174` |
| `docs/images/ports-zh-TW.png` | `aaf9ed8517c5c2fa3f9b852aafa84731feb3c2fa7c06e808ea8393783cd06f47` |
| `docs/images/smart-en.png` | `f9834a32093e0f52e9d8fe8022c2c4327fde275833dc6dd017863db07e76a093` |
| `docs/images/smart-zh-TW.png` | `3559056830441b5e2a59a3b03f2fe28f1de9855faa3e347b026b3e4dad26e783` |
| `docs/images/temperature-en.png` | `1a5f7b768623820f1d6ecf0e1f8c7ebf53290f8b081809129157d05228569171` |
| `docs/images/temperature-zh-TW.png` | `82e481c9c2442f613a9fc41472d737a04960a00e07af513125f3a7bcfe2aac20` |
| `packaging/wix/banner.bmp` | `4cac8d832c96cf4e8cd15ea66434c09629e01a95773d5afd5057a9251f7976e8` |
| `packaging/wix/dialog.bmp` | `c102c7e64ab5f7c4f26dd5a76799b152e8a0475026bd3d74f0757910f6914a83` |
