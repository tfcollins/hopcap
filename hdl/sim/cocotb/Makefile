# SPDX-License-Identifier: MIT
#
# Cocotb + Verilator runner for hopcap RTL.
#   make TOP=vrt49_fhm_hop_ctrl     # controller unit suite
#   make TOP=fhm_hop_chain          # controller + vrt49 framer (needs libvrt49.so, see CLAUDE.md)
#   make lint
# Mirrors third_party/vrt49/hdl/sim/cocotb/Makefile (venv re-exec, per-TOP build dirs).

REPO      := $(abspath $(CURDIR)/../../..)
HDL       := $(REPO)/hdl
VRT49_HDL := $(REPO)/third_party/vrt49/hdl
SIM       ?= verilator
TOPLEVEL_LANG ?= verilog
.DEFAULT_GOAL := sim

COCOTB_BIN_PATH := $(abspath $(CURDIR)/../.venv/bin)
COCOTB_FOUND := $(shell command -v cocotb-config >/dev/null 2>&1 && echo y)
ifneq ($(filter lint,$(MAKECMDGOALS)),lint)
ifeq ($(COCOTB_FOUND),)
.PHONY: sim
sim:
	@PATH="$(COCOTB_BIN_PATH):$$PATH" $(MAKE) sim
$(filter-out sim,$(MAKECMDGOALS)):
	@PATH="$(COCOTB_BIN_PATH):$$PATH" $(MAKE) $@
COCOTB_WRAPPED := 1
endif
endif

ifndef COCOTB_WRAPPED
VENV       := $(REPO)/hdl/sim/.venv
COCOTB_BIN := $(VENV)/bin
COCOTB_PY  := $(COCOTB_BIN)/python
COCOTB_PATH := $(shell $(COCOTB_PY) -c "import os, cocotb; print(os.path.dirname(cocotb.__file__))" 2>/dev/null)

LINT_WARN := -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM -Wno-PINCONNECTEMPTY -Wno-WIDTHEXPAND -Wno-WIDTHTRUNC -Wno-BLKSEQ

.PHONY: lint
lint:
	verilator --lint-only -Wall $(LINT_WARN) \
	    -I$(VRT49_HDL)/axi_vrt49_framer \
	    $(HDL)/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v
	verilator --lint-only -Wall $(LINT_WARN) -Wno-MULTITOP \
	    -I$(VRT49_HDL)/axi_vrt49_framer \
	    $(HDL)/sim/cocotb/fhm_hop_chain.v \
	    $(HDL)/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v \
	    $(VRT49_HDL)/axi_vrt49_framer/axi_vrt49_framer_axi.v \
	    $(VRT49_HDL)/axi_vrt49_framer/axi_vrt49_framer.v \
	    $(VRT49_HDL)/vrt49_pack/vrt49_pack.v

ifneq ($(MAKECMDGOALS),lint)

COMPILE_ARGS += -I$(VRT49_HDL)/axi_vrt49_framer

ifeq ($(TOP),vrt49_fhm_hop_ctrl)
TOPLEVEL := vrt49_fhm_hop_ctrl
MODULE   := test_fhm_hop_ctrl
VERILOG_SOURCES := $(HDL)/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v
endif

ifeq ($(TOP),fhm_hop_chain)
TOPLEVEL := fhm_hop_chain
MODULE   := test_fhm_hop_chain
VERILOG_SOURCES := \
    $(HDL)/sim/cocotb/fhm_hop_chain.v \
    $(HDL)/vrt49_fhm_hop_ctrl/vrt49_fhm_hop_ctrl.v \
    $(VRT49_HDL)/axi_vrt49_framer/axi_vrt49_framer_axi.v \
    $(VRT49_HDL)/axi_vrt49_framer/axi_vrt49_framer.v \
    $(VRT49_HDL)/vrt49_pack/vrt49_pack.v
COMPILE_ARGS += -Wno-WIDTHEXPAND -Wno-WIDTHTRUNC
endif

ifndef TOPLEVEL
$(error Set TOP=vrt49_fhm_hop_ctrl | fhm_hop_chain)
endif

export PYTHONPATH := $(CURDIR):$(REPO)/third_party/vrt49/hdl/sim/cocotb:$(PYTHONPATH)
export VRT49_LIB  ?= $(REPO)/build/vrt49/src/libvrt49/libvrt49.so
export PATH       := $(COCOTB_BIN):$(PATH)
SIM_BUILD           := sim_build_$(TOP)
COCOTB_RESULTS_FILE := results_$(TOP).xml
EXTRA_ARGS += --trace --trace-structs

COCOTB_MK := $(firstword $(wildcard $(COCOTB_PATH)/share/makefiles/Makefile.sim))
ifeq ($(COCOTB_MK),)
$(error Cannot locate cocotb Makefile.sim under $(COCOTB_PATH))
endif
include $(COCOTB_MK)

endif  # not lint
endif  # not wrapped
