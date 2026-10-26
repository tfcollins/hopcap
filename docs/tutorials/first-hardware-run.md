# First hardware run

Prerequisites, all from [the runbook](../how-to/tron-nemo-runbook.md): a verified bitstream
evidence directory, a verified boot evidence directory served over HTTP, a reservation on
`tron`, and a shell on `nemo`.

1. Dry-run the resolved configuration:

   ```sh
   hopcap run --plan examples/plans/band-2400-2500-4slot.json \
     --iio-uri ip:<tron-ip> --target root@<tron-ip> \
     --bind 192.0.2.2:4991 --rx-interface enp4s0f0np0 --out out/run1 --dry-run
   ```

2. Run for real, inside the reservation (the board must already be booted on the hopcap image):

   ```sh
   hopcap run ... --out out/run1
   hopcap verify run out/run1
   ```

3. Or let the launcher do boot, test and verification through adi-lg:

   ```sh
   export HOPCAP_PLAN=examples/plans/band-2400-2500-4slot.json HOPCAP_OUT=$PWD/out/run1
   # VRT49_CORUNDUM_BOOT unset (= auto) follows tron's boot-mode place tag (jtag if unset).
   export VRT49_CORUNDUM_BITSTREAM_REMOTE=/tmp/<dir on tron>/system_top.bit \
          VRT49_CORUNDUM_PSU_INIT_REMOTE=/tmp/<dir on tron>/psu_init.tcl \
          VRT49_CORUNDUM_KERNEL_URL=... VRT49_CORUNDUM_KERNEL_SHA256=... \
          VRT49_CORUNDUM_DTB_URL=... VRT49_CORUNDUM_DTB_SHA256=...
   python examples/hardware/zu11eg_hopcap.py --run --junit out/junit.xml
   ```

   `scripts/run-tron-hopcap.sh` does all of this from an artifact directory and the build's
   XSA, including copying the bitstream and `psu_init.tcl` to tron
   ([runbook](../how-to/tron-nemo-runbook.md)). On an SD-strapped board (tag `boot-mode=sd`) auto
   picks SD, which needs `VRT49_CORUNDUM_BOOT_BIN_URL`/`_SHA256` instead of the two JTAG paths.
   Set `VRT49_CORUNDUM_BOOT=jtag` or `sd` to override the tag.

What the run does, in order: identify the hop controller (`HOP2`) and Tick, snapshot framer
and NIC counters, program slots/settle/capture, configure the framer, start the spooler, park
the trigger low, enable FHM, compile the plan against the live Tick clock, hop, wait for the
controller to drain, disable FHM, stop the spooler, associate Contexts with captures, write the
bundle. `manifest.json` has one record per hop with its disposition (`ok`, `incomplete`,
`invalid`) and the reasons.
