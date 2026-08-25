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
| `docs/images/memory-en.png` | `7d42551fe8c8543d0647c6695029cb0b6a15fb480344fa9a143e5a5b1d7e5778` |
| `docs/images/memory-zh-TW.png` | `5dc744c3a318cfafaf7f48fa906fe122b17125d709ffd2c39b8260b9b8611979` |
| `docs/images/ports-en.png` | `e3a7d9eb0496d0e7bd375e78c66879a51d608ddb6c196eba49d2edfee1f8ce6f` |
| `docs/images/ports-zh-TW.png` | `dacb68467f258e931d57aeeeaf1abc6ae0a11bc5db6d8abdffa5ad839875abc0` |
| `docs/images/smart-en.png` | `082fa381e42e0917ccbce24154ba8263129ee93133a8ab233236227cdc55e39d` |
| `docs/images/smart-zh-TW.png` | `125e6a3fa2d0f2f82b4a48aaa55ff24db846f0616644e0e3a579042d5364a107` |
| `docs/images/temperature-en.png` | `b563b47aa11a0497a7316d21cedbbab7546a094d2e86456a844c6f5de022e434` |
| `docs/images/temperature-zh-TW.png` | `b20984c8741231ee5a621c5f34904b2475e18c53f6c8ffa2046cebc1706e7b3a` |
| `packaging/wix/banner.bmp` | `4cac8d832c96cf4e8cd15ea66434c09629e01a95773d5afd5057a9251f7976e8` |
| `packaging/wix/dialog.bmp` | `c102c7e64ab5f7c4f26dd5a76799b152e8a0475026bd3d74f0757910f6914a83` |
