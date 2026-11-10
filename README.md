# KernelForge — DroidSpaces-first Android kernel builder

**One kernel.yaml. No profiles. DroidSpaces configuration is always applied.**

KernelForge compiles a selected Android kernel with mandatory DroidSpaces support and ordered, optional kernel patches. Start with the official LineageOS 24.0 SM8250 kernel (Linux 4.19.325), not the entire Dreamworld fork. To target another kernel, edit the source URL, ref, compiler and configuration in kernel.yaml, and re-evaluate the optional patches.

## Run

Linux x86_64 with Python 3.10+, Git, Make, `ccache`, and standard kernel build dependencies (CI installs these automatically):

~~~bash
python3 -m pip install -r requirements.txt
./build.sh validate
./build.sh build --dry-run
./build.sh prepare
./build.sh build -j 8        # Compiles and always generates an AnyKernel3 ZIP
./build.sh package           # Rebuild only the ZIP from existing kernel outputs
~~~

The builder downloads a pinned kernel, applies patches in YAML order with git apply --check, copies the mandatory DroidSpaces fragment, merges all requested configs, checks the generated .config, builds the kernel, and **automatically packages AnyKernel3**. Build results live under .work/kernel/out and the YAML-named ZIP under .work/kernel/. The single pinned LLVM toolchain is stored under .work/_toolchains/kernel/llvm. Preparation resets and cleans only the managed .work/kernel/src checkout; don't hand-edit files there.

The commands are not a complete LineageOS ROM build. Integration into an Android device tree, SELinux, permissions and userspace runtime remains separate.

## Layout

~~~text
kernel.yaml                      One configuration file — no profiles
build.sh                         Command-line entrypoint
kernelforge/cli.py               Source / patch / config / build / package
assets/droidspaces.config        Always included
patches/0001-lxc-cgroup-prefix.patch
patches/backports-4.19/          Four systemd backport patches
schema/kernel.schema.json        Optional editor schema
tests/                           Validation tests
.github/workflows/kernel.yml     CI validation and on-demand build
.work/                           Generated files, ignored
~~~

## Specific 4.19 backports for LineageOS 24

Source: https://github.com/Droidspaces/droidspaces-backports/tree/main/kernel-4.19-series

The upstream 4.19 series has ten patches to help old Android kernels support systemd 258–262. We ran its feature probe on LineageOS SM8250 4.19.325 and found:

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

All four backports plus the LXC cgroup naming patch were previously checked together against the staged source index, but the kernel has **not been compiled or device-boot-tested** here. By default, **all four modern-systemd backports (0003–0006) are now disabled**; only the LXC cgroup patch stays enabled. For systemd 257 or earlier, these backports may be unnecessary. For systemd 260–262, reassess and enable the missing features, including 0003–0006. Upstream also requires a sufficiently recent DroidSpaces userspace runtime; the new mount API is addressed on the userspace side.

**When changing source kernels, re-run upstream tools/probe-kernel.sh and adjust the patches list.** Do not blindly apply all 4.19 patches to every vendor tree.

## Declarative patches

All patch entries in kernel.yaml can be enabled or disabled. The mandatory DroidSpaces fragment cannot be disabled. A patch that is missing or fails to apply aborts the build; none of these changes are committed into the upstream clone.

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

The official LineageOS 24 SM8250 kernel `build.config.common` specifies `clang-r416183b` with `LLVM=1`. This builder now uses **one pinned LLVM toolchain** for the entire build: Clang/Clang++ for compiling, LLD for linking, llvm-ar/llvm-nm/llvm-objcopy and the other LLVM binutils. Both configuration and compilation use `LLVM=1 LLVM_IAS=1`. No separate GCC ARM64/ARM32 compilers or cross-compile prefixes.

The bundled prebuilt is for Linux x86_64. For a native ARM64 build host, provide a compatible full LLVM toolchain (including `ld.lld` and LLVM binutils), not merely a `clang` binary:

~~~bash
export KERNELFORGE_LLVM_BIN=/opt/llvm/bin
./build.sh build -j 8
~~~

The `kernel.yaml` source and LLVM toolchain are both pinned to full Git commit hashes. If switching to another kernel version, check that it supports Clang's integrated assembler and the LLVM utilities before building. The build setup is not yet compiler-tested end to end.

## GitHub Actions and ccache

The GitHub Actions workflow (`.github/workflows/kernel.yml`) validates configuration and tests on pull requests. On pushes to `main` and manual `Actions → KernelForge → Run workflow` events it additionally compiles the one kernel defined in `kernel.yaml` and **automatically creates a YAML-configured AnyKernel3 ZIP**, with DroidSpaces always enabled. Successful builds upload the generated `*.zip`, raw kernel images, and resolved `.config` as downloadable workflow artifacts (retained for 14 days). ZIP naming and image mappings are configured only in YAML; this workflow is not hardcoded to OnePlus 9R. GitHub Releases publishing remains separate and is not enabled.

The build job installs `ccache` and dependencies, restores the pinned LLVM toolchain checkout and a compressed ccache directory using `actions/cache@v4`, resets hit/miss counters, builds with the pinned Clang toolchain, and prints cache statistics to both the job log and GitHub Actions step summary. The LLVM cache key follows the toolchain commit. Ccache keys include the runner OS/architecture and a configuration hash; a unique run ID permits refreshing the cache after every successful build. An older cache can be restored when configuration changes; ccache validates the compiler and inputs itself. The ccache limit defaults to 2 GiB via `kernel.yaml`; GitHub's cache retention/eviction limits still apply.

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

## Dynamic AnyKernel3 packaging — one YAML for any target

Every successful `./build.sh build` produces a ZIP using `package` in the **same** `kernel.yaml`. There are no device-specific scripts, hardcoded Python models, or separate profiles. KernelForge fetches the pinned upstream [AnyKernel3](https://github.com/osm0sis/AnyKernel3) template and **generates `anykernel.sh`** from the declared device and boot partition. The OnePlus 9R settings below match the current initial target; changing them changes ZIP contents automatically.

~~~yaml
package:
  type: anykernel3
  output: DroidSpaces-lemonades.zip
  device_check: false       # do.devicecheck=0 (default); true enforces codenames
  device:
    name: OnePlus 9R
    codenames: [lemonades]
    android_versions: ["17"]
  anykernel:
    url: https://github.com/osm0sis/AnyKernel3.git
    ref: master
    commit: dca9dc370838d919d56c1f59ec78b27a14a72c68
  boot:
    partition: boot           # block by-name
    slots: auto               # auto, ab, single
    ramdisk: auto             # auto, gz, lz4, ...
    vbmeta: auto              # auto, "0", "1"
    install: write_boot      # write_boot or flash_boot
  images:                    # output relative to .work/kernel/out → ZIP destination
    arch/arm64/boot/Image: Image
    arch/arm64/boot/dtb: dtb
    arch/arm64/boot/dtbo.img: dtbo.img
~~~

The generated script sets `do.devicecheck=0` **by default**, so AnyKernel3 skips codename checking. To enforce permitted codenames, set `package.device_check: true` in YAML, which generates `do.devicecheck=1`. The device codename and Android version fields are still generated, and the selected boot partition/slot policy is used. Disabling the codename check increases the risk of flashing an incompatible kernel onto another device. `do.modules=0` and `do.systemless=0`; no Magisk dependency is added. With `write_boot`, AnyKernel3 repacks the existing boot image and flashes bundled `dtbo.img` through its pinned core. The `flash_boot` alternative uses `split_boot`, `flash_boot` and `flash_dtbo`.

For another device, update **both** the kernel source/defconfig/patches and this package block. Set the actual `ro.product.device` codenames, verified Android versions, boot partition and A/B layout, correct kernel image filenames, and the required DTB/DTBO outputs. AnyKernel3 handles many device layouts, but cannot make mismatched vendor kernels, boot headers, image formats or uncommon GKI/vendor_boot arrangements compatible automatically. **Do not flash** until those inputs and a boot/recovery plan are verified on the physical device.

The generated ZIP is intended for testing only until the kernel compiles and boots successfully. GitHub Actions stores it as a workflow artifact, not a release. Use `./build.sh package` when you only need to repack already-built images.

## Source credits

DroidSpaces configuration source: https://github.com/JackA1ltman/dreamworld_oneplus_sm8250/tree/staging — original author credit preserved in asset.
LXC cgroup change: Dreamworld commit 60c147cf1f33c09828128478ec602837e9279a05.
Systemd backports: https://github.com/Droidspaces/droidspaces-backports (0006 adapted as documented).

This is a local Git repository. No GitHub remote was created and no flashable image was generated.
