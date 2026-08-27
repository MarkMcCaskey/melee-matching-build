# trapbuild

Overwrites every function the decomp has not byte-matched in
`build/GALE01/main.dol` with PowerPC traps and splices the result into a
bootable ISO.

```sh
export MELEE_REPO=~/src/melee        # melee checkout with a green `ninja`
export MELEE_ISO=~/ssbm_rev2.iso     # retail GALE01 rev 2 disc image

make check                           # confirm both inputs are found
make fn                              # -> $MELEE_REPO/build/GALE01/ssbm-fn100.iso
make tu                              # -> $MELEE_REPO/build/GALE01/ssbm-tu100.iso
make both

# or per invocation, without the environment
make both REPO=~/src/melee ISO=~/ssbm_rev2.iso
```

`fn` traps every function below 100%; `tu` traps every function in an unlinked
TU. `MELEE_ISO` may be omitted if the image is at `$MELEE_REPO/ssbm_rev2.iso`.

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
make top                         # trapped bytes by unit and by function
make lookup AT=0x803A00C0        # resolve a crash address
make clean
```

Additional variables: `FILL=entry|body`, `KEEP='name1 name2'`, `NO_ISO=1`,
`FORCE=1`, `LIMIT=20`, `FOR=fn|tu`, `BUILD_DIR=`, `PY=python3.12`.

The script runs standalone:

```sh
python3 traprom.py both
python3 traprom.py top --for fn
python3 traprom.py lookup --for fn 0x803A00C0
python3 traprom.py fn --keep gmMainLib_8015F600
```

## Output

Written to `<repo>/build/GALE01/`, or `--build-dir`:

| file | contents |
|---|---|
| `main-{fn,tu}100.dol` | the patched DOL |
| `ssbm-{fn,tu}100.iso` | bootable ISO |
| `main-{fn,tu}100.traps.txt` | manifest: id, address, size, match%, function, unit |
| `main-{fn,tu}100.traps.json` | the same, for tooling |

The entry word of each trapped function is `twi 31,r0,<id>` (`0x0FE0xxxx`),
where the id indexes the manifest; the body is filled with `tw 31,r0,r0`
(`0x7FE00008`).

## Granularity

- **`fn`** traps each function whose `fuzzy_match_percent` is below 100.
  Identifies the next function to match.
- **`tu`** traps every function in a unit that is not `Matching` in
  `configure.py`. Identifies the next TU to link; its blockers are typically
  functions already at 100% held back by a sibling in the same file.

The two make different claims: `tu` mode leaves only linked objects running,
while `fn` mode additionally leaves functions that score 100% inside non-linked
TUs, which execute as retail bytes. Neither mode traps data. See
[../README.md#limitations](../README.md#limitations).

## Behaviour

- `--keep debugconsole_main` is on by default. That TU is non-matching but owns
  `Exception_ReportCodeline` / `hsd_80397DA4`, so trapping it double-faults the
  reporter. `--no-keep-crash-handler` disables the exclusion.
- `--keep` also accepts function names, which is how a known blocker is stepped
  past: keep the function just hit, rebuild, boot again.
- Whole-body fill relies on functions owning their bytes, which holds in melee.
  `--fill entry` patches only the first instruction.
- Before splicing, the source ISO is checked for the `GALE01` game id and its
  DOL payload is compared against the build's `main.dol`; a mismatch is refused
  (`--force` overrides), as is a source ISO equal to the output path.
- Only differing DOL bytes are written, and the payload is checked to end
  before the FST, so the disc filesystem is not touched.
- The output ISO is a copy-on-write clone of the source (APFS `cp -c`,
  btrfs/XFS `cp --reflink`), with a full-copy fallback.

## Booting

The output is an ordinary ISO. `trapwatch.py` in the parent directory automates
it: boots under a nogui Dolphin, reads the exception line from the log, and
with `--iterate N` walks the blocker list by keeping each hit and rebuilding.
