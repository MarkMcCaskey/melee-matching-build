# melee-matching-build

Builds a variant of Super Smash Bros. Melee (GALE01 rev 2) in which every
function the decomp has not byte-matched is overwritten with PowerPC traps, and
splices the result into a bootable ISO. Booting it identifies the unmatched
function that execution reaches first, ordering matching work by reachability
rather than by byte count.

```sh
export MELEE_REPO=~/src/melee        # melee checkout with a green `ninja`
export MELEE_ISO=~/ssbm_rev2.iso     # retail GALE01 rev 2 disc image

make -C trapbuild fn                 # -> $MELEE_REPO/build/GALE01/ssbm-fn100.iso
make -C trapbuild tu                 # -> $MELEE_REPO/build/GALE01/ssbm-tu100.iso
make -C trapbuild both

# or per invocation, without the environment
make -C trapbuild both REPO=~/src/melee ISO=~/ssbm_rev2.iso
```

`fn` traps every function below 100%; `tu` traps every function in an unlinked
TU. `MELEE_ISO` may be omitted if the image is at `$MELEE_REPO/ssbm_rev2.iso`.
Run `make -C trapbuild check` first to confirm both inputs are found.

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

`build/GALE01/main.dol` is therefore byte-identical to the retail DOL at any
level of progress. The trap build starts from that complete ROM and overwrites
the regions the project has not reproduced, so the code that still executes is
code the decomp regenerates.

## Method

**Selection.** `report.json` supplies a `fuzzy_match_percent` per function and
a `complete` flag per unit. Two predicates (see [Modes](#modes)) produce a list
of functions, sorted by address and written to a manifest.

**Address translation.** A DOL header is a section table of 18 entries — file
offset, load address and size, 7 text sections then 11 data — so a virtual
address maps to a file offset by lookup.

**Patching.** Each selected function is overwritten in place:

```
ORIGINAL  7C0802A6 90010004 9421FE78 DBE10180 ...
PATCHED   0FE00000 7FE00008 7FE00008 7FE00008 ...
```

Address and length are unchanged, so there is no relocation, layout change or
symbol resolution, and call sites are not modified: a `bl` branches to the same
address and executes the trap on arrival.

**ISO splice.** The disc header holds the DOL's file offset at `0x420` and the
FST's at `0x424`. Since the patched DOL is the same length, only the differing
byte runs are written into that window and the filesystem is not touched. The
output ISO is a copy-on-write clone of the source (APFS `cp -c`, btrfs/XFS
`cp --reflink`), with a full-copy fallback on other filesystems.

**Detection.** A trap raises a Program exception. `OS_ERROR_PROGRAM` is 6 and
`db_SetupCrashHandler` installs a handler for it, so it reaches melee's
on-screen dump and the SDK's OSReport output:

```
ERROR 6: (PROGRAM)
Trap program exception at 8038E034 (read from SRR0)
```

`make lookup AT=<SRR0>` resolves the address against the manifest to a
function, unit and match percentage.

## Trap encoding

| word | encoding | role |
|---|---|---|
| `twi 31,r0,<id>` | `0x0FE0xxxx` | first instruction of the function |
| `tw 31,r0,r0` | `0x7FE00008` | fills the rest of the body |

`TO=31` sets every trap condition, so both forms trap unconditionally
regardless of operands. `twi`'s 16-bit `SIMM` field is unused in that case, so
the entry word carries its own index into the manifest.

## Modes

- **`fn`** traps every function scoring below 100%. Identifies the next
  function to match.
- **`tu`** traps every function in a unit that is not linked, matched or not.
  Identifies the next TU to link; its blockers are typically functions already
  at 100% held back by a sibling in the same file.

## Limitations

- `fn` mode asserts rather than substitutes. A function scoring 100% inside a
  non-linked TU is left running as retail bytes — roughly a tenth of what
  executes. objdiff comparing instruction encodings and relocation targets
  within a function is not equivalent to linking and running the compiled
  object. `tu` mode leaves only linked objects running.
- Function scoring does not cover link-time properties. Split-era symbol sizes,
  boundaries, names and scopes surface only when the TU is linked.
- Data is never trapped. Both modes touch only `.text`, and data follows the
  same split: a linked TU contributes its own data, an unlinked TU contributes
  retail's. An incorrect `static const` in an unmatched TU cannot affect the
  ROM, since that C is not in the binary.
- Coverage is limited to code the run reaches. Booting to the title screen
  exercises the boot path only; menus, stages, characters and items must be
  driven to be covered. A run ending without a trap means nothing on that path
  was trapped, not that it is matched.
- `debugconsole_main` is kept unpatched by default. It is non-matching but owns
  `Exception_ReportCodeline`, so trapping it makes the reporter double-fault.
- Melee's debug console swallows some traps: it parks the faulting thread on
  the trap instruction and spawns a priority-0 console thread that spins on
  `VIGetRetraceCount`, so the ROM freezes with nothing printed. Detecting those
  requires scanning the OS active-thread list for a saved SRR0 rather than
  reading the log.
- Whole-body fill assumes functions own their bytes. In melee the only
  non-function `.text` symbols are a handful of OS exception-vector and RAS
  labels, none of which fall inside a trapped range. `--fill entry` patches
  only the first instruction.
- The build must reproduce the source disc. The ISO's game id and DOL payload
  are checked before splicing; a mismatch is refused.

## trapwatch.py

```sh
python3 trapwatch.py control          # control: the unpatched ISO must not trap
python3 trapwatch.py fn --seconds 60
python3 trapwatch.py fn --iterate 5
```

`--iterate N` re-runs N times; after each hit it adds that function to
`traprom.py --keep`, rebuilds the ISO and boots again, producing the order in
which the game reaches them. Logs are written to `<repo>/build/GALE01/` and
contain escape sequences from the nogui status line, so `grep -a` is required.

Requires a Dolphin with the nogui frontend, set via `--dolphin` or
`DOLPHIN_NOGUI`. The stock macOS `Dolphin.app` does not work: it has no
headless platform, its file logging does not emit, and its GDB stub is
unreachable. `configure.py --map && ninja` optionally produces
`main.elf.MAP`, which `trapwatch.py` installs into Dolphin's `Maps/`; SRR0 is
otherwise decoded from `config/GALE01/symbols.txt`.

Detection reads Dolphin's log for the SDK's exception line rather than using
breakpoints, which do not fire under the JIT. The GDB connection serves one
purpose: the `dolphin-dap` fork boots paused waiting for a debug client, so
`trapwatch` attaches and sends a single `continue`. The stub does not honour
`Dolphin.General.GDBPort` and selects an ephemeral port, discovered via `lsof`.
