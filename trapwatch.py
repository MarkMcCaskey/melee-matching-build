#!/usr/bin/env python3
"""Boot a trap ISO under headless Dolphin and report the first unmatched
function the game actually reaches.

    trapwatch.py fn                 # function-granularity build
    trapwatch.py tu                 # TU-granularity build
    trapwatch.py control            # unpatched ISO, as a control
    trapwatch.py fn --iterate 5     # keep going: exclude each hit and re-run

A trap raises a Program exception; the SDK's exception reporter dumps it over
OSReport as `ERROR 6: (PROGRAM)` / `Trap program exception at <SRR0>`, and
Dolphin logs that. So detection needs no breakpoints -- which is just as well,
since Dolphin's GDB-stub breakpoints do not fire under the JIT.

Needs a Dolphin with the nogui frontend (~/etc/dolphin-dap); the stock macOS
.app has neither the headless platform nor working file logging. That fork also
boots *paused* waiting for a debug client, so we attach to its GDB stub purely
to send one `continue`.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DOLPHIN = "~/etc/dolphin-dap/build/Binaries/dolphin-emu-nogui"

SRR0_RE = re.compile(r"exception at ([0-9A-Fa-f]{8}) \(read from SRR0\)")
ERROR_RE = re.compile(r"ERROR (\d+): \(([A-Z ]+)\)")
OSREPORT_RE = re.compile(r"N\[OSREPORT\]: (.*)$", re.M)
SYM_RE = re.compile(r"^(\S+) = \.text:0x([0-9A-Fa-f]+);.*?size:0x([0-9A-Fa-f]+)")


def repo_root(explicit=None):
    p = Path(explicit or os.environ.get("MELEE_REPO", "~/etc/melee")).expanduser()
    if not (p / "configure.py").exists():
        sys.exit(f"{p} does not look like the melee repo (set MELEE_REPO or --repo)")
    return p.resolve()


# --------------------------------------------------------------- gdb (resume only)

class Gdb:
    """Just enough GDB remote protocol to resume a paused Dolphin."""

    def __init__(self, port, timeout=5):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.s.settimeout(timeout)
        self.buf = b""

    def _recv(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(4096)
            if not chunk:
                raise EOFError("gdb stub closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, cmd):
        pkt = cmd.encode()
        self.s.sendall(b"$" + pkt + b"#" + b"%02x" % (sum(pkt) & 0xFF))
        while True:
            if self._recv(1) == b"$":
                break
        data = b""
        while True:
            c = self._recv(1)
            if c == b"#":
                break
            data += c
        self._recv(2)
        self.s.sendall(b"+")
        return data.decode(errors="replace")

    def cont(self):
        self.s.sendall(b"$c#63")


def listening_ports(pid):
    """The fork picks an ephemeral GDB port, so discover it from the process."""
    try:
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP",
                              "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    except OSError:
        return []
    return [int(m) for m in re.findall(r":(\d+) \(LISTEN\)", out)]


def resume(proc, tries=40):
    for _ in range(tries):
        for port in listening_ports(proc.pid):
            try:
                g = Gdb(port)
                if g.send("?").startswith(("S", "T")):
                    g.cont()
                    return port
            except (OSError, EOFError):
                pass
        if proc.poll() is not None:
            return None
        time.sleep(0.5)
    return None


# ------------------------------------------------------------------------ symbols

def load_symbols(repo):
    syms = []
    for line in (repo / "config/GALE01/symbols.txt").read_text().splitlines():
        m = SYM_RE.match(line)
        if m:
            syms.append((int(m.group(2), 16), int(m.group(3), 16), m.group(1)))
    syms.sort()
    return syms


def sym_for(syms, addr):
    lo, hi, best = 0, len(syms) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if syms[mid][0] <= addr:
            best, lo = syms[mid], mid + 1
        else:
            hi = mid - 1
    if best and best[0] <= addr < best[0] + max(best[1], 4):
        off = addr - best[0]
        return f"{best[2]}+0x{off:X}" if off else best[2]
    return f"0x{addr:08X}"


# ---------------------------------------------------------------------- emulation

def provision_userdir(path, repo):
    """Dolphin needs OSREPORT logging on; the symbol map is optional but makes
    Dolphin's own log name things."""
    cfg = path / "Config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "Logger.ini").write_text(
        "[Options]\nVerbosity = 4\nWriteToFile = True\nWriteToConsole = True\n"
        "WriteToWindow = False\n[Logs]\nOSREPORT = True\nOSREPORT_HLE = True\n"
        "BOOT = True\nSYMBOLS = True\n")
    (cfg / "Dolphin.ini").write_text(
        "[Core]\nCPUCore = 1\nDSPHLE = True\nSkipIPL = True\nCPUThread = False\n"
        "[Interface]\nConfirmStop = False\nUsePanicHandlers = False\n"
        "[Analytics]\nEnabled = False\nPermissionAsked = True\n"
        "[DSP]\nBackend = No audio output\n")
    mapsrc = repo / "build/GALE01/main.elf.MAP"   # `configure.py --map && ninja`
    if mapsrc.exists():
        maps = path / "Maps"
        maps.mkdir(exist_ok=True)
        for name in ("GALE01.map", "GALE01r2.map"):
            (maps / name).write_bytes(mapsrc.read_bytes())
    return path


def run_once(dolphin, repo, mode, seconds, userdir, iso_override, tag=""):
    iso = (Path(iso_override).expanduser() if iso_override else
           (repo / "ssbm_rev2.iso" if mode == "control"
            else repo / f"build/GALE01/ssbm-{mode}100.iso"))
    if not iso.exists():
        sys.exit(f"{iso} missing; run trapbuild/traprom.py {mode} first")
    logpath = repo / f"build/GALE01/trapwatch-{mode}{tag}.log"
    cmd = [dolphin, "--platform", "headless", "-v", "Null",
           "-C", "Dolphin.General.GDBPort=55555",     # fork boots paused; we resume it
           "-C", "Dolphin.Core.CPUThread=False",      # required by the gdb stub
           "-C", "Dolphin.Core.CPUCore=1",            # JIT
           "-C", "Dolphin.Core.EmulationSpeed=0.0",   # unthrottled boot
           "-u", str(userdir), "--exec", str(iso)]

    with open(logpath, "w") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        port = resume(proc)
        if port is None:
            proc.kill()
            sys.exit("could not attach to dolphin's gdb stub to resume emulation")
        print(f"  resumed via gdb stub on port {port}")
        srr0 = err = None
        deadline = time.time() + seconds
        try:
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.5)
                text = logpath.read_text(errors="replace")
                m = SRR0_RE.search(text)
                if m:
                    srr0 = int(m.group(1), 16)
                    e = ERROR_RE.search(text)
                    err = e.groups() if e else None
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return srr0, err, logpath


def report(srr0, err, logpath, syms, manifest):
    osr = OSREPORT_RE.findall(logpath.read_text(errors="replace"))
    if srr0 is None:
        print("  NO TRAP: ran to the time limit without a program exception.")
        if osr:
            print(f"  (last OSReport: {osr[-1].strip()!r})")
        return None
    hit = next((m for m in manifest
                if m["address"] <= srr0 < m["address"] + m["size"]), None)
    print(f"  TRAPPED: ERROR {err[0]} ({err[1]})" if err else "  TRAPPED")
    print(f"  SRR0 0x{srr0:08X} -> {sym_for(syms, srr0)}")
    if hit:
        print(f"  {hit['function']}  [{hit['unit']}]  size {hit['size']}  "
              f"match {hit['match_percent']:.3f}%")
    stack = [l for l in osr if re.match(r"^[0-9A-F]{8}:", l.strip())]
    if stack:
        print("  caller chain (LR save):")
        for l in stack:
            parts = l.split()
            if len(parts) >= 3:
                print(f"    {parts[2]}  {sym_for(syms, int(parts[2], 16))}")
    return hit


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["fn", "tu", "control"])
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--iterate", type=int, default=1,
                    help="after each hit, rebuild with that function kept and re-run")
    ap.add_argument("--repo", help="melee checkout (default $MELEE_REPO or ~/etc/melee)")
    ap.add_argument("--dolphin", help="dolphin-emu-nogui binary")
    ap.add_argument("--userdir", help="Dolphin user dir (default <tooldir>/.dolphin-user)")
    ap.add_argument("--iso", help="override the ISO to boot")
    args = ap.parse_args()

    repo = repo_root(args.repo)
    dolphin = os.path.expanduser(
        args.dolphin or os.environ.get("DOLPHIN_NOGUI", DEFAULT_DOLPHIN))
    if not Path(dolphin).exists():
        sys.exit(f"no headless dolphin at {dolphin} (set DOLPHIN_NOGUI or --dolphin)")
    userdir = provision_userdir(
        Path(args.userdir).expanduser() if args.userdir else HERE / ".dolphin-user", repo)

    syms = load_symbols(repo)
    keep, found = [], []
    for i in range(args.iterate):
        if keep:
            cmd = [sys.executable, str(HERE / "trapbuild" / "traprom.py"), args.mode, "--repo", str(repo)]
            for k in keep:
                cmd += ["--keep", k]
            print(f"\nrebuilding with {len(keep)} function(s) kept...")
            subprocess.run(cmd, stdout=subprocess.DEVNULL, check=True)
        manifest = ([] if args.mode == "control" else json.loads(
            (repo / f"build/GALE01/main-{args.mode}100.traps.json").read_text()))
        print(f"\n=== {args.mode} run {i + 1}/{args.iterate} "
              f"({len(manifest)} traps, {args.seconds}s limit) ===")
        srr0, err, logpath = run_once(dolphin, repo, args.mode, args.seconds,
                                      userdir, args.iso, tag=f"-{i}" if i else "")
        hit = report(srr0, err, logpath, syms, manifest)
        if hit is None:
            break
        found.append(hit)
        keep.append(hit["function"])

    if len(found) > 1:
        print(f"\nboot-blocking order for `{args.mode}`:")
        for n, h in enumerate(found, 1):
            print(f"  {n}. {h['function']}  [{h['unit']}]  "
                  f"{h['match_percent']:.3f}%  {h['size']}B")


if __name__ == "__main__":
    main()
