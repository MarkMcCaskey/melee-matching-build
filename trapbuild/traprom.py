#!/usr/bin/env python3
"""Build a "matched-only" Melee: every function that is not byte-matched is
replaced with PowerPC traps, spliced into a bootable ISO.

    traprom.py both            # DOLs + ISOs for both granularities
    traprom.py fn              # function granularity only
    traprom.py tu              # TU granularity only
    traprom.py top --for fn    # what to match next, by trapped bytes
    traprom.py lookup --for fn 0x803A00C0    # SRR0 from a crash -> function
    traprom.py check           # are the inputs where we need them?
    traprom.py clean           # delete the generated DOLs, ISOs and manifests

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

The ISO is a copy-on-write clone of the source ISO (APFS `cp -c`, btrfs/XFS
`cp --reflink`) with only the changed DOL bytes spliced in, so each variant
costs a few hundred KB of real disk where the filesystem supports it.

Nothing outside the Python standard library is needed.
"""

import argparse
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path

TRAP = 0x7FE00008  # tw 31,r0,r0
TWI = 0x0FE00000   # twi 31,r0,SIMM
MAX_TRAPS = 0x10000  # the twi SIMM field is 16 bits

DOL_TEXT_SECTIONS = 7      # a DOL has 7 text sections then 11 data sections
DOL_SECTIONS = 18
ISO_DOL_OFFSET = 0x420     # disc header: file offset of main.dol
ISO_FST_OFFSET = 0x424

# The exception reporter must survive, or a trap double-faults instead of
# drawing the crash screen: this (non-matching) TU owns melee's
# fn_OSErrorHandler path -- Exception_ReportCodeline, hsd_80397DA4, ...
DEFAULT_KEEP = ["debugconsole_main"]

GAME_ID = b"GALE01"


class Fail(SystemExit):
    def __init__(self, msg):
        super().__init__(f"traprom: {msg}")


# --------------------------------------------------------------------- inputs

def find_repo(explicit=None):
    """--repo, then $MELEE_REPO, then the enclosing checkout, then ~/etc/melee."""
    if explicit or os.environ.get("MELEE_REPO"):
        p = Path(explicit or os.environ["MELEE_REPO"]).expanduser()
        if not (p / "configure.py").exists():
            raise Fail(f"{p} does not look like a melee checkout (no configure.py)")
        return p.resolve()
    for cand in [Path.cwd(), *Path.cwd().parents]:
        if (cand / "configure.py").exists() and (cand / "config/GALE01").exists():
            return cand.resolve()
    p = Path("~/etc/melee").expanduser()
    if (p / "configure.py").exists():
        return p.resolve()
    raise Fail("no melee checkout found; pass --repo or set MELEE_REPO")


def find_iso(repo, explicit=None):
    """--iso, then $MELEE_ISO, then ssbm_rev2.iso, then a lone *.iso in the repo."""
    if explicit or os.environ.get("MELEE_ISO"):
        p = Path(explicit or os.environ["MELEE_ISO"]).expanduser()
        if not p.exists():
            raise Fail(f"{p} does not exist")
        return p
    p = repo / "ssbm_rev2.iso"
    if p.exists():
        return p
    loose = sorted(x for x in repo.glob("*.iso") if not x.name.startswith("ssbm-"))
    if len(loose) == 1:
        return loose[0]
    return None


def build_dir(repo, explicit=None):
    return Path(explicit).expanduser() if explicit else repo / "build/GALE01"


# ------------------------------------------------------------------------ DOL

def dol_sections(data):
    """[(index, file_offset, load_address, size)] for the populated sections."""
    h = struct.unpack(">18I18I18I", data[:0xD8])
    off, addr, size = h[0:18], h[18:36], h[36:54]
    return [(i, off[i], addr[i], size[i]) for i in range(DOL_SECTIONS) if size[i]]


def dol_text_bytes(secs):
    return sum(s for i, _, _, s in secs if i < DOL_TEXT_SECTIONS)


def va_to_offset(secs, va, n):
    for _, off, addr, size in secs:
        if addr <= va and va + n <= addr + size:
            return off + (va - addr)
    return None


# -------------------------------------------------------------------- select

def collect(report, mode, keep):
    """[(vaddr, size, fn, unit, pct)] for every function to trap, by address."""
    out = []
    for u in report["units"]:
        meta = u["metadata"]
        if meta.get("module_id") not in (0, None):
            continue                       # main.dol only; RELs are not spliced
        linked = bool(meta.get("complete"))
        if any(k in u["name"] for k in keep):
            continue
        for f in u.get("functions") or []:
            pct = float(f["fuzzy_match_percent"])
            trap_it = pct < 100.0 if mode == "fn" else not linked
            if not trap_it:
                continue
            if any(k in f["name"] for k in keep):
                continue
            size = int(f["size"]) & ~3
            if size < 4:
                continue
            out.append((int(f["metadata"]["virtual_address"]), size, f["name"],
                        u["name"], pct))
    out.sort()
    return out


def patch_dol(repo, out, mode, fill, keep, quiet=False):
    report_path = out / "report.json"
    dol_path = out / "main.dol"
    for p in (report_path, dol_path):
        if not p.exists():
            raise Fail(f"{p} missing; run `ninja` in {repo} first")

    report = json.loads(report_path.read_text())
    data = bytearray(dol_path.read_bytes())
    secs = dol_sections(bytes(data))

    manifest, trapped, missing = [], 0, []
    for va, size, fn, unit, pct in collect(report, mode, keep):
        off = va_to_offset(secs, va, size)
        if off is None:
            missing.append(fn)
            continue
        tid = len(manifest)
        if tid >= MAX_TRAPS:
            raise Fail(f"more than {MAX_TRAPS} traps; the id will not fit in twi SIMM")
        n = size if fill == "body" else 4
        struct.pack_into(">I", data, off, TWI | tid)
        for i in range(4, n, 4):
            struct.pack_into(">I", data, off + i, TRAP)
        trapped += n
        manifest.append({"id": tid, "address": va, "size": size, "function": fn,
                         "unit": unit, "match_percent": pct})

    out_dol = out / f"main-{mode}100.dol"
    out_dol.write_bytes(bytes(data))
    text = dol_text_bytes(secs)
    say = (lambda *a: None) if quiet else print
    say(f"[{mode}] {len(manifest)} functions trapped, {trapped} bytes "
        f"({100.0 * trapped / text:.2f}% of .text)")
    if missing:
        say(f"[{mode}] WARNING: {len(missing)} functions not present in the DOL "
            f"({', '.join(missing[:3])}{'...' if len(missing) > 3 else ''})")
    write_manifest(out, mode, manifest, quiet)
    say(f"[{mode}] dol -> {out_dol}")
    return out_dol, manifest


def write_manifest(out, mode, manifest, quiet=False):
    txt = out / f"main-{mode}100.traps.txt"
    with open(txt, "w") as fh:
        fh.write("# id     address     size  match%   function  [unit]\n")
        for m in manifest:
            fh.write("%-6d 0x%08X %6d %7.3f  %s  [%s]\n" %
                     (m["id"], m["address"], m["size"], m["match_percent"],
                      m["function"], m["unit"]))
    (out / f"main-{mode}100.traps.json").write_text(json.dumps(manifest, indent=1))
    if not quiet:
        print(f"[{mode}] manifest -> {txt} (+ .json)")


# ------------------------------------------------------------------------ ISO

def clone(src, dst):
    """Copy-on-write where the filesystem offers it, else a plain copy."""
    flag = "-c" if platform.system() == "Darwin" else "--reflink=auto"
    try:
        r = subprocess.run(["cp", flag, str(src), str(dst)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and dst.exists():
            return "clone"
    except OSError:
        pass
    shutil.copy2(src, dst)
    return "copy"


def read_iso_header(src_iso):
    with open(src_iso, "rb") as fh:
        game_id = fh.read(6)
        fh.seek(ISO_DOL_OFFSET)
        dol_off = struct.unpack(">I", fh.read(4))[0]
        fst_off = struct.unpack(">I", fh.read(4))[0]
    return game_id, dol_off, fst_off


def build_iso(out, mode, base_dol, patched_dol, src_iso, force=False, quiet=False):
    say = (lambda *a: None) if quiet else print
    if src_iso is None or not src_iso.exists():
        say(f"[{mode}] no source ISO; skipping (pass --iso or set MELEE_ISO)")
        return None

    out_iso = out / f"ssbm-{mode}100.iso"
    if out_iso.resolve() == src_iso.resolve():
        raise Fail(f"the source ISO is the output ISO ({out_iso.name}); "
                   "point --iso at an unmodified disc image")

    base = base_dol.read_bytes()
    new = patched_dol.read_bytes()
    if len(base) != len(new):
        raise Fail("patched DOL changed size")

    game_id, dol_off, fst_off = read_iso_header(src_iso)
    if game_id != GAME_ID and not force:
        raise Fail(f"{src_iso} is {game_id.decode('latin1')!r}, not "
                   f"{GAME_ID.decode()} (--force to splice anyway)")
    if dol_off + len(base) > fst_off:
        raise Fail(f"DOL payload at 0x{dol_off:X} would overrun the FST "
                   f"at 0x{fst_off:X}")
    with open(src_iso, "rb") as fh:
        fh.seek(dol_off)
        on_disc = fh.read(len(base))
    if on_disc != base and not force:
        raise Fail(f"the DOL on {src_iso.name} differs from {base_dol.name}; "
                   "the build must reproduce this ISO's DOL exactly, or the "
                   "splice would mix two binaries (--force to override)")

    if out_iso.exists():
        out_iso.unlink()
    how = clone(src_iso, out_iso)

    # Splice only the differing runs, so we can never touch the FST that
    # immediately follows the DOL payload on disc.
    written = runs = 0
    with open(out_iso, "r+b") as fh:
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
    say(f"[{mode}] iso -> {out_iso} ({written} bytes spliced in {runs} runs "
        f"at 0x{dol_off:X}, {how})")
    return out_iso


# -------------------------------------------------------------------- reports

def load_manifest(out, mode):
    p = out / f"main-{mode}100.traps.json"
    if not p.exists():
        raise Fail(f"no manifest for mode {mode}; run: traprom.py {mode}")
    return json.loads(p.read_text())


def cmd_top(out, mode, limit):
    man = load_manifest(out, mode)
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


def cmd_lookup(out, mode, values):
    man = load_manifest(out, mode)
    if not values:
        raise Fail("lookup needs an address or a trap id")
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
        off = n - hit["address"] if kind == "address" else 0
        where = f"0x{hit['address']:08X}" + (f"+0x{off:X}" if off else "")
        print(f"{v}: {kind} -> {hit['function']}  [{hit['unit']}]  {where}  "
              f"match {hit['match_percent']:.3f}%  trap id {hit['id']}")


def cmd_clean(out):
    n = 0
    for pat in ("main-*100.dol", "main-*100.traps.txt", "main-*100.traps.json",
                "ssbm-*100.iso"):
        for p in sorted(out.glob(pat)):
            print(f"  rm {p.name}")
            p.unlink()
            n += 1
    print(f"removed {n} file(s) from {out}")
    return 0


def cmd_check(repo, out, src_iso):
    ok = True

    def line(good, what, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  {'ok  ' if good else 'MISS'}  {what}{'  ' + detail if detail else ''}")

    print(f"repo     {repo}")
    print(f"build    {out}")
    line((repo / "configure.py").exists(), "melee checkout")
    line((out / "report.json").exists(), "build/GALE01/report.json", "(needs `ninja`)")
    line((out / "main.dol").exists(), "build/GALE01/main.dol", "(needs `ninja`)")
    if src_iso is None:
        line(False, "source ISO", "(pass --iso or set MELEE_ISO)")
    else:
        gid = read_iso_header(src_iso)[0] if src_iso.exists() else b""
        line(src_iso.exists() and gid == GAME_ID, f"source ISO {src_iso.name}",
             gid.decode("latin1", "replace"))
    if (out / "report.json").exists():
        report = json.loads((out / "report.json").read_text())
        for mode in ("fn", "tu"):
            n = len(collect(report, mode, DEFAULT_KEEP))
            print(f"  ....  {mode}: {n} functions would be trapped")
    return 0 if ok else 1


# ----------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="traprom.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["fn", "tu", "both", "top", "lookup", "check",
                                     "clean"])
    ap.add_argument("values", nargs="*", help="addresses / trap ids for lookup")
    ap.add_argument("--repo", help="melee checkout "
                    "(default: $MELEE_REPO, the enclosing checkout, or ~/etc/melee)")
    ap.add_argument("--iso", help="source ISO "
                    "(default: $MELEE_ISO or <repo>/ssbm_rev2.iso)")
    ap.add_argument("--build-dir", help="where main.dol/report.json live and "
                    "outputs go (default <repo>/build/GALE01)")
    ap.add_argument("--fill", choices=["entry", "body"], default="body",
                    help="entry: first instruction only; body: whole function (default)")
    ap.add_argument("--keep", action="append", default=[], metavar="NAME",
                    help="substring of a unit or function name to leave untouched")
    ap.add_argument("--no-keep-crash-handler", action="store_true",
                    help=f"also trap {DEFAULT_KEEP[0]} (traps then double-fault "
                         "instead of reporting)")
    ap.add_argument("--no-iso", action="store_true", help="patch the DOL only")
    ap.add_argument("--force", action="store_true",
                    help="splice even if the ISO's game id or DOL does not match")
    ap.add_argument("--quiet", "-q", action="store_true")
    ap.add_argument("--limit", type=int, default=20, help="rows for `top`")
    ap.add_argument("--for", dest="which", choices=["fn", "tu"], default="fn",
                    help="which manifest top/lookup should read")
    args = ap.parse_args(argv)

    repo = find_repo(args.repo)
    out = build_dir(repo, args.build_dir)
    src_iso = find_iso(repo, args.iso)

    if args.mode == "clean":
        return cmd_clean(out)
    if args.mode == "check":
        return cmd_check(repo, out, src_iso)
    if args.mode == "top":
        return cmd_top(out, args.which, args.limit)
    if args.mode == "lookup":
        return cmd_lookup(out, args.which, args.values)

    keep = list(args.keep) + ([] if args.no_keep_crash_handler else DEFAULT_KEEP)
    for mode in (["fn", "tu"] if args.mode == "both" else [args.mode]):
        dol, _ = patch_dol(repo, out, mode, args.fill, keep, args.quiet)
        if not args.no_iso:
            build_iso(out, mode, out / "main.dol", dol, src_iso, args.force,
                      args.quiet)
        if not args.quiet:
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
