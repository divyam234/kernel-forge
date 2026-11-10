# KernelForge — DroidSpaces-first Android kernel builder

**One kernel.yaml. No profiles. DroidSpaces configuration is always applied.**

KernelForge compiles a selected Android kernel with mandatory DroidSpaces support and ordered, optional kernel patches. Start with the official LineageOS 24.0 SM8250 kernel (Linux 4.19.325), not the entire Dreamworld fork. To target another kernel, edit the source URL, ref, compiler and configuration in kernel.yaml, and re-evaluate the optional patches.

## Run

Linux x86_64 with Python 3.10+, Git, Make, standard kernel build dependencies:

~~~bash
python3 -m pip install -r requirements.txt
./build.sh validate
./build.sh build --dry-run
./build.sh prepare
./build.sh build -j 8
~~~

The builder downloads a pinned kernel, applies patches in YAML order with git apply --check, copies the mandatory DroidSpaces fragment, merges all requested configs, checks the generated .config, and builds the kernel. Build results live under .work/kernel/out. Toolchains are downloaded into .work/_toolchains/kernel. Preparation resets and cleans only the managed .work/kernel/src checkout; don't hand-edit files there.

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

The default toolchains reproduce the *approach* in Dreamworld staging: LineageOS Clang plus Android GCC 4.9 ARM64 and ARM32. They contain Linux x86_64 binaries. For an ARM64 *build host* you must supply native compatible toolchains using:

~~~bash
export KERNELFORGE_CLANG_BIN=/opt/clang/bin
export KERNELFORGE_GCC64_BIN=/opt/aarch64-gcc/bin
export KERNELFORGE_GCC32_BIN=/opt/arm32-gcc/bin
./build.sh build -j 8
~~~

The kernel is pinned by SHA. For reproducibility, pin each toolchain by optional full commit SHA under toolchain sections, too.

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
