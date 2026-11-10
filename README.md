# KernelForge — DroidSpaces-first Android kernel builder

**Shared `kernel.yaml` plus a small YAML file per device. DroidSpaces is mandatory for every target.**

KernelForge builds LineageOS 24.0 kernels with shared DroidSpaces config, a common LXC cgroup patch, optional systemd backports and one pinned LLVM toolchain. OnePlus 9R (`lemonades`, SM8250) retains its standard AnyKernel3 ZIP. **Motorola moto g10 power (XT2127-4, LineageOS variant 2)** uses a **non-flashable Capri payload ZIP** as an intermediate for complete image generation; Actions publishes finished images for the exact November 5, 2026 LineageOS 24.0 ROM. The regular moto g10 is variant 1 of the same LineageOS `capri` build; `caprip` is a different device (moto g30). Each target uses its official LineageOS kernel source and defconfig.

## Run

Linux x86_64 with Python 3.10+, Git, Make, `ccache`, and standard kernel build dependencies (CI installs these automatically):

~~~bash
python3 -m pip install -r requirements.txt
./build.sh validate --device lemonades
./build.sh validate --device capri
./build.sh build --device capri --dry-run
./build.sh build --device capri -j 8       # Compiles + packages DroidSpaces-capri-payload.zip
./build.sh build --device lemonades -j 8   # OnePlus 9R (default)
./build.sh package --device capri          # Repackage previously built capri images
~~~

The builder merges `kernel.yaml` with `devices/<codename>.yaml`, fetches the **current `lineage-24.0` branch HEAD at each build** (no kernel commit pins), applies enabled shared patches, copies the mandatory DroidSpaces fragment, merges device-specific defconfig fragments using the kernel's Kconfig tools, compiles, and packages the selected device. Kconfig resolves unsupported or unavailable options. **A successful build does not prove every DroidSpaces feature is enabled**; inspect the generated `.config` and test container functionality. Workspaces are isolated: `.work/kernel/` for lemonades and `.work/kernel-capri/` for capri. LLVM is shared at `.work/_toolchains/kernel/llvm`; ccache at `.work/_ccache`. Preparation resets only the selected managed source checkout.

The commands are not a complete LineageOS ROM build. Integration into an Android device tree, SELinux, permissions and userspace runtime remains separate.

## Built-in BakaSU root

Both devices enable [BakaSU](https://github.com/Baka-SU/BakaSU) by default,
pinned to `548d1dda060ca198823d8808804d01f5209ef4a9` in `kernel.yaml`.
Source preparation installs it under the managed kernel checkout's `KernelSU/`
directory and applies `patches/0002-bakasu-manual-hooks-4.19.patch`.
`assets/bakasu.config` requests built-in `CONFIG_KSU=y`, manual syscall hooks,
automatic LSM setuid/init-rc hooks, and the input handler. Tracepoint hooking and
SUSFS are not enabled. The BakaSU fragment is merged after the DroidSpaces
fragment; DroidSpaces features, including `CONFIG_SYSVIPC`, remain requested.

Install the matching **BakaSU manager APK**, then explicitly grant root to the
apps that need it. Neither package installs the manager or grants apps
root automatically. Use the upstream [manager installation instructions](https://bakasu.org/guide/install/);
they currently point to development manager builds. Built-in BakaSU does not
require Magisk boot-image patching or a separate KernelSU loadable module.
Lemonades AnyKernel3 handling of an already patched boot image is unchanged.

Set `root.enabled: false` in shared `kernel.yaml` before rebuilding to omit
BakaSU, its hooks, and its config fragment. Repackaging an existing kernel
cannot enable or disable built-in root.

The manual-hook patch applies to both inspected LineageOS 24.0 4.19 source
snapshots and passes the pinned BakaSU hook-presence checker. CLI validation
and default-root build previews also pass. See **Verified Capri run** below
for the compiled configuration and physical boot evidence. Those results do
not validate OnePlus hardware or future upstream branch changes.


## Layout

~~~text
kernel.yaml                      Shared LLVM, DroidSpaces, patches, AnyKernel3 defaults
devices/lemonades.yaml            OnePlus 9R kernel and package settings
devices/capri.yaml                Moto g10 power (variant 2) kernel and package settings
build.sh                         Command-line entrypoint
kernelforge/cli.py               Source / patch / config / build / package
assets/droidspaces.config        Always included
patches/0001-lxc-cgroup-prefix.patch
patches/backports-4.19/          Four systemd backport patches
schema/common.schema.json        Shared kernel.yaml editor/validation schema
schema/device.schema.json        Device override editor/validation schema
schema/kernel.schema.json        Fully merged per-device validation schema
tests/                           Validation tests
.github/workflows/kernel.yml     CI validation and on-demand build
.work/                           Generated files, ignored
~~~

The `common` and `device` schemas validate the two on-disk YAML formats; the `kernel` schema validates the resolved merge. All three are exercised by the test suite.

The `validate` subcommand and CI still verify device YAML structure, paths, patch entries, and packaging settings. These are input checks, **not** a post-Kconfig feature gate.

## Specific 4.19 backports for LineageOS 24

Source: https://github.com/Droidspaces/droidspaces-backports/tree/main/kernel-4.19-series

The upstream 4.19 series has ten patches for newer systemd. The feature-probe observations below are for **SM8250 only**, not a completed probe of the Motorola kernel:

| Patch | Feature | Already available? | Action |
|---|---|---|---|
| 0001 | waitid P_PIDFD | Yes | Skip |
| 0002 | SO_BINDTOIFINDEX | Yes | Skip |
| 0003 | statx mount ID | No | Included, disabled by default |
| 0004 | statx mount root | No | Included, disabled by default |
| 0005 | utimensat AT_EMPTY_PATH | No | Included, disabled by default |
| 0006 | faccessat2 | No | Included, disabled by default |
| 0007–0008 | close_range | Yes | Skip |
| 0009 | rtnetlink nexthop | Yes | Skip |
| 0010 | x86 syscall entries | ARM64 kernel | Skip |

The original 0006 patch conflicts because LineageOS 24 already has clone3 (435), close_range (436), epoll_pwait2 (441) and syscall count 442. Its adapted version changes only the missing 439 entries while keeping these pre-existing entries and counts.

All four modern-systemd backports (0003–0006) are included but **disabled on both devices by default**. The enabled shared LXC cgroup patch has matching context in both vendor kernel trees. The LineageOS-specific `0006` adaptation is not automatically safe to enable on other vendors. Reprobe and port optional patches per device before using systemd 260+. The DroidSpaces userspace runtime also needs its mount-API fallback.

**When changing source kernels, re-run upstream tools/probe-kernel.sh and adjust the patches list.** Do not blindly apply all 4.19 patches to every vendor tree.

## Declarative patches

The same `patches` list and `assets/droidspaces.config` are loaded for every device from shared `kernel.yaml`; device YAMLs **cannot override** these shared settings. Keep the modern-systemd backports disabled until tested on the selected source. A missing or failed enabled patch aborts the build.

~~~yaml
droidspaces:
  config: assets/droidspaces.config
  destination: arch/arm64/configs/vendor/droidspaces.config
  target: vendor/droidspaces.config

patches:
  - file: patches/backports-4.19/0003-statx-add-mount-ID.patch
    enabled: false
  # 0004 and 0005 also remain listed but disabled by default.
  # 0006 faccessat2 also remains listed but disabled by default.
  - file: patches/0001-lxc-cgroup-prefix.patch
    enabled: true
~~~

The cgroup patch restores compatibility naming for LXC and can be disabled if the container runtime does not require it. It is not a substitute for Android userspace cgroup setup.

## Compiler selection

KernelForge now downloads the **Greenforce Clang 24.0.0 GitHub release (2026-11-07)** for both capri and lemonades, using `LLVM=1 LLVM_IAS=1`. The archive is approximately **45 MB**, compared with a much larger Google Gitiles archive. The shared `kernel.yaml` pins the exact release URL and SHA256 checksum; the builder verifies the checksum before extracting and caches the compiler across devices. This is a third-party LLVM build, **not the identical Google Clang prebuilt**. Compilation and boot compatibility with the device kernels still need testing. No GCC cross-toolchain is mixed in.

The bundled prebuilt is for Linux x86_64. For a native ARM64 build host, provide a compatible full LLVM toolchain (including `ld.lld` and LLVM binutils), not merely a `clang` binary:

~~~bash
export KERNELFORGE_LLVM_BIN=/opt/llvm/bin
./build.sh build -j 8
~~~

The checksum-verified GitHub release archive is cached and shared, while source/defconfig and build workspaces are device specific. **Kernel source commits are intentionally not pinned:** each `prepare` or `build` fetches the selected device's latest `lineage-24.0` branch head and records the resolved SHA in `.work/<device-workspace>/prepared.json` for traceability. The toolchain release and AnyKernel3 template remain pinned for tooling stability. Upstream changes can break patches, compilation or boot compatibility, so verify results before flashing.

## GitHub Actions and ccache

GitHub Actions runs **only when manually dispatched**: commits and pull requests do **not** trigger builds. Choose `both` (default), `lemonades`, or `capri` in the existing workflow. Each run validates both configurations and executes tests before compilation. Every successful compilation uploads `compiled-<device>-<run_number>` containing `Image` and `kernel.config` **before packaging**; Capri also includes `modules.tar.gz` with its matching module binaries and ABI inputs. Capri then automatically downloads and verifies the pinned official ROM, extracts matching originals and generates `boot.img`, `vendor_boot.img`, `vendor.img` and `manifest.json`. Only successful complete generation uploads `DroidSpaces-capri-images-lineage-24.0-20261105-<run_number>`; the non-flashable payload remains an intermediate, not a published installer. `DroidSpaces-lemonades-<run_number>` still contains the unchanged standard AnyKernel3 ZIP. Artifacts have 14-day retention and are staged under visible `dist/` paths. Manual runs are independent and do not automatically cancel. GitHub Releases publishing is not enabled. No workflow flashes a phone.

Each matrix build compiles with `-j "$(nproc)"`, using the runner's available processing units instead of hardcoded parallelism. Jobs restore the shared LLVM toolchain and device-specific ccache, then **explicitly save ccache immediately after compilation and before packaging**. Partial compilation caches are also saved on ordinary build failures; cancelled jobs cannot reliably save their cache. The compiled `Image` and config are uploaded before ZIP packaging so packaging errors cannot discard finished kernel outputs. Official GitHub Actions use checkout v7, setup-python v7, cache v6, and upload-artifact v7.

To use ccache locally (including on ARM64 hosts with a native LLVM toolchain):

~~~bash
sudo apt-get install ccache
./build.sh build -j 8
CCACHE_DIR="$PWD/.work/_ccache" ccache --show-stats
~~~

Ccache is on by default via the single YAML file:

~~~yaml
build:
  ccache:
    enabled: true
    max_size: 2G
~~~

KernelForge invokes `CC=ccache clang`, `HOSTCC=ccache clang`, and `HOSTCXX=ccache clang++` in **both** Make invocations, while keeping `LLVM=1 LLVM_IAS=1`. No GCC toolchain is introduced. Local cache files are stored at `.work/_ccache`; CI overrides `CCACHE_DIR` to `.ccache` so GitHub Actions can persist the cache. Set `enabled: false` if ccache is unavailable; initial builds are typically cache misses, and linking and device-tree generation are not cached by ccache.

## Device-specific packaging

`kernel.yaml` supplies pinned AnyKernel3 tooling and shared boot defaults.
Lemonades keeps its standard `DroidSpaces-lemonades.zip` installer and boot/slot
policy unchanged. Capri sets `package.type: manual-images` and produces
`DroidSpaces-capri-payload.zip`. **This payload is not flashable: never sideload
it or install it in recovery or a kernel-manager app. Previous Capri sideload
artifacts are unsafe and must not be used.** The recovery-time module installer
has been removed; `anykernel3` packages cannot include matched modules.
Actions uses this payload only as an intermediate and publishes complete images
instead. The finished Capri artifact is for **only**
`lineage-24.0-20261105-nightly-capri-signed.zip`, not another nightly or ROM.
It requires manual bootloader/fastbootd flashing as described below, not sideload.


The Capri ZIP contains only `boot-files/Image`, matched vendor and early-boot
module trees and checksum manifests, pinned `tools/magiskboot` and `tools/fec`,
and `payload.json`. The root manifest identifies Capri and the kernel release
and hashes every other archive member. There is no recovery updater, AnyKernel
core, `anykernel.sh` or installation script. The pinned AnyKernel checkout is
used solely as a source for the two offline tools.

Capri's `package.modules.aliases` follows the official LineageOS
[`TARGET_MODULE_ALIASES`](https://github.com/LineageOS/android_device_motorola_sm6225-common/blob/lineage-24.0/BoardConfigCommon.mk).
The November 5 ROM contains 43 modules, including 29 renamed files.
`package.modules.vendor_boot` selects `nova_0flash_mmi.ko`, `ili9882_mmi.ko` and
`exfat.ko`, in that order. Packaging requires the complete build output:
`Image`, `modules.order`, every listed `.ko`, `Module.symvers`, and
`include/config/kernel.release`. Module release strings, imported symbol CRCs
and dependency closure are checked before staging. LLVM strips debug information
from staged copies only; signed modules cannot be stripped. Official filenames,
load order, dependency metadata and checksums stay together. Neither this
cutover nor BakaSU disables `CONFIG_SYSVIPC` or relaxes module ABI validation.

### Generate complete Capri images offline

Use three **matching original images from the exact installed ROM build**,
not images from another release or a previously modified kernel. Confirm
model `XT2127-4`, codename `capri`, and the firmware baseline in the
[official variant-2 guide](https://lineageos.github.io/lineage_wiki/devices/capri/install/variant2/).
Keep the originals and an independent restore method. No replacement DTB/DTBO
is supplied: unrelated ramdisk data and the original device-tree data are
retained. Keep `CONFIG_CAPRI_DTB=y`; `CAPRIP` belongs to the Moto G30.

The managed provisioning helper automatically downloads the official
[`lineage-24.0-20261105-nightly-capri-signed.zip`](https://mirrorbits.lineageos.org/full/capri/20261105/lineage-24.0-20261105-nightly-capri-signed.zip)
(1,147,102,832 bytes), pinned to SHA256
`34486153eacc2b0e6f6de471a89dae726f278f66a95adb2977a9b952105b3463`.
It does not select the latest nightly. It verifies the ZIP and uses pinned
`payload-dumper-go` 2.1.0 with its normal payload verification to extract only
`boot`, `vendor_boot` and `vendor`, then checks each image's expected SHA256.
The managed `.work/capri-lineage-20261105` cache includes the ZIP, originals and
tools; cached files are checksum-validated on reuse. Actions caches that entire
directory under a fixed dated key.
If the dated ROM disappears upstream and no verified cache exists, preparation
fails rather than substituting a newer nightly. Updating the base-ROM pins
requires testing that new ROM/image combination.

On Linux x86_64, install Python dependencies from `requirements.txt` and
`e2fsprogs`, `qemu-user`, `libc6` and `libgcc-s1` (Actions installs these).
QEMU runs the bundled ARM Android `magiskboot`. The provisioning helper also
downloads checksum-pinned **native** `avbtool`, `fec` and matching `libc++.so`
from [AOSP build-tools commit `07b44a63150de430daa6338a18067e7521a458e1`](https://android.googlesource.com/kernel/prebuilts/build-tools/+/07b44a63150de430daa6338a18067e7521a458e1/).
Keep their `tools/bin/` and `tools/lib64/` layout and put the absolute `tools/bin/`
on `PATH`. The bundled Android `fec` needs an Android linker and libraries;
QEMU alone is not sufficient, so image generation uses the native provisioned
encoder instead and never silently drops FEC.

~~~bash
python -m kernelforge.lineage --output .work/capri-lineage-20261105
export PATH="$PWD/.work/capri-lineage-20261105/tools/bin:$PATH"
mkdir -p dist/images
python -m kernelforge.images \
  --payload .work/kernel-capri/DroidSpaces-capri-payload.zip \
  --boot .work/capri-lineage-20261105/originals/boot.img \
  --vendor-boot .work/capri-lineage-20261105/originals/vendor_boot.img \
  --vendor .work/capri-lineage-20261105/originals/vendor.img \
  --output dist/images/capri
~~~

The output directory must not already exist; its parent must exist. The command
operates on local copies and atomically produces complete `boot.img`,
`vendor_boot.img`, `vendor.img` and `manifest.json`. It does not contact or
flash a device.

Offline processing rebuilds AVB hashes, the vendor hashtree, FEC and image
footers while preserving partition sizes and supported metadata. This does
**not** recreate original external `vbmeta` signatures or establish that the
device's verified-boot chain will trust the new images. Matching module CRCs,
successful image generation and clean filesystem checks are not physical boot
or hardware validation. Do not disable AVB, erase partitions, or force-load
modules to work around a mismatch.

### Verified Capri run

On 2026-11-10, the kernel from [GitHub Actions run 18](https://github.com/divyam234/kernel-forge/actions/runs/42843641468)
was repackaged into complete images using the November 5 LineageOS 24.0
originals, manually flashed to slot `b`, and completed Android boot.

- Running kernel: `4.19.325-cip136-st20-perf-g1da2d0861244-dirty`.
- The compiled Image retained SYSVIPC, IPC namespaces, CFI/LTO, module versioning
  and built-in BakaSU at `548d1dda060ca198823d8808804d01f5209ef4a9`.
- All 43 module files and three generated vendor metadata files matched the
  payload after boot. All three early modules loaded; 42 modules were loaded
  overall, with the alternative `fpc1020_mmi` driver not loaded.
- Captured kernel logs contained no module CRC/symbol-loading errors or
  panic/Oops/CFI failures. BakaSU initialized in built-in mode.
- The device's existing top-level `vbmeta_b` had flags `3` and its live vendor
  mapping was linear. Neither vbmeta partition was changed. This is an observed
  device configuration, not a recommendation to disable verification elsewhere.

The payload packaging and offline generator passed 92 local tests and a
full-ROM generation smoke run during that verification. Those results describe
the run-18 kernel and manual image generation, not a later Actions image artifact.
Physical boot evidence does not establish complete hardware, app-root, or
container workload coverage.

For DroidSpaces userspace setup, the [official Android guide](https://www.droidspaces.org/docs/installation-android.html)
says the app installs its backend and a small root-manager boot module itself.
Grant DroidSpaces root in the manager and reboot when requested; a separate
manually downloaded daemon module is not required for that documented setup.

### Manual flashing only, with an explicit slot

The following is a manual operator sequence, not a command run by KernelForge
or CI. Back up first and verify the output images and ROM identity. Enter the
bootloader manually, then query the current slot; **do not assume `a` or `b`**:

~~~bash
fastboot getvar product
fastboot getvar current-slot
# Set SLOT to the exact a or b returned above; stop if missing or unexpected.
read -r -p 'Verified current slot (a or b): ' SLOT
case "$SLOT" in a|b) ;; *) echo "Invalid slot"; exit 1 ;; esac
fastboot --slot="$SLOT" flash boot /path/to/existing-parent/capri-images/boot.img
fastboot --slot="$SLOT" flash vendor_boot /path/to/existing-parent/capri-images/vendor_boot.img
fastboot reboot fastboot
fastboot getvar is-userspace
fastboot getvar current-slot
# Continue only if is-userspace=yes and current-slot still equals $SLOT.
fastboot --slot="$SLOT" flash vendor /path/to/existing-parent/capri-images/vendor.img
~~~

`vendor` is a logical partition and must be flashed in **fastbootd**, not
bootloader fastboot. Stop on any error or changed slot; do not mix slots or
use `--slot=all`. No erase, verification-disabling flag, automatic flashing,
or automatic final reboot is provided. A successful flash still does not prove
boot, BakaSU manager operation, SELinux, radio, Wi-Fi or other hardware works.
Never cross-flash Capri and OnePlus packages.

Rebuild with `./build.sh build --device capri -j 8` or repackage a complete
output tree with `./build.sh package --device capri`. The compiled-module
artifact is retained independently if packaging fails. Earlier image-only
downloads lack the module set and cannot be repaired by repacking alone.


## Source credits

DroidSpaces configuration source: https://github.com/JackA1ltman/dreamworld_oneplus_sm8250/tree/staging — original author credit preserved in asset.
LXC cgroup change: Dreamworld commit 60c147cf1f33c09828128478ec602837e9279a05.
Systemd backports: https://github.com/Droidspaces/droidspaces-backports (0006 adapted as documented).

No commands in this documentation are run automatically against a phone.
