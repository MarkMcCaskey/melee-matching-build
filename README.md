# melee-matching-build

Builds a variant of Super Smash Bros. Melee (GALE01 rev 2) in which every
function the decomp has not byte-matched is overwritten with PowerPC trap
instructions, and splices the result into a bootable ISO. Booting it identifies
the unmatched function that execution reaches first, which orders matching work
by reachability rather than by byte count.

```sh
export MELEE_REPO=~/etc/melee   # a melee checkout with a green `ninja`

make -C trapbuild fn            # -> build/GALE01/ssbm-fn100.iso   sub-100% functions
make -C trapbuild tu            # -> build/GALE01/ssbm-tu100.iso   functions in unlinked TUs
make -C trapbuild both          # both of the above
```

| path | |
|---|---|
| [`trapbuild/`](trapbuild/) | the ROM builder: `traprom.py`, a Makefile and a usage reference. Standard library only. |
| `trapwatch.py` | boots a trap ISO under headless Dolphin and reports the crash. Requires a Dolphin built with the nogui frontend. |

## Background

dtk links a `NonMatching` translation unit from its extracted original object,
not from the compiled source. The C is still compiled — to
`build/GALE01/src/**.o`, for objdiff scoring — but the linker is handed
`build/GALE01/obj/**.o` instead. `build.ninja` records this per file:

```
# melee/lb/lbvector.c:    lb (Library) (linked True)    -> links build/GALE01/src/.../lbvector.o
# melee/lb/lbcollision.c: lb (Library) (linked False)   -> links build/GALE01/obj/.../lbcollision.o
```

`main.elf` currently links 1000 objects: 952 from `src/` and 59 from `obj/`.
`build/GALE01/main.dol` is therefore byte-identical to the retail DOL at any
level of progress.

The trap build starts from that complete ROM and overwrites the regions the
project has not reproduced, so the code that still executes is code the decomp
regenerates.

## Method

### Selection

`build/GALE01/report.json` supplies a `fuzzy_match_percent` per function and a
`complete` flag per unit. Two predicates (see [Modes](#modes)) turn that into a
list of functions, sorted by address and written to a manifest.

### Address translation

A DOL header is a section table of 18 entries — file offset, load address and
size, 7 text sections followed by 11 data sections — so a virtual address maps
to a file offset by lookup:

```
virtual 0x80006094  ->  DOL file offset 0x2C74  size 1892
```

### Patching

Each selected function is overwritten in place:

```
ORIGINAL  7C0802A6 90010004 9421FE78 DBE10180 DBC10178 ...
PATCHED   0FE00000 7FE00008 7FE00008 7FE00008 7FE00008 ...
```

Address and length are unchanged, so there is no relocation, layout change or
symbol resolution. Call sites are not modified; a `bl` branches to the same
address and executes the trap on arrival.

### ISO splice

The disc header holds the DOL's file offset at `0x420` (`0x1E800` on retail)
and the FST's at `0x424` (`0x456E00`, 32 bytes past the end of the DOL
payload). Since the patched DOL is the same length, only the differing byte
runs are written into that window and the filesystem is not touched:

```
[fn] iso -> ssbm-fn100.iso (279123 bytes spliced in 49786 runs at 0x1E800, clone)
```

The output ISO is a copy-on-write clone of the source (APFS `cp -c`, btrfs/XFS
`cp --reflink`), costing approximately zero bytes of disk per variant, with a
full-copy fallback on other filesystems.

### Detection

A trap raises a Program exception. `OS_ERROR_PROGRAM` is 6, and
`db_SetupCrashHandler` installs a handler for it — it skips only 4, 7, 8 and 9
— so the exception reaches melee's on-screen dump. The SDK's reporter also
prints it over OSReport:

```
- UNHANDLED EXCEPTION -------------------------------
DSISR=00000000 DAR=00000000
ERROR 6: (PROGRAM)
Trap program exception at 8038E034 (read from SRR0)
```

SRR0 is the faulting address. `make lookup AT=0x8038E034` resolves it against
the manifest to a function, unit, match percentage and trap id.

## Trap encoding

| word | encoding | role |
|---|---|---|
| `twi 31,r0,<id>` | `0x0FE0xxxx` | first instruction of the function |
| `tw 31,r0,r0` | `0x7FE00008` | fills the rest of the body |

`tw` compares `rA` against `rB` and traps if any condition in the 5-bit `TO`
field holds; `TO=31` sets all of them, so it traps unconditionally regardless
of operands. `tw 31,r0,r0` is the canonical spelling.

`twi` is the immediate form and traps identically, but its 16-bit `SIMM` field
is unused when `TO=31`, so the entry word carries its own index into the
manifest:

```
id   0  entry word 0x0FE00000  -> SIMM 0   = lbColl_80006094
id 100  entry word 0x0FE00064  -> SIMM 100 = mnStageSw_80236CBC
id 173  entry word 0x0FE000AD  -> SIMM 173 = hsd_803B6BE4
```

`.long 0` also faults, but carries no identifier.

## Modes

- **`fn`** traps every function scoring below 100%. Identifies the next
  function to match.
- **`tu`** traps every function in a unit that is not linked, matched or not.
  Identifies the next TU to link; its blockers are typically functions already
  at 100% held back by a sibling in the same file.

Provenance of the resulting ROMs, measured at 91.4% matched code:

| | functions | bytes | provenance |
|---|---|---|---|
| linked units | 17,491 | 2,926,404 | compiled from the decomp source |
| unlinked units, scored 100% | 2,160 | 621,292 | retail bytes, assumed identical |
| unlinked units, scored <100% | 178 | 334,336 | trapped in `fn` mode |

`tu` mode leaves 17,491 functions running, all of them linked from the decomp's
own objects. `fn` mode leaves 19,651 running, of which 2,160 (11%) are retail
bytes, left in place on the basis of objdiff's score.

## Limitations

- `fn` mode asserts rather than substitutes. The 2,160 functions above are
  retail bytes; objdiff comparing instruction encodings and relocation targets
  within a function is not equivalent to linking and running the compiled
  object. `tu` mode does not have this property.
- Function scoring does not cover link-time properties. A byte-matching object
  does not validate split-era symbol sizes, boundaries, names or scopes, which
  surface only when the TU is linked.
- Data is never trapped. Both modes touch only `.text`, and data follows the
  same 952/59 split: a linked TU contributes its own data, an unlinked TU
  contributes retail's. An incorrect `static const` in an unmatched TU
  therefore cannot affect the ROM, since that C is not in the binary. The
  project-wide gap appears in the report as `matched_data` 96.5% against
  `complete_data` 78.1%.
- Coverage is limited to code the run reaches. Booting to the title screen
  exercises the boot path only; menus, stages, characters and items must be
  driven to be covered. A run ending without a trap means nothing on that path
  was trapped, not that it is matched.
- `debugconsole_main` is kept unpatched by default. It is non-matching but owns
  `Exception_ReportCodeline`, so trapping it makes the reporter double-fault.
  This leaves 4 sub-100% functions running.
- Melee's debug console swallows some traps: it parks the faulting thread on
  the trap instruction and spawns a priority-0 console thread that spins on
  `VIGetRetraceCount`, so the ROM freezes with audio running and nothing is
  printed. Detecting those requires scanning the OS active-thread list for a
  saved SRR0 rather than reading the log.
- Whole-body fill assumes functions own their bytes. In melee the only
  non-function `.text` symbols are 11 OS exception-vector and RAS labels, none
  of which fall inside a trapped range in either mode. `--fill entry` patches
  only the first instruction.
- The build must reproduce the source disc. Before splicing, the ISO is checked
  for the `GALE01` game id and its DOL payload is compared against `main.dol`;
  a mismatch is refused, since the splice would combine two binaries.

## Usage

Full reference in [`trapbuild/README.md`](trapbuild/README.md).

```sh
make -C trapbuild check           # verify the inputs
make -C trapbuild top             # trapped bytes by unit and by function
make -C trapbuild lookup AT=0x803A00C0
make -C trapbuild clean
```

### trapwatch.py

```sh
python3 trapwatch.py control          # control: the unpatched ISO must not trap
python3 trapwatch.py fn --seconds 60
python3 trapwatch.py fn --iterate 5
```

`--iterate N` re-runs N times; after each hit it adds that function to
`traprom.py --keep`, rebuilds the ISO and boots again, producing the order in
which the game reaches them.

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

Logs are written to `<repo>/build/GALE01/trapwatch-<mode>*.log`. They contain
escape sequences from the nogui status line, so `grep -a` is required.

Requirements beyond the builder:

- A Dolphin with the nogui frontend, e.g.
  `~/etc/dolphin-dap/build/Binaries/dolphin-emu-nogui`; override with
  `--dolphin` or `DOLPHIN_NOGUI`. The stock macOS `Dolphin.app` does not work:
  it has no headless platform, its file logging does not emit, and its GDB stub
  is unreachable.
- Optionally `configure.py --map && ninja`, which produces
  `build/GALE01/main.elf.MAP`; `trapwatch.py` installs it into Dolphin's
  `Maps/` so Dolphin's own log names functions. SRR0 is otherwise decoded from
  `config/GALE01/symbols.txt`.

Detection reads Dolphin's log for the SDK's `Trap program exception at <SRR0>`
line rather than using breakpoints, which do not fire under the JIT. The GDB
connection serves one purpose: the `dolphin-dap` fork boots paused waiting for
a debug client, so `trapwatch` attaches to the stub and sends a single
`continue`. The stub does not honour `Dolphin.General.GDBPort` and selects an
ephemeral port, discovered via `lsof`. Without the resume, the emulator remains
at the entry point and every run resembles a clean boot.

## Recorded run

2026-08-22, master `e7844caa6`, 90.00% matched. Neither build booted; both
stopped in audio/HSD init within a second of `main`.

`fn`, in the order reached:

| # | function | unit | match |
|---|---|---|---|
| 1 | `HSD_AudioGetAuxHeapSize` | `sysdolphin/baselib/axdriver` | 86.000% |
| 2 | `gmMainLib_8015F600` | `melee/gm/gmmain_lib` | 96.970% |
| 3 | `HSD_SynthSFXSampleLoadCallback` | `sysdolphin/baselib/synth` | 93.368% |
| 4 | `gm_801A4014` | `melee/gm/gm_1A3F` | 97.857% |
| 5 | `Toy_80305058` | `melee/ty/toy` | 99.498% |

`tu`, all three already at 100% and held back only by their TU not being
linked:

| # | function | unit | match |
|---|---|---|---|
| 1 | `AXDriver_8038E498` | `sysdolphin/baselib/axdriver` | 100.000% |
| 2 | `HSD_SynthInit` | `sysdolphin/baselib/synth` | 100.000% |
| 3 | `HSD_AudioMalloc` | `sysdolphin/baselib/synth` | 100.000% |

As of 2026-08-26 all eight are resolved, and the `fn` build is down from 206
traps to 174. The list needs re-running.
