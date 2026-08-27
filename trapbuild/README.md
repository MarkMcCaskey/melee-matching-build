# trapbuild — a matched-only Melee ROM

Take the decomp's byte-identical `main.dol`, replace every function that is
**not** byte-matched with PowerPC traps, and splice the result into a bootable
ISO. Boot it and the game dies at the first unmatched function it actually
reaches.

The point is to prioritise matching work by *execution*, not by byte count: it
answers "which unmatched function is standing between me and a booting
100%-matched build".

```
                report.json ─┐
                             ├─ traprom.py ─→ main-fn100.dol ─→ ssbm-fn100.iso
                 main.dol ───┘                 + traps.json          │
                                                                     ▼
                                                            boot it in Dolphin
                                                                     │
                                       "ERROR 6 (PROGRAM) at 803A00C0"
                                                                     │
                                       traprom.py lookup 0x803A00C0
                                                                     │
                                              HSD_SynthSFXSampleLoadCallback
```

## Requirements

- Python 3.8+. **Standard library only** — nothing to install.
- A [doldecomp/melee](https://github.com/doldecomp/melee) checkout with a green
  `ninja`, which produces the two inputs:
  - `build/GALE01/report.json` — objdiff's per-function scores, generated with
    `functionRelocDiffs=data_value` (the same scoring as the PR bot)
  - `build/GALE01/main.dol`
- A retail `GALE01` (rev 2) disc image. The build's `main.dol` must reproduce
  the one on that disc byte for byte; `traprom.py` checks this before splicing.

`traprom.py` finds the checkout from `--repo`, then `$MELEE_REPO`, then the
directory it is run in, then `~/etc/melee`. The ISO comes from `--iso`, then
`$MELEE_ISO`, then `<repo>/ssbm_rev2.iso`, then a lone `*.iso` in the checkout.

## Use

```sh
make check                       # are the inputs where we need them?
make fn                          # trap every function below 100%
make tu                          # trap every function in an unlinked TU
make both
make top                         # what to match next, by trapped bytes
make lookup AT=0x803A00C0        # decode a crash address
make clean
```

Point it somewhere else with `make fn REPO=~/src/melee ISO=~/isos/ssbm.iso`, or
export `MELEE_REPO`. Other knobs: `FILL=entry|body`, `KEEP='name1 name2'`,
`NO_ISO=1`, `FORCE=1`, `LIMIT=20`, `FOR=fn|tu`, `PY=python3.12`.

The script stands alone if you would rather skip make:

```sh
python3 traprom.py both
python3 traprom.py top --for fn
python3 traprom.py lookup --for fn 0x803A00C0 0x8038E034 42
python3 traprom.py fn --keep gmMainLib_8015F600 --keep debugconsole_main
```

```
$ make fn
[fn] 174 functions trapped, 331512 bytes (8.54% of .text)
[fn] manifest -> build/GALE01/main-fn100.traps.txt (+ .json)
[fn] dol -> build/GALE01/main-fn100.dol
[fn] iso -> build/GALE01/ssbm-fn100.iso (279123 bytes spliced in 49786 runs at 0x1E800, clone)
```

## Output

Everything lands in `<repo>/build/GALE01/` (override with `--build-dir`):

| file | what |
|---|---|
| `main-{fn,tu}100.dol` | the patched DOL |
| `ssbm-{fn,tu}100.iso` | bootable ISO |
| `main-{fn,tu}100.traps.txt` | grep-able manifest: id, address, size, match%, function, unit |
| `main-{fn,tu}100.traps.json` | same, for tooling |

## Granularity

- **`fn`** — trap each function whose `fuzzy_match_percent` is below 100.
  Answers *"which function do I match next"*.
- **`tu`** — trap every function in a TU that is not `Matching` in
  `configure.py`, i.e. not linked from our own object, matched or not.
  Answers *"which TU do I need to finish linking next"*. Its blockers are
  usually functions that are *already* at 100%, held back by a sibling.

## What survives, and what that proves

The two modes make very different claims, and it matters:

- **`tu` mode is physically true.** Everything still running is your own
  compiled, linked output.
- **`fn` mode asserts.** A function scoring 100% inside a *non-linked* TU is
  left running as **retail's bytes** — your compile of it was never linked, so
  the ROM is trusting objdiff's word. At 91.4% matched that is 2160 functions,
  11% of what still executes.

Also: **data is never trapped.** Both modes only touch `.text`, so a wrong
`static const` in an unmatched TU cannot make this ROM crash — that C isn't in
the binary at all. And a run with no trap means "nothing on the path the game
took", not "matched".

The parent [README](../README.md#limitations) has the full list.

## Trap encoding

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

Feed that address back in: `make lookup AT=0x8038E034`.

## Notes on correctness

- `--keep debugconsole_main` is **on by default**: that TU is non-matching but
  owns `Exception_ReportCodeline` / `hsd_80397DA4`, so trapping it double-faults
  the reporter instead of reporting. `--no-keep-crash-handler` disables this.
- `--keep` also takes ordinary function names, which is how you step past a
  blocker: keep the one you just hit, rebuild, boot again, and you get the next.
- Whole-body fill is safe: the only non-function `.text` symbols in melee are
  11 OS exception-vector / RAS labels, and none fall inside a trapped range.
  `--fill entry` patches just the first instruction if you want to be careful.
- Before splicing, the source ISO is checked for the `GALE01` game id, and its
  DOL payload is compared against the build's `main.dol`. A mismatch means the
  build does not reproduce that disc and the splice would mix two binaries, so
  it is refused (`--force` overrides).
- Only the *differing* DOL bytes are written, and the DOL payload is checked to
  end before the FST (0x20 bytes later on retail), so the disc's filesystem is
  never touched.
- The output ISO is a copy-on-write clone of the source (APFS `cp -c`,
  btrfs/XFS `cp --reflink`), so each variant costs a few hundred KB of real
  disk. On other filesystems it falls back to a full copy.

## Booting it

Any emulator or console will do — the output is an ordinary ISO. To do it
headlessly and automatically, `trapwatch.py` in the parent directory boots the
ISO under a nogui Dolphin, reads the SDK's exception line out of the log, and
`--iterate N` walks the whole blocker list by keeping each hit and rebuilding.
That one needs a Dolphin built with the nogui frontend; this build tool does
not.
