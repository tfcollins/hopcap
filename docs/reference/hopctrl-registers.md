# `vrt49_fhm_hop_ctrl` registers

Generated from `hdl/hopctrl_regmap.json`, the single source the RTL, the host and this page are
all tested against. Base address `0x84AC0000`, AXI-Lite, 100 MHz `sys_cpu_clk`.

```{include} _regmap_table.md
```

States: IDLE 0, DRAIN 1, SETTLE 2, PUBLISH 3, OPEN 4, CLOSED 5. An event is the rising edge of
`scheduler_gpio[0]` while enabled; the slot comes from `scheduler_gpio[3:1]` or, in sequence
mode, from `SEQ_INDEX`. With `enable=0` the gate is forced open.
