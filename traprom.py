#!/usr/bin/env python3
"""Build a "matched-only" Melee: every function that is not byte-matched is
replaced with PowerPC traps, spliced into a bootable ISO.

    traprom.py both            # DOLs + ISOs for both granularities
    traprom.py fn              # function granularity only
    traprom.py tu              # TU granularity only
    traprom.py top --for fn    # what to match next, by trapped bytes
    traprom.py lookup --for fn 0x803A00C0    # SRR0 from a crash -> function

Requires a green `ninja` in the melee repo: it reads build/GALE01/report.json
(objdiff's per-function scores) and build/GALE01/main.dol.

Trap encoding
    entry word  twi 31,r0,<id>   0x0FE0xxxx   id = index into the manifest
    body fill   trap             0x7FE00008   tw 31,r0,r0

`trap` is PowerPC's `ud2`: an unconditional `tw` that always raises a Program
exception. `twi 31,r0,SIMM` traps identically but leaves 16 free immediate
bits, so the entry word carries its own id.

Granularity
    fn  trap each function whose objdiff fuzzy_match_percent < 100
    tu  trap every function in a TU that is not `Matching` in configure.py,
        i.e. not linked from our own object, matching-or-not

The ISO is an APFS clone (`cp -c`) of the source ISO with only the changed DOL
bytes spliced in, so each variant costs a few hundred KB of real disk.
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

TRAP = 0x7FE00008  # tw 31,r0,r0
TWI = 0x0FE00000   # twi 31,r0,SIMM

# The exception reporter must survive, or a trap double-faults instead of
# drawing the crash screen: this (non-matching) TU owns melee's
# fn_OSErrorHandler path -- Exception_ReportCodeline, hsd_80397DA4, ...
DEFAULT_KEEP = ["debugconsole_main"]


def repo_root(explicit=None):
    p = Path(explicit or os.environ.get("MELEE_REPO", "~/etc/melee")).expanduser()
    if not (p / "configure.py").exists():
        sys.exit(f"{p} does not look like the melee repo (set MELEE_REPO or --repo)")
    return p.resolve()


# --------------------------------------------------------------------------- DOL

def dol_sections(data):
    h = struct.unpack(">18I18I18I", data[:0xD8])
    off, addr, size = h[0:18], h[18:36], h[36:54]
    return [(off[i], addr[i], size[i]) for i in range(18) if size[i]]


def collect(report, mode, keep):
    """[(vaddr, size, fn, unit, pct)] for every function to trap."""
    out = []
    for u in report["units"]:
        meta = u["metadata"]
        if meta.get("module_id") not in (0, None):
            continue           # main.dol only; RELs are not spliced
        linked = bool(meta.get("complete"))
        for f in u.get("functions") or []:
            pct = float(f["fuzzy_match_percent"])
            if not (pct < 100.0 if mode == "fn" else not linked):
                continue
            if any(k in u["name"] or k in f["name"] for k in keep):
                continue
            size = int(f["size"])
            if size < 4:
                continue
            out.append((int(f["metadata"]["virtual_address"]), size, f["name"],
                        u["name"], pct))
    out.sort()
    return out


def patch_dol(repo, mode, fill, keep):
    report = json.loads((repo / "build/GALE01/report.json").read_text())
    dol = repo / "build/GALE01/main.dol"
    data = bytearray(dol.read_bytes())
    secs = dol_sections(bytes(data))

    def to_off(va, n):
        for o, a, s in secs:
            if a <= va and va + n <= a + s:
                return o + (va - a)
        return None

    manifest, trapped, missing = [], 0, 0
    for va, size, fn, unit, pct in collect(report, mode, keep):
        off = to_off(va, size)
        if off is None:
            missing += 1
            continue
        tid = len(manifest)
        if tid > 0xFFFF:
            sys.exit("more than 65536 traps; id will not fit in the twi SIMM field")
        n = size if fill == "body" else 4
        struct.pack_into(">I", data, off, TWI | tid)
        for i in range(4, n, 4):
            struct.pack_into(">I", data, off + i, TRAP)
        trapped += n
        manifest.append({"id": tid, "address": va, "size": size, "function": fn,
                         "unit": unit, "match_percent": pct})

    out_dol = repo / f"build/GALE01/main-{mode}100.dol"
    out_dol.write_bytes(bytes(data))
    text = sum(s for _, a, s in secs if 0x80003100 <= a < 0x803B7240)
    print(f"[{mode}] {len(manifest)} functions trapped, {trapped} bytes "
          f"({100.0 * trapped / text:.2f}% of .text)")
    if missing:
        print(f"[{mode}] WARNING: {missing} functions not present in the DOL")
    write_manifest(repo, mode, manifest)
    print(f"[{mode}] dol -> {out_dol.relative_to(repo)}")
    return out_dol, manifest


def write_manifest(repo, mode, manifest):
    txt = repo / f"build/GALE01/main-{mode}100.traps.txt"
    with open(txt, "w") as fh:
        fh.write("# id     address     size  match%   function  [unit]\n")
        for m in manifest:
            fh.write("%-6d 0x%08X %6d %7.3f  %s  [%s]\n" %
                     (m["id"], m["address"], m["size"], m["match_percent"],
                      m["function"], m["unit"]))
    (repo / f"build/GALE01/main-{mode}100.traps.json").write_text(
        json.dumps(manifest, indent=1))
    print(f"[{mode}] manifest -> {txt.relative_to(repo)} (+ .json)")


# --------------------------------------------------------------------------- ISO

def build_iso(repo, mode, patched_dol, src_iso):
    if not src_iso.exists():
        print(f"[{mode}] no {src_iso}; skipping ISO")
        return None
    out = repo / f"build/GALE01/ssbm-{mode}100.iso"
    if out.exists():
        out.unlink()
    # APFS clone: near-zero disk until we write the changed DOL bytes.
    if subprocess.run(["cp", "-c", str(src_iso), str(out)]).returncode != 0:
        shutil.copy2(src_iso, out)

    base = (repo / "build/GALE01/main.dol").read_bytes()
    new = patched_dol.read_bytes()
    assert len(base) == len(new), "patched DOL changed size"
    with open(src_iso, "rb") as fh:
        fh.seek(0x420)
        dol_off = struct.unpack(">I", fh.read(4))[0]

    # Splice only the differing runs, so we can never touch the FST that
    # immediately follows the DOL payload on disc.
    written = runs = 0
    with open(out, "r+b") as fh:
        i, n = 0, len(base)
        while i < n:
            if base[i] == new[i]:
                i += 1
                continue
            j = i
            while j < n and base[j] != new[j]:
                j += 1
            fh.seek(dol_off + i)
            fh.write(new[i:j])
            written += j - i
            runs += 1
            i = j
    print(f"[{mode}] iso -> {out.relative_to(repo)} "
          f"({written} bytes spliced in {runs} runs at 0x{dol_off:X})")
    return out


# --------------------------------------------------------------------------- reports

def load_manifest(repo, mode):
    p = repo / f"build/GALE01/main-{mode}100.traps.json"
    if not p.exists():
        sys.exit(f"no manifest for mode {mode}; run: traprom.py {mode}")
    return json.loads(p.read_text())


def cmd_top(repo, mode, limit):
    man = load_manifest(repo, mode)
    by_unit = {}
    for m in man:
        u = by_unit.setdefault(m["unit"], {"bytes": 0, "fns": 0, "worst": 100.0})
        u["bytes"] += m["size"]
        u["fns"] += 1
        u["worst"] = min(u["worst"], m["match_percent"])
    print(f"{'trapped B':>10} {'fns':>4} {'worst%':>7}  unit")
    for name, u in sorted(by_unit.items(), key=lambda kv: -kv[1]["bytes"])[:limit]:
        print(f"{u['bytes']:>10} {u['fns']:>4} {u['worst']:>7.3f}  {name}")
    print(f"\n{'size':>10} {'match%':>7}  function")
    for m in sorted(man, key=lambda m: -m["size"])[:limit]:
        print(f"{m['size']:>10} {m['match_percent']:>7.3f}  {m['function']}"
              f"  [{m['unit']}]")


def cmd_lookup(repo, mode, values):
    man = load_manifest(repo, mode)
    for v in values:
        n = int(v, 0)
        hit, kind = None, "address"
        if n <= 0xFFFF and not v.lower().startswith("0x8"):
            hit, kind = next((m for m in man if m["id"] == n), None), "trap id"
        if hit is None:
            hit, kind = next((m for m in man
                              if m["address"] <= n < m["address"] + m["size"]),
                             None), "address"
        if hit is None:
            print(f"{v}: no trap here (that code is matched, or is data)")
            continue
        where = (f"0x{hit['address']:08X}+0x{n - hit['address']:X}"
                 if kind == "address" else f"0x{hit['address']:08X}")
        print(f"{v}: {kind} -> {hit['function']}  [{hit['unit']}]  {where}  "
              f"match {hit['match_percent']:.3f}%  trap id {hit['id']}")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["fn", "tu", "both", "top", "lookup"])
    ap.add_argument("values", nargs="*", help="addresses / trap ids for lookup")
    ap.add_argument("--repo", help="melee checkout (default $MELEE_REPO or ~/etc/melee)")
    ap.add_argument("--iso", help="source ISO (default <repo>/ssbm_rev2.iso)")
    ap.add_argument("--fill", choices=["entry", "body"], default="body",
                    help="entry: first instruction only; body: whole function (default)")
    ap.add_argument("--keep", action="append", default=[],
                    help="substring of a unit or function name to leave untouched")
    ap.add_argument("--no-keep-crash-handler", action="store_true")
    ap.add_argument("--no-iso", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--for", dest="which", choices=["fn", "tu"], default="fn",
                    help="which manifest top/lookup should read")
    args = ap.parse_args()
    repo = repo_root(args.repo)

    if args.mode == "top":
        return cmd_top(repo, args.which, args.limit)
    if args.mode == "lookup":
        return cmd_lookup(repo, args.which, args.values)

    if not (repo / "build/GALE01/report.json").exists() or \
       not (repo / "build/GALE01/main.dol").exists():
        sys.exit("run `ninja` in the melee repo first "
                 "(build/GALE01/{report.json,main.dol} missing)")
    keep = list(args.keep) + ([] if args.no_keep_crash_handler else DEFAULT_KEEP)
    src_iso = Path(args.iso).expanduser() if args.iso else repo / "ssbm_rev2.iso"

    for mode in (["fn", "tu"] if args.mode == "both" else [args.mode]):
        dol, _ = patch_dol(repo, mode, args.fill, keep)
        if not args.no_iso:
            build_iso(repo, mode, dol, src_iso)
        print()


if __name__ == "__main__":
    main()
