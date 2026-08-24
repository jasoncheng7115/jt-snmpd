# dist/ — Release artefacts (installers)

繁體中文：[README_zh-TW.md](README_zh-TW.md)

What ships to customers. **Not under version control** (see `.gitignore`);
produced by the release process and attached to GitHub Releases.

```
dist/
├── jt-snmpd-<version>-x64.msi           the deliverable — GPO / Intune / SCCM / msiexec
├── jt-snmpd-<version>-x64.msi.sha256
└── releases/<version>/                  per-version archive (MSI + sha256 + BUILDINFO.txt)
```

## Why MSI

Group Policy software installation accepts MSI and nothing else, which settles the
choice on its own. MSI also brings UpgradeCode-based upgrades, transactional
rollback on failure, and an entry in Add/Remove Programs — visible both to customer
asset inventories and to `hrSWInstalledTable`.

EXE installers produced by Inno Setup or NSIS support neither GPO software
installation nor transactional rollback. They are not in the same category.

## Delivery rules

- **Fully self-contained.** The installer downloads nothing; everything, including
  any third-party binary, is inside the MSI.
- **Signing is not in place yet.** An unsigned file cannot be allowed by
  publisher rule under WDAC, so those sites need a hash rule or their own
  certificate until the project certificate arrives — see
  `docs/code-signing.md`.
- **Release Gate green before it ships.** See `TEST_PLAN.md` §10.

## Status

The MSI is implemented and verified on real hardware: install, upgrade, uninstall,
reinstall and PURGE uninstall — 40 lifecycle assertions, all green
(`tests/lifecycle.ps1`).

Each release is archived under `dist/releases/<version>/` with the MSI, its
`.sha256`, and `BUILDINFO.txt`. BUILDINFO records the SHA-256 of three sources —
the configure script, the WiX source, and the agent. That exists because a machine
once held two copies of `msi-configure.ps1` and the build used the one that had not
been edited: the build succeeded, the version number advanced, and the fix was
simply not in the MSI. The fingerprints are how you answer "which version of the
script is inside the package the customer is holding".

**This installer is not Authenticode signed yet.** A certificate through an
open-source code-signing programme is planned; until then integrity comes from
the accompanying `.sha256`. For trusting it manually and for WDAC and AppLocker
environments, see
https://jasoncheng7115.github.io/jt-snmpd/code-signing.html

The installer itself does not go into git; it is attached to the GitHub Release.
