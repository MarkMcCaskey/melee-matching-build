# trapbuild

Overwrites every function the decomp has not byte-matched in
`build/GALE01/main.dol` with PowerPC traps and splices the result into a
bootable ISO. Booting it identifies the unmatched function that execution
reaches first.

```sh
export MELEE_REPO=~/etc/melee   # a melee checkout with a green `ninja`

make fn                         # -> build/GALE01/ssbm-fn100.iso   sub-100% functions
make tu                         # -> build/GALE01/ssbm-tu100.iso   functions in unlinked TUs
make both                       # both of the above
```

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
                                       psDispParticles  [psdisp]  98.676%
```

Background, method and limitations: [../README.md](../README.md).

## Requirements

- Python 3.8+, standard library only.
- A [doldecomp/melee](https://github.com/doldecomp/melee) checkout with a green
  `ninja`, which produces `build/GALE01/report.json` (objdiff scoring, with
  `functionRelocDiffs=data_value`) and `build/GALE01/main.dol`.
- A retail `GALE01` rev 2 disc image whose DOL the build reproduces exactly;
  `traprom.py` verifies this before splicing.

The checkout is resolved from `--repo`, then `$MELEE_REPO`, then the directory
`traprom.py` runs in, then `~/etc/melee`. The ISO is resolved from `--iso`,
then `$MELEE_ISO`, then `<repo>/ssbm_rev2.iso`, then a lone `*.iso` in the
checkout.

## Use

```sh
make check                       # verify the inputs
make top                         # trapped bytes by unit and by function
make lookup AT=0x803A00C0        # resolve a crash address
make clean
```

Other locations: `make fn REPO=~/src/melee ISO=~/isos/ssbm.iso`, or export
`MELEE_REPO`. Additional variables: `FILL=entry|body`, `KEEP='name1 name2'`,
`NO_ISO=1`, `FORCE=1`, `LIMIT=20`, `FOR=fn|tu`, `PY=python3.12`.

The script runs standalone:

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

Written to `<repo>/build/GALE01/`, or `--build-dir`:

| file | contents |
|---|---|
| `main-{fn,tu}100.dol` | the patched DOL |
| `ssbm-{fn,tu}100.iso` | bootable ISO |
| `main-{fn,tu}100.traps.txt` | manifest: id, address, size, match%, function, unit |
| `main-{fn,tu}100.traps.json` | the same, for tooling |

## Granularity

- **`fn`** traps each function whose `fuzzy_match_percent` is below 100.
  Identifies the next function to match.
- **`tu`** traps every function in a unit that is not `Matching` in
  `configure.py`. Identifies the next TU to link; its blockers are typically
  functions already at 100% held back by a sibling in the same file.

The two make different claims. `tu` mode leaves only linked objects running.
`fn` mode additionally leaves functions that score 100% inside non-linked TUs,
which execute as retail bytes — 2160 functions, 11% of what runs, at 91.4%
matched code. Neither mode traps data. See
[../README.md#limitations](../README.md#limitations).

## Trap encoding

```
entry word   twi 31,r0,<id>    0x0FE0xxxx    id = index into the manifest
body fill    trap              0x7FE00008    tw 31,r0,r0
```

`TO=31` sets every trap condition, so both forms trap unconditionally
regardless of operands. `twi`'s 16-bit `SIMM` field is unused in that case, so
the entry word carries its own manifest index.

A trap raises a Program exception. `OS_ERROR_PROGRAM` is 6 and
`db_SetupCrashHandler` installs a handler for it (skipping only 4, 7, 8 and 9),
so it reaches melee's on-screen dump and the SDK's OSReport output:

```
- UNHANDLED EXCEPTION -------------------------------
DSISR=00000000 DAR=00000000
ERROR 6: (PROGRAM)
Trap program exception at 8038E034 (read from SRR0)
```

`make lookup AT=0x8038E034` resolves SRR0 against the manifest.

## Behaviour

- `--keep debugconsole_main` is on by default. That TU is non-matching but owns
  `Exception_ReportCodeline` / `hsd_80397DA4`, so trapping it double-faults the
  reporter. `--no-keep-crash-handler` disables the exclusion.
- `--keep` also accepts function names, which is how a known blocker is stepped
  past: keep the function just hit, rebuild, boot again.
- Whole-body fill relies on functions owning their bytes. In melee the only
  non-function `.text` symbols are 11 OS exception-vector and RAS labels, none
  of which fall inside a trapped range. `--fill entry` patches only the first
  instruction.
- Before splicing, the source ISO is checked for the `GALE01` game id and its
  DOL payload is compared against the build's `main.dol`; a mismatch is refused
  (`--force` overrides), as is a source ISO equal to the output path.
- Only differing DOL bytes are written, and the payload is checked to end
  before the FST (32 bytes later on retail), so the disc filesystem is not
  touched.
- The output ISO is a copy-on-write clone of the source (APFS `cp -c`,
  btrfs/XFS `cp --reflink`), with a full-copy fallback on other filesystems.

## Booting

The output is an ordinary ISO. `trapwatch.py` in the parent directory automates
it: boots under a nogui Dolphin, reads the exception line from the log, and
with `--iterate N` walks the blocker list by keeping each hit and rebuilding.
It requires a Dolphin built with the nogui frontend; this builder does not.
