# KernelForge — DroidSpaces-first Android kernel builder

**Shared `kernel.yaml` plus a small YAML file per device. DroidSpaces is mandatory for every target.**

KernelForge builds LineageOS 24.0 kernels with shared DroidSpaces config, a common LXC cgroup patch, optional systemd backports, one pinned LLVM toolchain, and device-specific AnyKernel3 ZIPs. Supported targets: OnePlus 9R (`lemonades`, SM8250) and **Motorola moto g10 power (XT2127-4, LineageOS variant 2)** under `capri` (Snapdragon SM4250, using the SM6225/Bengal-family source tree). The regular moto g10 is variant 1 of the same LineageOS `capri` build; `caprip` is a different device (moto g30). Each target uses its official LineageOS kernel source and defconfig.

## Run

Linux x86_64 with Python 3.10+, Git, Make, `ccache`, and standard kernel build dependencies (CI installs these automatically):

~~~bash
python3 -m pip install -r requirements.txt
./build.sh validate --device lemonades
./build.sh validate --device capri
./build.sh build --device capri --dry-run
./build.sh build --device capri -j 8       # Compiles + packages DroidSpaces-capri.zip
./build.sh build --device lemonades -j 8   # OnePlus 9R (default)
./build.sh package --device capri          # Repackage previously built capri images
~~~

The builder merges `kernel.yaml` with `devices/<codename>.yaml`, fetches the **current `lineage-24.0` branch HEAD at each build** (no kernel commit pins), applies enabled shared patches, copies the mandatory DroidSpaces fragment, merges device-specific defconfig fragments using the kernel's Kconfig tools, compiles, and packages AnyKernel3. KernelForge no longer compares the final `.config` against every requested symbol: Kconfig itself resolves unsupported or unavailable options. **A successful build does not prove every DroidSpaces feature is enabled**; inspect the generated `.config` and test container functionality before flashing. Workspaces are isolated: `.work/kernel/` for lemonades and `.work/kernel-capri/` for capri. LLVM is shared at `.work/_toolchains/kernel/llvm`; ccache is shared at `.work/_ccache`. Preparation resets only the selected managed source checkout.

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
apps that need it. The kernel ZIP does not install the manager or grant apps
root automatically. Use the upstream [manager installation instructions](https://bakasu.org/guide/install/);
they currently point to development manager builds. Built-in BakaSU does not
require Magisk boot-image patching or a separate KernelSU loadable module.
Existing AnyKernel3 handling of an already patched boot image is unchanged.

Set `root.enabled: false` in shared `kernel.yaml` before rebuilding to omit
BakaSU, its hooks, and its config fragment. Repackaging an existing kernel
cannot enable or disable built-in root.

Verification: the manual-hook patch applies to both inspected LineageOS 24.0
4.19 source snapshots and passes the pinned BakaSU hook-presence checker.
CLI validation and default-root build previews also pass. These checks are
**not** a full kernel compilation, final Kconfig verification, or device root
test. Inspect the built `.config` for `CONFIG_KSU=y` and
`CONFIG_KSU_MANUAL_HOOK=y`, then verify manager recognition, app approval, and
root access on the device before treating either ZIP as validated.


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

All four modern-systemd backports (0003–0006) are included but **disabled on both devices by default**. Only the shared LXC cgroup naming patch is enabled. Its target function has matching context in both vendor kernel source trees, but capri has not been compiled or boot-tested. The LineageOS-specific `0006` adaptation is not automatically safe to enable on other vendors. Reprobe and port optional patches per device before using systemd 260+. The DroidSpaces userspace runtime also needs its mount-API fallback.

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

GitHub Actions runs **only when manually dispatched**: commits pushed to `main` and pull requests do **not** trigger validation or builds. Go to **Actions → KernelForge → Run workflow** and choose `both` (default), `lemonades`, or `capri`; the requested device jobs compile. Each manual run validates both configurations and executes tests before building the selected device(s). Every successful compilation uploads an early `compiled-<device>-<run_number>` artifact containing `Image` and `kernel.config` **before AnyKernel3 packaging**; Capri also includes `modules.tar.gz` with its matching module binaries and ABI inputs. When packaging succeeds, a separate `droidspaces-<device>-<run_number>` artifact contains the flashable ZIP. Both have 14-day retention. Files are staged under visible `dist/` paths because `actions/upload-artifact` ignores files inside hidden `.work/` by default. Manual runs have independent workflow concurrency groups and are not automatically cancelled. GitHub Releases publishing is not enabled.

Each matrix build compiles with `-j "$(nproc)"`, using the runner's available processing units instead of hardcoded parallelism. Jobs restore the shared LLVM toolchain and device-specific ccache, then **explicitly save ccache immediately after compilation and before packaging**. Partial compilation caches are also saved on ordinary build failures; cancelled jobs cannot reliably save their cache. The compiled `Image` and config are uploaded before ZIP packaging so packaging errors cannot discard finished kernel outputs. Official GitHub Actions use checkout v7, setup-python v7, cache v6, and upload-artifact v7. Capri has completed compilation and packaging on GitHub Actions, but physical boot remains unverified.

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

KernelForge invokes `CC=ccache clang`, `HOSTCC=ccache clang`, and `HOSTCXX=ccache clang++` in **both** Make invocations, while keeping `LLVM=1 LLVM_IAS=1`. No GCC toolchain is introduced. Local cache files are stored at `.work/_ccache`; CI overrides `CCACHE_DIR` to `.ccache` so GitHub Actions can persist the cache. Set `enabled: false` if ccache is unavailable; initial builds are typically cache misses, and linking and device-tree generation are not cached by ccache. This setup has passed local tests and dry-runs; the actual GitHub-hosted kernel compile still needs a successful workflow run.

## Device-specific AnyKernel3 packaging

`kernel.yaml` supplies the pinned AnyKernel3 template and common boot policy.
`devices/<codename>.yaml` supplies the source, configuration, image mapping and
installation policy. Source branches still track their latest `lineage-24.0`
head; adding matched modules does not disable DroidSpaces features such as
`CONFIG_SYSVIPC` and does not relax module CRC checks.

| Target | ZIP contents and installation |
|---|---|
| `lemonades` | Kernel `Image` only; existing boot/slot policy is unchanged. |
| `capri` | Kernel plus every module in `modules.order`; updates active-slot `boot`, `/vendor/lib/modules`, and the early modules in active-slot `vendor_boot`. Codename checking is required. |

Capri's `package.modules.aliases` follows the official LineageOS
[`TARGET_MODULE_ALIASES`](https://github.com/LineageOS/android_device_motorola_sm6225-common/blob/lineage-24.0/BoardConfigCommon.mk).
The inspected November 5 ROM contains 43 modules, including 29 renamed files.
`package.modules.vendor_boot` selects `nova_0flash_mmi`, `ili9882_mmi`, and `exfat`,
in that order, for normal early boot. Their copies in both module locations must
match the kernel. This policy is specific to Capri; do not copy another device's
empty `modules.load` or recovery-only load list.

Packaging requires the complete output tree: `Image`, `modules.order`, every
listed `.ko`, `Module.symvers`, and `include/config/kernel.release`. It verifies
module release strings, imported symbol CRCs and dependency closure before
staging. LLVM strips debug information from staged copies, not build originals;
already-signed modules cannot be stripped this way. Installed filenames,
dependency/alias metadata and checksum manifests are generated together.
The existing vendor `modules.load` is not replaced.

The recovery-only installer checks the active slot, vendor filesystem, module
coverage, payload checksums and vendor boot header v3 before changing the kernel.
It installs the matched vendor files with checked writes, updates `boot`, then
repackages `vendor_boot` with the selected early modules. Partition readback and
module checksums detect failed writes; shell failures attempt to restore the
original boot images and vendor files together. Temporary backups are retained
on failure. **This is not power-loss-atomic. Keep external backups and a working
recovery/fastboot restore method.**

No replacement DTB or DTBO is shipped. The installer retains unrelated ramdisk
contents and verifies the vendor boot DTB is unchanged. The kernel must remain
compatible with the ROM's device-tree data. Keep `CONFIG_CAPRI_DTB=y`; `CAPRIP`
belongs to the Moto G30 and is not a G10 Power variant.

**Before installing on Moto G10 Power:** confirm model `XT2127-4`, codename
`capri`, the LineageOS 24.0 Android 11 firmware baseline, and matching ROM images
from the [official variant-2 guide](https://lineageos.github.io/lineage_wiki/devices/capri/install/variant2/).
Use a compatible recovery that exposes the active vendor partition as ext4 and
can safely remount it writable. Verified/unsupported mappings, missing modules
and failed remounts must abort; the installer does not disable AVB or force-load
modules. Installation from a booted Android kernel-manager app is unsupported.
Never cross-flash Capri and OnePlus ZIPs.

Rebuild with `./build.sh build --device capri -j 8`, or repackage a complete
existing output tree with `./build.sh package --device capri`. GitHub Actions
retains Capri's modules and ABI inputs as `modules.tar.gz` alongside `Image` and
`kernel.config` **before** ZIP packaging. The earlier image-only downloads do
not contain the module set and cannot be repaired by repacking alone.

This follows the [Moto G84 matched-module installer pattern](https://github.com/humandroid-2002/AnyKernel3/tree/sm6375),
with Capri's actual filenames and normal early-load policy. Local packaging and
image-repacking checks do not establish physical boot compatibility; generated
ZIPs remain testing artifacts until a device boot and hardware check pass.

## Source credits

DroidSpaces configuration source: https://github.com/JackA1ltman/dreamworld_oneplus_sm8250/tree/staging — original author credit preserved in asset.
LXC cgroup change: Dreamworld commit 60c147cf1f33c09828128478ec602837e9279a05.
Systemd backports: https://github.com/Droidspaces/droidspaces-backports (0006 adapted as documented).

The repository has only local commits; no remote push or flashable device build has been completed here.
