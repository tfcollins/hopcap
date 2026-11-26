#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Cross-build the two out-of-tree modules the hopcap adrv9361 image needs on lablp's stock
# Kuiper 2023_R2_P1 (kernel 6.1.70-284114-g2e8908932dfd, 32-bit ARMv7):
#
#   axi_timed_command_scheduler.ko   Tick driver, from third_party/tick/kernel (the pinned Tick)
#   iio-vrt49-buffer.ko              vrt49 framer driver, from third_party/vrt49/kernel/iio-vrt49-buffer
#
# Same container approach as tick's scripts/build_adi_linux_tick_module.sh, with one change:
# the module sources and tick's prepare_adi_linux_kernel_tree.py are COPIED into the work dir
# and only that copy is mounted. hopcap is usually a git worktree and both third_party trees are
# submodules, so their .git entries point outside any mount; nothing here needs git metadata
# from them. --linux-dir must be a real ADI Linux checkout at 2023_r2_p1 (2e8908932dfd), not a
# worktree: the prepare script checks its commit with git, and the kernel's setlocalversion
# derives the -g2e8908932dfd suffix from it.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/build_lablp_modules.sh --linux-dir <adi-linux @ 2023_r2_p1> --work-dir <dir>
         [--config <config.gz|config>] [--localversion <s>] [--expect-kernelrelease <r>]
         [--expect-kernel-version <v>] [--expect-commit <sha>] [--image ubuntu:24.04] [--dry-run]

Defaults target lablp's stock Kuiper 2023_R2_P1 image:
  --config               third_party/tick/ci/lablp/kuiper-2023_r2_p1-zynq-adrv9361-z7035.config.gz
  --localversion         auto: empty when the checkout's .scmversion already holds -284114-g<sha>
                         (as Kuiper-prepared trees do), else -284114
  --expect-kernelrelease 6.1.70-284114-g2e8908932dfd
  --expect-kernel-version 6.1.70
  --expect-commit        2e8908932dfd9faf40ec9220a508aa1ce02699a8
Outputs <work-dir>/axi_timed_command_scheduler.ko and <work-dir>/iio-vrt49-buffer.ko, and
prints each module's vermagic and depends (insmod does not resolve dependencies).
USAGE
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dry_run=0
image="ubuntu:24.04"
linux_dir=""
work_dir=""
config_path="${repo_root}/third_party/tick/ci/lablp/kuiper-2023_r2_p1-zynq-adrv9361-z7035.config.gz"
localversion="auto"
expect_kernelrelease="6.1.70-284114-g2e8908932dfd"
expect_kernel_version="6.1.70"
expect_commit="2e8908932dfd9faf40ec9220a508aa1ce02699a8"
arch="arm"
cross_prefix="arm-linux-gnueabihf-"
apt_toolchain="gcc-arm-linux-gnueabihf"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --linux-dir) linux_dir="${2:?missing value for --linux-dir}"; shift 2 ;;
    --work-dir) work_dir="${2:?missing value for --work-dir}"; shift 2 ;;
    --config) config_path="${2:?missing value for --config}"; shift 2 ;;
    # May be empty: with .scmversion supplying the whole suffix LOCALVERSION must be empty.
    --localversion) localversion="${2-}"; shift 2 ;;
    --expect-kernelrelease) expect_kernelrelease="${2:?missing value}"; shift 2 ;;
    --expect-kernel-version) expect_kernel_version="${2:?missing value}"; shift 2 ;;
    --expect-commit) expect_commit="${2:?missing value}"; shift 2 ;;
    --image) image="${2:?missing value for --image}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "${linux_dir}" || -z "${work_dir}" ]]; then
  usage >&2
  exit 2
fi
if [[ ! -f "${linux_dir}/Makefile" ]]; then
  echo "error: not an ADI Linux checkout: ${linux_dir}" >&2
  exit 2
fi
if [[ -f "${linux_dir}/.git" ]]; then
  echo "error: ${linux_dir} is a git worktree (.git is a file); use a real checkout" >&2
  exit 2
fi
if [[ ! -f "${config_path}" ]]; then
  echo "error: missing target kernel config: ${config_path}" >&2
  exit 2
fi
tick_kernel="${repo_root}/third_party/tick/kernel"
vrt49_kernel="${repo_root}/third_party/vrt49/kernel/iio-vrt49-buffer"
prepare="${repo_root}/third_party/tick/scripts/prepare_adi_linux_kernel_tree.py"
for f in "${tick_kernel}/axi_timed_command_scheduler.c" "${vrt49_kernel}/iio-vrt49-buffer.c" "${prepare}"; do
  if [[ ! -f "$f" ]]; then
    echo "error: missing ${f} (git submodule update --init?)" >&2
    exit 2
  fi
done
linux_dir="$(cd "${linux_dir}" && pwd)"
if [[ "${localversion}" == "auto" ]]; then
  # A checkout prepared like Kuiper's carries .scmversion with the whole suffix
  # ("-284114-g<sha>"), and setlocalversion then prints it verbatim: LOCALVERSION must be
  # empty or the release reads -284114-284114-g<sha>. A plain checkout yields only -g<sha>.
  want="${expect_kernelrelease#"${expect_kernel_version}"}"; want="${want%-g*}"
  scm="$(cat "${linux_dir}/.scmversion" 2>/dev/null || true)"
  if [[ -n "${want}" && "${scm}" == "${want}-g"* ]]; then
    localversion=""
  else
    localversion="${want}"
  fi
  echo "localversion auto: '${localversion}' (.scmversion: ${scm:-none})"
fi
work_dir="$(realpath -m "${work_dir}")"
config_name="config"
[[ "${config_path}" == *.gz ]] && config_name="config.gz"

# The container runs as root (apt); hand everything it writes back to the caller, also when
# the build fails, so the work dir can be removed without root.
container_script='set -euo pipefail
trap "chown -R '"$(id -u):$(id -g)"' /work" EXIT
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends git ca-certificates make gcc '"${apt_toolchain}"' bc bison flex libssl-dev libelf-dev xz-utils gzip kmod python3 >/dev/null
git config --global --add safe.directory /linux
python3 /work/src/prepare_adi_linux_kernel_tree.py \
  --linux-dir /linux --build-dir /work/build --config /work/'"${config_name}"' \
  --arch '"${arch}"' --cross-compile '"${cross_prefix}"' \
  --localversion='"${localversion@Q}"' --expect-kernelrelease='"${expect_kernelrelease@Q}"' \
  --expect-kernel-version='"${expect_kernel_version@Q}"' --expect-commit='"${expect_commit@Q}"'
for m in tick-kernel iio-vrt49-buffer; do
  make -C /work/build M=/work/src/$m ARCH='"${arch}"' CROSS_COMPILE='"${cross_prefix}"' KBUILD_MODPOST_WARN=1 modules
done
cp -f /work/src/tick-kernel/axi_timed_command_scheduler.ko /work/src/iio-vrt49-buffer/iio-vrt49-buffer.ko /work/
for ko in /work/axi_timed_command_scheduler.ko /work/iio-vrt49-buffer.ko; do
  '"${cross_prefix}"'readelf -h "$ko" | grep -E "Class|Machine"
  echo "MODULE $(basename "$ko") vermagic=$(modinfo -F vermagic "$ko" | cut -d" " -f1) depends=$(modinfo -F depends "$ko")"
done
chown -R "${HOST_UID}:${HOST_GID}" /work 2>/dev/null || true'

docker_cmd=(
  docker run --rm
  -e "HOST_UID=$(id -u)"
  -e "HOST_GID=$(id -g)"
  -v "${work_dir}:/work"
  -v "${linux_dir}:/linux:ro"
  -w /work
  "${image}"
  bash -lc "${container_script}"
)

if [[ "${dry_run}" -eq 1 ]]; then
  echo "Stages: ${tick_kernel} -> ${work_dir}/src/tick-kernel"
  echo "Stages: ${vrt49_kernel} -> ${work_dir}/src/iio-vrt49-buffer"
  echo "Stages: ${prepare} -> ${work_dir}/src/prepare_adi_linux_kernel_tree.py"
  echo "Stages: ${config_path} -> ${work_dir}/${config_name}"
  printf '+'
  printf ' %q' "${docker_cmd[@]}"
  printf '\n'
  echo "ARCH=${arch} CROSS_COMPILE=${cross_prefix} LOCALVERSION=${localversion} kernelrelease=${expect_kernelrelease}"
  echo "HOPCAP_TICK_MODULE=${work_dir}/axi_timed_command_scheduler.ko"
  echo "HOPCAP_VRT49_MODULE=${work_dir}/iio-vrt49-buffer.ko"
  exit 0
fi

mkdir -p "${work_dir}"
rm -rf "${work_dir}/src"
mkdir -p "${work_dir}/src/tick-kernel" "${work_dir}/src/iio-vrt49-buffer"
cp -a "${tick_kernel}/." "${work_dir}/src/tick-kernel/"
cp -a "${vrt49_kernel}/Kbuild" "${vrt49_kernel}/Makefile" "${vrt49_kernel}/iio-vrt49-buffer.c" \
  "${work_dir}/src/iio-vrt49-buffer/"
cp -f "${prepare}" "${work_dir}/src/prepare_adi_linux_kernel_tree.py"
cp -f "${config_path}" "${work_dir}/${config_name}"
# Stale objects from a host-native build would be relinked for the wrong ARCH.
find "${work_dir}/src" \( -name '*.o' -o -name '*.ko' -o -name '*.mod*' -o -name '.*.cmd' \
  -o -name Module.symvers -o -name modules.order \) -delete
{
  echo "tick_revision=$(git -C "${repo_root}/third_party/tick" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "vrt49_revision=$(git -C "${repo_root}/third_party/vrt49" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "expect_kernelrelease=${expect_kernelrelease}"
} >"${work_dir}/SOURCES.txt"

"${docker_cmd[@]}" | tee "${work_dir}/build.log"
grep -q "^MODULE axi_timed_command_scheduler.ko vermagic=${expect_kernelrelease} " "${work_dir}/build.log"
grep -q "^MODULE iio-vrt49-buffer.ko vermagic=${expect_kernelrelease} " "${work_dir}/build.log"
echo "HOPCAP_TICK_MODULE=${work_dir}/axi_timed_command_scheduler.ko"
echo "HOPCAP_VRT49_MODULE=${work_dir}/iio-vrt49-buffer.ko"
