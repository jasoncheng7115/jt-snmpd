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
| `docs/images/memory-en.png` | `602a3797bccad64b369dd870ec29117cf38b88f7b972d78a9755a2550bbcdebf` |
| `docs/images/memory-zh-TW.png` | `10e0c3ae5666dc94f866d7566d6ada3d3485f7ca53dbeb4f0f774c45630daa1e` |
| `docs/images/ports-en.png` | `3668e17341f1e0baba92bad0fbb0fe1bbccda8b228589bd004b87338dd75ff86` |
| `docs/images/ports-zh-TW.png` | `88fea3208bf03a1573082bf2879970e7b91f17db527bd76fb46899771e755d78` |
| `docs/images/smart-en.png` | `3074c3d5cd5a24487d899fa53e71497845731e8e553173ab4a65e926b0904438` |
| `docs/images/smart-zh-TW.png` | `7a97a20acfbcd5909efca4c1a21e0a6b8f1f6079551dd90b320cc6aff52513b1` |
| `docs/images/temperature-en.png` | `36f1001d21aa29ceed8cc67e9828660ed665ce80069d7d67dd19390376fc42ea` |
| `docs/images/temperature-zh-TW.png` | `c008033143d03b458b4e57b76e1623d3adf54a0c18d8f0b578a96839a4f1b8ca` |
| `packaging/wix/banner.bmp` | `4cac8d832c96cf4e8cd15ea66434c09629e01a95773d5afd5057a9251f7976e8` |
| `packaging/wix/dialog.bmp` | `c102c7e64ab5f7c4f26dd5a76799b152e8a0475026bd3d74f0757910f6914a83` |
