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

GitHub Actions validates and dry-runs both device configurations on pull requests. Pushes to `main` build **both devices in separate matrix jobs**. For manual builds, go to **Actions → KernelForge → Run workflow** and choose `both` (default), `lemonades`, or `capri`; only the selected device jobs compile. Every successful compilation uploads an early `compiled-<device>-<run_number>` artifact containing `Image` and `kernel.config` **before AnyKernel3 packaging**. When packaging succeeds, a separate `droidspaces-<device>-<run_number>` artifact contains the flashable ZIP. Both have 14-day retention. Files are staged under visible `dist/` paths because `actions/upload-artifact` ignores files inside hidden `.work/` by default. Manual runs use independent workflow concurrency groups so running one device does not cancel or block another. GitHub Releases publishing is not enabled.

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

`kernel.yaml` provides the pinned AnyKernel3 template, common boot policy and `device_check: false`. Each `devices/<codename>.yaml` supplies the name, codename, Android version, kernel source, defconfig and image mapping. The generator creates `anykernel.sh` from the selected device rather than editing hardcoded scripts. For example, `devices/capri.yaml` contains:

~~~yaml
source:
  url: https://github.com/LineageOS/android_kernel_motorola_sm6225.git
  ref: lineage-24.0
  # Always track the latest lineage-24.0 branch head.
build:
  defconfig: vendor/bengal-perf_defconfig
  fragments:
    - vendor/ext_config/moto-bengal.config
    - vendor/ext_config/capri-default.config
  artifacts:
    - arch/arm64/boot/Image
package:
  output: DroidSpaces-capri.zip
  device:
    name: Motorola moto g10 power
    codenames: [capri]
    android_versions: ["17"]
  images:
    arch/arm64/boot/Image: Image
~~~

Generated installers default to `do.devicecheck=0` for both devices. Set `package.device_check: true` in shared `kernel.yaml` to enforce codenames; with the default off, the installer will **not** protect against accidentally flashing a ZIP on a different phone. Both use `boot` and automatic slot detection without Magisk. **Both devices now package only the kernel `Image`**, retaining the installed DTB and DTBO rather than shipping replacement images. This requires the new kernel to stay compatible with the current ROM's device-tree data. The official `capri-default.config` sets `CONFIG_CAPRI_DTB=y` for both G10 variants; **do not switch to `CONFIG_CAPRIP_DTB`**, which belongs to moto g30. AnyKernel3's ability to preserve the installed boot header v3, device-tree data and the matched vendor_boot/dtbo partitions still requires real-device testing on XT2127-4. **Never cross-flash** ZIPs between capri and lemonades.

**Moto g10 power verification before flashing:** confirm model `XT2127-4` and device codename `capri` (`adb shell getprop ro.product.device`); confirm the required Android 11 stock firmware baseline from the [LineageOS 24.0 variant-2 installation guide](https://lineageos.github.io/lineage_wiki/devices/capri/install/variant2/); retain matching boot/vendor_boot/dtbo images and a recovery method. The Power model has a 6000 mAh battery versus 5000 mAh on the regular G10, but LineageOS uses the same `capri` source/configuration, so **no separate Power-specific kernel fragment** is required.

For another device, create `devices/<codename>.yaml` with source URL and branch ref (no pinned kernel SHA), defconfig/fragments, and AnyKernel3 device/output/images. Verify its kernel config, cgroup patch applicability, boot partition, DTB/DTBO format and recovery plan. A ZIP alone does not prove flash compatibility.

Generated ZIPs are testing artifacts until a complete kernel build and physical boot pass. Rebuild packaging with `./build.sh package --device capri` or `--device lemonades`.

## Source credits

DroidSpaces configuration source: https://github.com/JackA1ltman/dreamworld_oneplus_sm8250/tree/staging — original author credit preserved in asset.
LXC cgroup change: Dreamworld commit 60c147cf1f33c09828128478ec602837e9279a05.
Systemd backports: https://github.com/Droidspaces/droidspaces-backports (0006 adapted as documented).

The repository has only local commits; no remote push or flashable device build has been completed here.
