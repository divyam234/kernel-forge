# KernelForge — DroidSpaces-first Android kernel builder

**Shared `kernel.yaml` plus a small YAML file per device. DroidSpaces is mandatory for every target.**

KernelForge builds LineageOS 24.0 kernels with shared DroidSpaces config, a common LXC cgroup patch, optional systemd backports, one pinned LLVM toolchain, and device-specific AnyKernel3 ZIPs. Supported targets: OnePlus 9R (`lemonades`, SM8250) and Motorola moto g10 (`capri`, Snapdragon SM4250, using the SM6225/Bengal-family source tree). Each uses its official LineageOS source and defconfig; **no OnePlus kernel settings are used on capri**.

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

The builder merges `kernel.yaml` with `devices/<codename>.yaml`, fetches the device's pinned source, applies the enabled shared patches, copies the mandatory shared DroidSpaces fragment, merges device-specific defconfig fragments, verifies `.config`, compiles and packages AnyKernel3. Workspaces are isolated: `.work/kernel/` for lemonades and `.work/kernel-capri/` for capri. LLVM is shared at `.work/_toolchains/kernel/llvm`; ccache is shared at `.work/_ccache`. Preparation resets only the selected managed source checkout.

The commands are not a complete LineageOS ROM build. Integration into an Android device tree, SELinux, permissions and userspace runtime remains separate.

## Layout

~~~text
kernel.yaml                      Shared LLVM, DroidSpaces, patches, AnyKernel3 defaults
devices/lemonades.yaml            OnePlus 9R kernel and package settings
devices/capri.yaml                Motorola moto g10 kernel and package settings
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

KernelForge uses a single pinned `clang-r416183b` LLVM toolchain for all devices, with `LLVM=1 LLVM_IAS=1` for config and compilation. This compiler matches the original OnePlus SM8250 kernel setup; compatibility with Motorola's full kernel build still needs testing. No GCC cross-toolchain is mixed in.

The bundled prebuilt is for Linux x86_64. For a native ARM64 build host, provide a compatible full LLVM toolchain (including `ld.lld` and LLVM binutils), not merely a `clang` binary:

~~~bash
export KERNELFORGE_LLVM_BIN=/opt/llvm/bin
./build.sh build -j 8
~~~

The LLVM checkout is shared, while source/defconfig and build workspaces are device specific. Both kernel sources are pinned to known commits; update the pins deliberately and verify compiler compatibility before flashing.

## GitHub Actions and ccache

GitHub Actions validates and dry-runs both devices on pull requests. Pushes to `main` and manual workflow runs build **lemonades and capri in separate matrix jobs**, each producing its own AnyKernel3 ZIP, kernel images and `.config` artifact (14-day retention). GitHub Releases publishing is not enabled.

Each matrix build restores the shared pinned LLVM toolchain and its own ccache key (device + configuration hash), prints cache statistics and uploads a device-labeled artifact. Ccache validates compiler/options before reuse. A full build and hardware boot have not been confirmed for capri.

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
  commit: 1da2d0861244d324cfff0fcb31dec374dd94436b
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
    name: Motorola moto g10
    codenames: [capri]
    android_versions: ["17"]
  images:
    arch/arm64/boot/Image: Image
~~~

Generated installers default to `do.devicecheck=0` for both devices. Set `package.device_check: true` in shared `kernel.yaml` to enforce the selected codename. Both use `boot` and automatic slot detection without Magisk. **Capri packages only the kernel `Image`**: the existing DTB and DTBO are retained because this kernel source does not produce verified combined images for standalone packaging. Capri's boot-header-v3 and vendor_boot arrangement still require real-device testing. **Never cross-flash** ZIPs between capri and lemonades.

For another device, create `devices/<codename>.yaml` with source URL/ref/commit, defconfig/fragments, AnyKernel3 device/output/images. Verify its kernel config, cgroup patch applicability, boot partition, DTB/DTBO format and recovery plan. A ZIP alone does not prove flash compatibility.

Generated ZIPs are testing artifacts until a complete kernel build and physical boot pass. Rebuild packaging with `./build.sh package --device capri` or `--device lemonades`.

## Source credits

DroidSpaces configuration source: https://github.com/JackA1ltman/dreamworld_oneplus_sm8250/tree/staging — original author credit preserved in asset.
LXC cgroup change: Dreamworld commit 60c147cf1f33c09828128478ec602837e9279a05.
Systemd backports: https://github.com/Droidspaces/droidspaces-backports (0006 adapted as documented).

The repository has only local commits; no remote push or flashable device build has been completed here.
