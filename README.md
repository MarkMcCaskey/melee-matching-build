# melee-trapbuild

Build a "matched-only" Super Smash Bros. Melee: take the decomp's byte-identical
`main.dol` and replace every function that is **not** byte-matched with PowerPC
traps, splice it into a bootable ISO, then boot it headless and report the first
unmatched function the game actually reaches.

The point is to prioritise matching work by *execution*, not by byte count: the
tool answers "which unmatched function is standing between me and a booting
100%-matched build", and `--iterate` turns that into an ordered list.

```
                report.json ─┐
                             ├─ traprom.py ─→ main-fn100.dol ─→ ssbm-fn100.iso
                 main.dol ───┘                 + traps.json          │
                                                                     ▼
                                                              trapwatch.py
                                                                     │
                                       "ERROR 6 (PROGRAM) at 8038E034"
                                                                     │
                                              HSD_AudioGetAuxHeapSize (86.0%)
```

## Requirements

- A melee checkout with a **green `ninja`** — the tools read
  `build/GALE01/report.json` (objdiff's per-function scores, generated with
  `functionRelocDiffs=data_value`, the same scoring as the PR bot) and
  `build/GALE01/main.dol`. Point at it with `--repo` or `MELEE_REPO`
  (default `~/etc/melee`).
- `ssbm_rev2.iso` in the repo root (or `--iso`).
- For `trapwatch.py`: a Dolphin with the **nogui frontend**, e.g.
  `~/etc/dolphin-dap/build/Binaries/dolphin-emu-nogui`. Override with
  `--dolphin` or `DOLPHIN_NOGUI`. The stock macOS `Dolphin.app` will not work:
  it has no headless platform, its file logging never emits, and its GDB stub
  is not reachable.
- Optional: `configure.py --map && ninja` produces `build/GALE01/main.elf.MAP`,
  which `trapwatch.py` installs into Dolphin's `Maps/` so Dolphin's own log
  names functions. Not required — SRR0 is decoded from `config/GALE01/symbols.txt`.

## traprom.py — build the trap ROM

```sh
export MELEE_REPO=~/etc/melee
python3 traprom.py both              # DOLs + ISOs for both granularities
python3 traprom.py fn                # function granularity only
python3 traprom.py top --for fn      # what to match next, by trapped bytes
python3 traprom.py lookup --for fn 0x803A00C0   # SRR0 -> function
```

Outputs land in `<repo>/build/GALE01/`:

| file | what |
|---|---|
| `main-{fn,tu}100.dol` | the patched DOL |
| `ssbm-{fn,tu}100.iso` | bootable ISO |
| `main-{fn,tu}100.traps.txt` | grep-able manifest: id, address, size, match%, function, unit |
| `main-{fn,tu}100.traps.json` | same, for tooling |

### Granularity

- **`fn`** — trap each function whose `fuzzy_match_percent` < 100.
  Currently 206 functions, 385 KB, ~10% of `.text`.
  Answers *"which function do I match next"*.
- **`tu`** — trap every function in a TU that is not `Matching` in
  `configure.py`, i.e. not linked from our own object, matched or not.
  Currently 2535 functions, 1008 KB, ~26% of `.text`.
  Answers *"which TU do I need to finish linking next"*. Its blockers are
  usually functions that are *already* at 100%, held back by a sibling.

### Trap encoding

```
entry word   twi 31,r0,<id>    0x0FE0xxxx    id = index into the manifest
body fill    trap              0x7FE00008    tw 31,r0,r0
```

`trap` is PowerPC's `ud2`: an unconditional `tw` that always raises a Program
exception. `twi 31,r0,SIMM` traps identically but leaves 16 free immediate bits,
so the entry word carries its own id. (`.long 0` also faults, but carries
nothing.)

`OS_ERROR_PROGRAM` is 6, and melee's `db_SetupCrashHandler` installs a handler
for it (it skips only 4/7/8/9), so a trap reaches the on-screen exception dump.
The SDK's reporter also prints it over OSReport:

```
- UNHANDLED EXCEPTION -------------------------------
DSISR=00000000 DAR=00000000
ERROR 6: (PROGRAM)
Trap program exception at 8038E034 (read from SRR0)
```

### Notes on correctness

- `--keep debugconsole_main` is **on by default**: that TU is non-matching but
  owns `Exception_ReportCodeline` / `hsd_80397DA4`, so trapping it double-faults
  the reporter instead of reporting. `--no-keep-crash-handler` disables this.
- Whole-body fill is safe: the only non-function `.text` symbols in melee are
  11 OS exception-vector / RAS labels, and none fall inside a trapped range.
  `--fill entry` patches just the first instruction if you want to be careful.
- The ISO is an APFS clone (`cp -c`) of the source with **only the differing DOL
  bytes** spliced at the disc's DOL offset (0x1E800), so each variant costs a
  few hundred KB of real disk rather than 1.4 GB. The FST at 0x456E00 sits 0x20
  bytes past the end of the DOL payload and is never touched.

## trapwatch.py — boot it and find the blocker

```sh
python3 trapwatch.py control            # sanity: unpatched ISO must NOT trap
python3 trapwatch.py fn --seconds 60
python3 trapwatch.py tu --seconds 60
python3 trapwatch.py fn --iterate 5     # ordered list of boot blockers
```

`--iterate N` re-runs N times; after each hit it adds that function to
`traprom.py --keep`, rebuilds the ISO, and boots again — so you get the order
the game would hit them, not just the first.

```
=== fn run 1/5 (206 traps, 60s limit) ===
  resumed via gdb stub on port 53025
  TRAPPED: ERROR 6 (PROGRAM)
  SRR0 0x8038E034 -> HSD_AudioGetAuxHeapSize
  HSD_AudioGetAuxHeapSize  [main/sysdolphin/baselib/axdriver]  size 728  match 86.000%
  caller chain (LR save):
    80028508  lbAudioAx_8002838C+0x17C
    8015FFAC  main+0xF8
    8000533C  0x8000533C
```

Logs are kept at `<repo>/build/GALE01/trapwatch-<mode>*.log`. They contain
binary escape sequences from the nogui status line, so `grep -a` them.

### How it detects the trap

By reading Dolphin's log for the SDK's `Trap program exception at <SRR0>` line,
not by breakpoints — **Dolphin's GDB-stub breakpoints do not fire under the
JIT**, which cost an hour to discover. The GDB connection is used for exactly
one thing: the `dolphin-dap` fork boots **paused** waiting for a debug client,
so `trapwatch` attaches to the stub (on an ephemeral port it discovers via
`lsof`, since `Dolphin.General.GDBPort` is not honoured) and sends a single
`continue`. Without that, the emulator sits at the entry point and every run
looks like a clean boot.

## Results as of 2026-08-22 (master @ e7844caa6, 90.00% matched)

Neither build boots — both die in audio/HSD init, within a second of `main`.

`fn` — real match targets, in the order the game hits them:

| # | function | unit | match |
|---|---|---|---|
| 1 | `HSD_AudioGetAuxHeapSize` | `sysdolphin/baselib/axdriver` | 86.000% |
| 2 | `gmMainLib_8015F600` | `melee/gm/gmmain_lib` | 96.970% |
| 3 | `HSD_SynthSFXSampleLoadCallback` | `sysdolphin/baselib/synth` | 93.368% |
| 4 | `gm_801A4014` | `melee/gm/gm_1A3F` | 97.857% |
| 5 | `Toy_80305058` | `melee/ty/toy` | 99.498% |

`tu` — all three blockers are *already 100%*, held back only by their TU not
being linked, which is the distinction the two modes exist to show:

| # | function | unit | match |
|---|---|---|---|
| 1 | `AXDriver_8038E498` | `sysdolphin/baselib/axdriver` | 100.000% |
| 2 | `HSD_SynthInit` | `sysdolphin/baselib/synth` | 100.000% |
| 3 | `HSD_AudioMalloc` | `sysdolphin/baselib/synth` | 100.000% |

Both roads out of the boot start at **axdriver** and **synth**.
