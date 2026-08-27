# melee-trapbuild -- build a "matched-only" Melee ISO.
#
#   make check              are the inputs where we need them?
#   make fn                 trap every function below 100%          -> ssbm-fn100.iso
#   make tu                 trap every function in an unlinked TU   -> ssbm-tu100.iso
#   make both               both of the above
#   make top                what to match next, by trapped bytes
#   make lookup AT=0x803A00C0   decode a crash address
#   make clean              remove the generated DOLs, ISOs and manifests
#
# Point it at a melee checkout with a green `ninja`:
#
#   make fn REPO=~/src/melee ISO=~/isos/ssbm_rev2.iso
#   MELEE_REPO=~/src/melee make fn
#
# Knobs: FILL=entry|body  KEEP='name1 name2'  NO_ISO=1  FORCE=1  LIMIT=20  FOR=fn|tu

PY      ?= python3
TRAPROM := $(PY) $(dir $(lastword $(MAKEFILE_LIST)))traprom.py

FOR     ?= fn
LIMIT   ?= 20

FLAGS   := $(if $(REPO),--repo $(REPO)) $(if $(ISO),--iso $(ISO)) \
           $(if $(BUILD_DIR),--build-dir $(BUILD_DIR))
BUILD   := $(FLAGS) $(if $(FILL),--fill $(FILL)) $(if $(NO_ISO),--no-iso) \
           $(if $(FORCE),--force) $(foreach k,$(KEEP),--keep $(k))

.PHONY: help check fn tu both top lookup clean

help:
	@sed -n '1,17p' $(firstword $(MAKEFILE_LIST)) | sed 's/^# \{0,1\}//'

check:
	@$(TRAPROM) check $(FLAGS)

fn tu both:
	@$(TRAPROM) $@ $(BUILD)

top:
	@$(TRAPROM) top --for $(FOR) --limit $(LIMIT) $(FLAGS)

lookup:
	@test -n "$(AT)" || { echo "usage: make lookup AT=0x803A00C0 [FOR=fn|tu]" >&2; exit 2; }
	@$(TRAPROM) lookup --for $(FOR) $(AT) $(FLAGS)

clean:
	@$(TRAPROM) clean $(FLAGS)
