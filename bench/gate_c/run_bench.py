"""The gate C performance experiment.

Three measurements:
  1. processing cost per varbind        threshold < 80 µs
  2. full-tree snmpbulkwalk time        threshold < 10 s
  3. one GETBULK round trip             threshold < 30 ms (max-repetitions 25)

It also runs pysnmp's stock BulkCommandResponder alongside the batched one, to
put a number on what "GETBULK degenerates to an array slice" is worth.
"""

from __future__ import annotations

import os
import re
import shutil
import statistics
import subprocess
import sys
import time

HOST, PORT, COMMUNITY = "127.0.0.1", 11171, "bench"
BASE_OID = ".1.3.6.1.4.1.99999"
PY = sys.executable
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat", "rb") as fh:
        parts = fh.read().rsplit(b")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    return (int(parts[11]) + int(parts[12])) / ticks  # utime + stime


def _start_agent(n: int, stock: bool):
    cmd = [PY, "-m", "bench.gate_c.agent", "--varbinds", str(n),
           "--host", HOST, "--port", str(PORT), "--community", COMMUNITY]
    if stock:
        cmd.append("--stock-bulk")
    p = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(600):
        line = p.stdout.readline()
        if not line:
            break
        if line.startswith("READY"):
            return p, float(re.search(r"build_ms=([\d.]+)", line).group(1))
        time.sleep(0.01)
    p.kill()
    raise RuntimeError("the agent did not become ready")


def _walk(timeout: int) -> tuple[float, int]:
    t = time.perf_counter()
    r = subprocess.run(
        ["snmpbulkwalk", "-v2c", "-c", COMMUNITY, "-Cr25", "-r0", "-t", "10", "-On",
         f"{HOST}:{PORT}", BASE_OID],
        capture_output=True, text=True, timeout=timeout,
)
    d = time.perf_counter() - t
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith(".1.3.6.1.4.1.99999")]
    return d, len(lines)


def _getbulk_latency(samples: int = 40) -> list[float]:
    """One GETBULK round trip at max-repetitions 25; snmpbulkget sends a single packet."""
    out = []
    for i in range(samples):
        oid = f"{BASE_OID}.1.1.{(i % 20) + 1}.1"
        t = time.perf_counter()
        subprocess.run(
            ["snmpbulkget", "-v2c", "-c", COMMUNITY, "-Cr25", "-Cn0", "-r0", "-t", "5", "-On",
             f"{HOST}:{PORT}", oid],
            capture_output=True, text=True, timeout=15,
)
        out.append((time.perf_counter() - t) * 1000)
    return out


def main() -> None:
    if not shutil.which("snmpbulkwalk"):
        sys.exit("the net-snmp tools are required: apt-get install -y snmp")

    sizes = [int(x) for x in (sys.argv[1:] or ["1000", "10000", "50000"])]
    print(f"{'varbinds':>9} {'bulk':>8} {'build_ms':>9} {'walk_s':>8} {'µs/vb':>8} "
          f"{'agent_cpu_s':>11} {'cpu_µs/vb':>10} {'getbulk_p50':>12} {'getbulk_p95':>12} {'count':>7}")
    print("-" * 108)

    for n in sizes:
        for stock in (False, True):
            proc, build_ms = _start_agent(n, stock)
            try:
                _walk(timeout=300)  # warm-up, so the interpreter and caches settle
                c0 = _cpu_seconds(proc.pid)
                t0 = time.perf_counter()
                reps = 3
                total_rows = 0
                for _ in range(reps):
                    d, rows = _walk(timeout=300)
                    total_rows = rows
                walk_s = (time.perf_counter() - t0) / reps
                cpu = (_cpu_seconds(proc.pid) - c0) / reps
                lat = _getbulk_latency()
                print(f"{n:>9} {'stock' if stock else 'batched':>8} {build_ms:>9.1f} "
                      f"{walk_s:>8.3f} {walk_s / max(total_rows, 1) * 1e6:>8.1f} "
                      f"{cpu:>11.3f} {cpu / max(total_rows, 1) * 1e6:>10.1f} "
                      f"{statistics.median(lat):>11.2f}m {sorted(lat)[int(len(lat) * 0.95)]:>11.2f}m "
                      f"{total_rows:>7}")
            finally:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    main()
