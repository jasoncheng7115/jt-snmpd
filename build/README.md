# build/ — Build output (executable)

繁體中文：[README_zh-TW.md](README_zh-TW.md)

PyInstaller one-folder output. **Not under version control** (see `.gitignore`).

```
build/
└── jt-snmpd/
    ├── jt-snmpd.exe        the service (and the CLI entry point)
    └── _internal/          bundled Python runtime, pysnmp, pywin32
```

## How it is produced

On the Windows target:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-exe.ps1 `
    -Python C:\jtdev\Python312\python.exe `
    -Source deploy\jt_agent.py `
    -OutDir build
```

The build script has three gates; failing any of them exits 1:

1. **Stop the service and wait for the process to disappear.** `Stop-Service`
   returning does not mean the file handles have been released.
2. **The artefact must be newer than the source.** Checking only that the exe
   exists picks up a stale copy when the build has actually failed — this happened.
3. **`--selftest`.** Actually initialises the SNMP engine and builds a snapshot.
   Catches the case where the exe is produced but a data file is missing: pysnmp's
   MIB files were once left out, the exe built fine, and the service reported
   Running while raising `MibNotFoundError` on every request.

## Why one-folder rather than one-file

One-file extracts itself into `%TEMP%` (under the service account, that is
`C:\Windows\Temp`) before executing. That is a known DLL hijacking path, and it is
also more likely to be blocked under WDAC and HVCI. This is a hard rule for the
project, not a preference.
