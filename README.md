# melee-matching-build

Build a version of Super Smash Bros. Melee in which every function the decomp
has **not** byte-matched is replaced with a crash instruction, then boot it and
see how far the game gets. The first crash names the unmatched function that is
actually standing between you and a working 100%-matched build.

| | |
|---|---|
| **[`trapbuild/`](trapbuild/)** | the ROM builder. Portable, standard library only, `make fn` and you have a trap ISO. **Start here** — it is also the part that is meant to be shared. |
| `trapwatch.py` | boots a trap ISO under headless Dolphin, reads the crash out of the log, and `--iterate N` walks the whole blocker list. Needs a Dolphin with the nogui frontend. |

Everything else in this directory is untracked scratch: input-driving and
scene-scanning experiments (`trapdrive`, `vsdrive`, `vsscan`, `menuscan`,
`menuiter`, `attractscan`) built against a local `dolphin-dap` /
`dolphin-decomp-mcp` pair, with hardcoded paths. They are not part of the
package and are gitignored.

---

# How it works

## The problem

A decompilation project rewrites a shipped binary as C that compiles back to
the *same bytes*. Progress is measured in bytes matched — melee is at ~91% of
`.text` as I write this. But that percentage says nothing about **which**
functions matter. You can spend a week matching a 15 KB function that runs
once on the results screen, while a 200-byte function in audio init blocks the
game from booting at all.

This tool reorders the work by execution. Break everything that isn't matched,
boot it, and see what the game hits first.

## Background: the decomp build never fails

The thing that makes this cheap is a property of how dtk-based decomps link.
`configure.py` marks each translation unit:

```
Matching    = True   # Object matches and should be linked
NonMatching = False  # Object does not match and should not be linked
```

For a `NonMatching` TU, your C is still compiled — but only so objdiff can
score it. The **linker is handed dtk's object extracted from the retail DOL
instead**. `build.ninja` spells this out per file:

```
# melee/lb/lbvector.c:    lb (Library) (linked True)    -> links build/GALE01/src/.../lbvector.o
# melee/lb/lbcollision.c: lb (Library) (linked False)   -> links build/GALE01/obj/.../lbcollision.o
```

`src/**.o` is yours, `obj/**.o` is theirs. Right now `main.elf` links **1000
objects: 952 from `src/`, 59 from `obj/`**.

The consequence: `build/GALE01/main.dol` is byte-identical to the retail DOL
whether the project is at 10% or 99%. Verified — the build's DOL compares equal
to the one on the disc.

So there is nothing to link here, and nothing to copy in. We start from a
complete, working ROM and **destroy the parts we can't yet claim to have
reproduced**. Whatever still runs is code the decomp genuinely regenerates.

## Building the trap ROM

Five steps, all in [`trapbuild/traprom.py`](trapbuild/traprom.py):

**1. Decide what to break.** `build/GALE01/report.json` is objdiff's scoring
output — a `fuzzy_match_percent` per function and a `complete` flag per unit.
Two predicates (see *Modes* below) turn that into a list of functions.

**2. Map virtual address to file offset.** The manifest has addresses like
`0x80006094`; we need a byte offset into the DOL. A DOL header is a section
table — 18 entries of (file offset, load address, size), 7 text then 11 data —
so it's a lookup:

```
virtual 0x80006094  ->  DOL file offset 0x2C74  size 1892
```

**3. Overwrite the function in place.**

```
ORIGINAL  7C0802A6 90010004 9421FE78 DBE10180 DBC10178 ...   mflr/stw/stwu/stfd...
PATCHED   0FE00000 7FE00008 7FE00008 7FE00008 7FE00008 ...
```

Same address, same length. Nothing moves, so there is no relocation, no layout
change, no symbol resolution — and call sites are untouched. The `bl` still
branches to the same address and hits the trap on arrival.

**4. Splice the DOL into the ISO.** The disc header holds the DOL's file offset
at `0x420` (`0x1E800` on retail) and the FST's at `0x424` (`0x456E00`, i.e. 32
bytes past the end of the DOL payload). Because the patched DOL is exactly the
same length, only the differing byte runs are written into that window and the
disc's filesystem is never touched:

```
[fn] iso -> ssbm-fn100.iso (279123 bytes spliced in 49786 runs at 0x1E800, clone)
```

The output is a copy-on-write clone of the source ISO (APFS `cp -c`, btrfs/XFS
`cp --reflink`), so each 1.3 GB variant costs ~0 bytes of real disk.

**5. Boot it and read the crash.** Below.

## The trap instruction

Two PowerPC words, both of which unconditionally raise a Program exception:

| word | encoding | role |
|---|---|---|
| `twi 31,r0,<id>` | `0x0FE0xxxx` | first instruction of the function |
| `tw 31,r0,r0` | `0x7FE00008` | fills the rest of the body |

`tw` is "trap word": compare `rA` against `rB` and trap if any of the
conditions in the 5-bit `TO` field hold. `TO=31` sets all of them, so it traps
unconditionally regardless of the operands — PowerPC's `ud2`. `tw 31,r0,r0` is
the canonical spelling and assembles to `0x7FE00008`.

`twi` is the immediate form, and it traps identically — but its 16-bit `SIMM`
field is dead weight when `TO=31`, so the entry word carries **its own index
into the manifest**:

```
id   0  entry word 0x0FE00000  -> SIMM 0   = lbColl_80006094
id 100  entry word 0x0FE00064  -> SIMM 100 = mnStageSw_80236CBC
id 173  entry word 0x0FE000AD  -> SIMM 173 = hsd_803B6BE4
```

(`.long 0` also faults, but carries no information.)

A Program exception is `OS_ERROR_PROGRAM` = 6, and melee's
`db_SetupCrashHandler` installs a handler for it — it skips only 4, 7, 8 and 9
— so a trap reaches the on-screen exception dump. The SDK's reporter also
prints it over OSReport, which is what Dolphin logs:

```
- UNHANDLED EXCEPTION -------------------------------
DSISR=00000000 DAR=00000000
ERROR 6: (PROGRAM)
Trap program exception at 8038E034 (read from SRR0)
```

SRR0 is the faulting address. Feed it back with `make lookup AT=0x8038E034` and
you get the function, its unit, its match percentage and its trap id. That is
your next matching target.

## Modes: what actually survives

The two modes differ only in the predicate, but they make very different
claims. Numbers below are a snapshot at 91.4% matched code.

**`fn` — trap every function scoring below 100%.** Answers *"which function do
I match next?"*

**`tu` — trap every function in a unit that isn't linked**, matched or not.
Answers *"which TU do I finish linking next?"* Its blockers are usually
functions that are already at 100%, held back by a sibling in the same file.

| | functions | bytes | provenance |
|---|---|---|---|
| linked units | 17,491 | 2,926,404 | **physically your compiled output** |
| unlinked units, scored 100% | 2,160 | 621,292 | **retail bytes, assumed identical** |
| unlinked units, scored <100% | 178 | 334,336 | trapped in `fn` mode |

So `tu` mode leaves 17,491 functions running, every one of them genuinely
yours. `fn` mode leaves 19,651 running, of which **2,160 (11%) are code you
never linked** — retail's instructions, left in place because objdiff says your
compile of them *would* be identical.

## Limitations

Worth being blunt about, because the build looks like it proves more than it
does.

- **`fn` mode asserts, it does not substitute.** Those 2,160 functions are
  retail bytes. objdiff comparing instruction encodings and relocation targets
  within a function is strong evidence, but it is not the same as having
  actually linked and run your code. `tu` mode is the one that is physically
  true.
- **Function scoring cannot see link-time problems.** A byte-matching object
  still doesn't validate symbol sizes, boundaries, names or scopes at the split
  level. Those only surface when the TU is actually linked — which is, again,
  what `tu` mode tests.
- **Data is never trapped.** Both modes only touch `.text`. Every byte of
  `.data`, `.rodata`, `.sdata` and `.bss` follows the same 952/59 split: a
  linked TU contributes its own data, an unlinked TU contributes retail's. The
  blunt consequence is that **a wrong `static const` table in an unmatched TU
  can never make this ROM crash**, because that C isn't in the binary. The
  project-wide gap is visible in the report: `matched_data` 96.5% against
  `complete_data` 78.1%.
- **It only tests code the game actually reaches.** Booting to the title screen
  clears the boot path, not the game. Menus, each stage, each character, each
  item have to be driven to be covered — and a run that ends with no trap means
  "nothing on that path", not "matched".
- **`debugconsole_main` is kept unpatched by default.** It is non-matching, but
  it owns `Exception_ReportCodeline`, so trapping it makes the reporter
  double-fault instead of telling you anything. That deliberately leaves 4
  sub-100% functions running.
- **Melee's debug console swallows some traps.** Instead of reporting, it parks
  the faulting thread on the trap instruction and spawns a priority-0 console
  thread that spins on `VIGetRetraceCount` — so the ROM freezes with audio
  still playing and nothing is ever printed. Detecting those needs a scan of
  the OS active-thread list for a saved SRR0 rather than a log grep.
- **Whole-body fill assumes functions own their bytes.** True for melee: the
  only non-function `.text` symbols are 11 OS exception-vector / RAS labels,
  and none fall inside a trapped range. `--fill entry` patches only the first
  instruction if you'd rather not rely on that.
- **Your build must reproduce the disc.** Before splicing, the source ISO is
  checked for the `GALE01` game id and its DOL payload is compared against your
  `main.dol`. A mismatch means the splice would blend two different binaries,
  so it is refused.

---

## Build a trap ROM

See [`trapbuild/README.md`](trapbuild/README.md) for the full usage reference.

```sh
export MELEE_REPO=~/etc/melee     # a checkout with a green `ninja`
cd trapbuild
make check
make both                         # -> build/GALE01/ssbm-{fn,tu}100.iso
make top                          # what to match next, by trapped bytes
make lookup AT=0x803A00C0         # decode a crash address
```

## Boot it and find the blocker

```sh
python3 trapwatch.py control          # sanity: the unpatched ISO must NOT trap
python3 trapwatch.py fn --seconds 60
python3 trapwatch.py fn --iterate 5   # ordered list of boot blockers
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

### Requirements beyond the builder

- A Dolphin with the **nogui frontend**, e.g.
  `~/etc/dolphin-dap/build/Binaries/dolphin-emu-nogui`. Override with
  `--dolphin` or `DOLPHIN_NOGUI`. The stock macOS `Dolphin.app` will not work:
  it has no headless platform, its file logging never emits, and its GDB stub
  is not reachable.
- Optional: `configure.py --map && ninja` produces `build/GALE01/main.elf.MAP`,
  which `trapwatch.py` installs into Dolphin's `Maps/` so Dolphin's own log
  names functions. Not required — SRR0 is decoded from
  `config/GALE01/symbols.txt`.

### How it detects the trap

By reading Dolphin's log for the SDK's `Trap program exception at <SRR0>` line,
not by breakpoints — **Dolphin's GDB-stub breakpoints do not fire under the
JIT**, which cost an hour to discover. The GDB connection is used for exactly
one thing: the `dolphin-dap` fork boots **paused** waiting for a debug client,
so `trapwatch` attaches to the stub (on an ephemeral port it discovers via
`lsof`, since `Dolphin.General.GDBPort` is not honoured) and sends a single
`continue`. Without that, the emulator sits at the entry point and every run
looks like a clean boot.

## Recorded run — 2026-08-22, master `e7844caa6`, 90.00% matched

Neither build booted; both died in audio/HSD init, within a second of `main`.

`fn` — real match targets, in the order the game hit them:

| # | function | unit | match |
|---|---|---|---|
| 1 | `HSD_AudioGetAuxHeapSize` | `sysdolphin/baselib/axdriver` | 86.000% |
| 2 | `gmMainLib_8015F600` | `melee/gm/gmmain_lib` | 96.970% |
| 3 | `HSD_SynthSFXSampleLoadCallback` | `sysdolphin/baselib/synth` | 93.368% |
| 4 | `gm_801A4014` | `melee/gm/gm_1A3F` | 97.857% |
| 5 | `Toy_80305058` | `melee/ty/toy` | 99.498% |

`tu` — all three blockers were *already 100%*, held back only by their TU not
being linked, which is the distinction the two modes exist to show:

| # | function | unit | match |
|---|---|---|---|
| 1 | `AXDriver_8038E498` | `sysdolphin/baselib/axdriver` | 100.000% |
| 2 | `HSD_SynthInit` | `sysdolphin/baselib/synth` | 100.000% |
| 3 | `HSD_AudioMalloc` | `sysdolphin/baselib/synth` | 100.000% |

As of 2026-08-26 **all eight are resolved** — matched, or their TU linked — and
the `fn` build is down from 206 traps to 174. The boot blocker list needs
re-running.
