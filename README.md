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
./build.sh build -j 8
~~~

The builder downloads a pinned kernel, applies patches in YAML order with git apply --check, copies the mandatory DroidSpaces fragment, merges all requested configs, checks the generated .config, and builds the kernel. Build results live under .work/kernel/out. The single pinned LLVM toolchain is stored under .work/_toolchains/kernel/llvm. Preparation resets and cleans only the managed .work/kernel/src checkout; don't hand-edit files there.

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
| 0003 | statx mount ID | No | Apply |
| 0004 | statx mount root | No | Apply |
| 0005 | utimensat AT_EMPTY_PATH | No | Apply |
| 0006 | faccessat2 | No | Apply with syscall-table adaptation |
| 0007–0008 | close_range | Yes | Skip |
| 0009 | rtnetlink nexthop | Yes | Skip |
| 0010 | x86 syscall entries | ARM64 kernel | Skip |

The original 0006 patch conflicts because LineageOS 24 already has clone3 (435), close_range (436), epoll_pwait2 (441) and syscall count 442. Its adapted version changes only the missing 439 entries while keeping these pre-existing entries and counts.

All four selected backports, plus the LXC cgroup naming patch, were successfully applied together to a staged Git index of the exact source snapshot. The combined patchset changes 12 files, but has **not been kernel-compiled or device-boot-tested** here. For systemd 257 or earlier, these systemd-specific backports may not be necessary. For systemd 260–262, upstream also requires a sufficiently recent DroidSpaces userspace runtime; the new mount API is addressed on the userspace side.

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
    enabled: true
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

The GitHub Actions workflow (`.github/workflows/kernel.yml`) validates configuration and tests on pull requests. On pushes to `main` and manual `Actions → KernelForge → Run workflow` events it additionally compiles the one kernel defined in `kernel.yaml`, with DroidSpaces always enabled. Successful builds upload `Image`, `dtb`, `dtbo.img`, and the resolved kernel `.config` as downloadable workflow artifacts (retained for 14 days). No generic flashable ZIP is produced.

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

## Safe packaging

Packaging is disabled. For a specific device, supply a full AnyKernel3 template and configure:

~~~yaml
package:
  enabled: true
  template: packaging/my-device-anykernel3
  images:
    Image: Image
    dtb: dtb
    dtbo.img: dtbo.img
~~~

The template must contain anykernel.sh with do.devicecheck=1. Run ./build.sh package only after compiling and verifying the exact target device, DTB, DTBO, boot partition and recovery plan. Unlike Dreamworld's standalone script, this project never disables device checks.

## Source credits

DroidSpaces configuration source: https://github.com/JackA1ltman/dreamworld_oneplus_sm8250/tree/staging — original author credit preserved in asset.
LXC cgroup change: Dreamworld commit 60c147cf1f33c09828128478ec602837e9279a05.
Systemd backports: https://github.com/Droidspaces/droidspaces-backports (0006 adapted as documented).

This is a local Git repository. No GitHub remote was created and no flashable image was generated.
