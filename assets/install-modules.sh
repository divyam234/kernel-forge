#!/sbin/sh
# Sourced by anykernel.sh after ak3-core.sh. Never invoke as a standalone flasher.

_kf_fail() { abort "KernelForge: $*"; }

# These file-only helpers also permit host-side behavioral tests, without block overrides.
_kf_verify() (
  cd "$1" || exit 1
  [ -s "$2" ] || exit 1
  while read -r digest name extra; do
    case "$digest" in *[!0-9a-f]*|'') exit 1;; esac
    [ "${#digest}" -eq 64 ] && [ -z "$extra" ] || exit 1
    case "$name" in ''|*[!a-zA-Z0-9_.-]*|.*) exit 1;; esac
    [ -f "$name" ] && [ ! -L "$name" ] || exit 1
  done < "$2"
  sha256sum -c "$2" >/dev/null
)

_kf_same_modules() (
  for directory in "$1" "$2"; do
    [ -d "$directory" ] || exit 1
    for module in "$directory"/*.ko; do
      [ -f "$module" ] && [ ! -L "$module" ] || exit 1
      basename "$module"
    done > "$3.unsorted" || exit 1
    LC_ALL=C sort "$3.unsorted" > "$3.$([ "$directory" = "$1" ] && echo source || echo target)" || exit 1
  done
  cmp -s "$3.source" "$3.target"
)

_kf_payload_complete() (
  directory=$1 manifest=$2
  for file in "$directory"/*.ko "$directory/modules.dep" "$directory/modules.alias" "$directory/modules.softdep"; do
    [ -f "$file" ] || exit 1
    name=$(basename "$file")
    grep -Eq "^[0-9a-f]{64}  $name$" "$manifest" || exit 1
  done
  while read -r digest name extra; do
    case "$name" in *.ko|modules.dep|modules.alias|modules.softdep|modules.load) :;; *) exit 1;; esac
  done < "$manifest"
)

_kf_copy_files() (
  source=$1 target=$2 reference=$3 manifest=$4
  while read -r digest name extra; do
    [ ! -L "$target/$name" ] || exit 1
    cp -f "$source/$name" "$target/$name" || exit 1
    chown 0:0 "$target/$name" && chmod 0644 "$target/$name" || exit 1
    if [ -e "$reference/$name" ] && chcon --reference="$reference/$name" "$target/$name" 2>/dev/null; then
      :
    elif command -v restorecon >/dev/null 2>&1 && restorecon "$target/$name"; then
      :
    else
      chcon u:object_r:vendor_file:s0 "$target/$name" || exit 1
    fi
  done < "$manifest"
  _kf_verify "$target" "$manifest"
)

_kf_restore_files() (
  target=$1 backup=$2 manifest=$3
  failed=0
  while read -r digest name extra; do
    if [ -e "$backup/$name" ]; then
      cp -af "$backup/$name" "$target/$name" || failed=1
    else
      rm -f "$target/$name" || failed=1
    fi
  done < "$manifest"
  return "$failed"
)

_kf_save_labels() (
  directory=$1 manifest=$2
  while read -r digest name extra; do
    [ -e "$directory/$name" ] || continue
    [ ! -L "$directory/$name" ] || exit 1
    label=$(ls -Zd "$directory/$name") || exit 1
    label=${label%% *}
    case "$label" in *:*:*:*) printf '%s %s\n' "$label" "$name";;
      *) exit 1;;
    esac
  done < "$manifest"
)

_kf_apply_labels() (
  failed=0
  while read -r label name; do
    chcon "$label" "$1/$name" || failed=1
  done < "$2"
  return "$failed"
)

_kf_original_checksums() (
  cd "$1" || exit 1
  while read -r digest name extra; do
    [ -e "$name" ] || continue
    sha256sum "$name" || exit 1
  done < "$2"
)

_kf_remount_ro() {
  [ "$kf_remounted" = 1 ] || return 0
  mount -o remount,ro /vendor || return 1
  kf_remounted=0
}

# Restore a coherent installation, not just modules paired with a new kernel.
_kf_restore_install() {
  local backup=$1 boot=$2 vendor_boot=$3 vendor_modules=$4 manifest=$5 failed=0
  dd if="$backup/boot.img" of="$boot" bs=1048576 conv=fsync || failed=1
  dd if="$backup/vendor_boot.img" of="$vendor_boot" bs=1048576 conv=fsync || failed=1
  _kf_restore_files "$vendor_modules" "$backup/modules" "$manifest" || failed=1
  _kf_apply_labels "$vendor_modules" "$backup/labels" || failed=1
  sync || failed=1
  cmp -s "$backup/boot.img" "$boot" || failed=1
  cmp -s "$backup/vendor_boot.img" "$vendor_boot" || failed=1
  _kf_verify "$vendor_modules" "$backup/original.sha256" || failed=1
  return "$failed"
}

_kf_exit() {
  kf_status=$1
  trap - EXIT HUP INT TERM
  if [ "$kf_writes" = 1 ]; then
    ui_print "KernelForge: installation failed; restoring BOTH partitions and vendor modules."
    if ! _kf_restore_install "$kf_backup" "$kf_boot" "$kf_vendor_boot" /vendor/lib/modules "$kf_vendor_manifest"; then
      ui_print "ROLLBACK FAILED. DO NOT REBOOT. Restore external boot, vendor_boot and vendor backups."
    else
      ui_print "KernelForge: original partitions and module files restored."
    fi
  fi
  _kf_remount_ro || ui_print "KernelForge: WARNING: could not restore /vendor read-only mount."
  [ "$kf_status" -ne 0 ] || kf_status=1
  exit "$kf_status"
}

_kf_unpack_vendor() (
  mkdir -p "$2" && cd "$2" || exit 1
  # Vendor boot v3: VNDRBOOT magic, then little-endian uint32 header version.
  [ "$(dd if="$1" bs=1 count=8 2>/dev/null)" = VNDRBOOT ] || exit 1
  [ "$(od -An -tu4 -j8 -N4 "$1" | tr -d ' \n')" = 3 ] || exit 1
  "$BIN/magiskboot" unpack -h "$1" > unpack.log 2>&1
  result=$?
  [ "$result" -eq 3 ] && [ -s ramdisk.cpio ] && [ -s dtb ] || exit 1
  mkdir extracted && cd extracted || exit 1
  "$BIN/magiskboot" cpio ../ramdisk.cpio extract || exit 1
  [ -d lib/modules ]
)

# Accept only complete, numeric dm-linear rows from either Android dmctl or
# dmsetup. In particular, a second non-linear target must not hide behind one
# occurrence of the word "linear".
_kf_linear_table() (
  mapping=$1
  rows=0
  set -f
  while IFS= read -r row || [ -n "$row" ]; do
    [ -n "$row" ] || continue
    [ "$row" != "Targets in the device-mapper table for $mapping:" ] || continue
    set -- $row
    case "$#" in
      4)
        case "$1" in *-*: ) :;; *) exit 1;; esac
        [ "$2" = linear, ] || exit 1
        range=${1%:}
        start=${range%%-*}
        length=${range#*-}
        device=$3 offset=$4
        ;;
      5)
        [ "$3" = linear ] || exit 1
        start=$1 length=$2 device=$4 offset=$5
        ;;
      *) exit 1;;
    esac
    major=${device%:*} minor=${device#*:}
    [ "$major" != "$device" ] || exit 1
    for number in "$start" "$length" "$offset" "$major" "$minor"; do
      case "$number" in ''|*[!0-9]*) exit 1;; esac
    done
    [ "$length" -gt 0 ] || exit 1
    rows=$((rows + 1))
  done
  [ "$rows" -gt 0 ]
)

_kf_check_verity() {
  # An unlocked/orange boot state alone says nothing about dm-verity enforcement.
  case "$(getprop ro.boot.veritymode)" in enforcing|eio|logging)
    _kf_fail "dm-verity is enabled; use a recovery with an explicitly writable, non-verity vendor mapping.";;
  esac
  case "$(getprop ro.boot.verifiedbootstate)" in green|yellow)
    _kf_fail "verified boot is locked/verified; refusing direct vendor changes.";;
  esac
  kf_dm=$(basename "$(readlink -f "$kf_vendor_source")")
  case "$kf_dm" in dm-*)
    kf_dm_name=$(cat "/sys/block/$kf_dm/dm/name") || _kf_fail "Cannot inspect vendor dm name."
    kf_dm_uuid=$(cat "/sys/block/$kf_dm/dm/uuid") || _kf_fail "Cannot inspect vendor dm UUID."
    case "$kf_dm_uuid" in *[Vv][Ee][Rr][Ii][Tt][Yy]*) _kf_fail "Vendor uses a verity mapping.";; esac
    kf_table=
    if command -v dmsetup >/dev/null 2>&1; then
      kf_table=$(dmsetup table "$kf_dm_name" 2>/dev/null) || kf_table=
    fi
    if [ -z "$kf_table" ] && command -v dmctl >/dev/null 2>&1; then
      kf_table=$(dmctl table "$kf_dm_name" 2>/dev/null) || kf_table=
    fi
    [ -n "$kf_table" ] || _kf_fail "Cannot inspect vendor dm table; recovery needs dmctl or dmsetup (plain UUID is not proof of dm-linear)."
    _kf_linear_table "$kf_dm_name" <<EOF || _kf_fail "Vendor dm table contains unsupported or malformed target rows."
$kf_table
EOF
    # Fail closed on layered device-mapper backing. A plain top-level linear
    # map must never conceal a lower verity/crypt/snapshot mapping.
    [ -r "/sys/block/$kf_dm/slaves" ] || _kf_fail "Cannot inspect vendor dm backing devices."
    kf_slave_count=0
    for kf_slave in "/sys/block/$kf_dm/slaves/"*; do
      [ -e "$kf_slave" ] || continue
      kf_slave_count=$((kf_slave_count + 1))
      case "$(basename "$(readlink -f "$kf_slave")")" in
        dm-*) _kf_fail "Layered vendor dm backing is unsupported; use a directly linear recovery mapping.";;
      esac
    done
    [ "$kf_slave_count" -gt 0 ] || _kf_fail "Vendor dm mapping has no inspectable backing devices."
  esac
}

# The mounted vendor filesystem must belong to the same slot as both boot images.
_kf_vendor_slot_matches() (
  source=$1 expected=$2 mapping=$3 slot=$4
  case "$slot" in _a|_b) :;; *) exit 1;; esac
  [ -z "$mapping" ] || [ "$mapping" = "vendor$slot" ] || exit 1
  if [ -n "$expected" ]; then
    [ "$(readlink -f "$source")" = "$(readlink -f "$expected")" ]
  else
    [ "$mapping" = "vendor$slot" ]
  fi
)

kernelforge_begin_install() {
  for kf_command in getprop readlink blockdev dd cmp cp chown chmod chcon ls sha256sum head od tr du df cut tail sort sync mount; do
    command -v "$kf_command" >/dev/null 2>&1 || _kf_fail "Recovery lacks required command: $kf_command"
  done
  [ -x "$BIN/magiskboot" ] || _kf_fail "Bundled magiskboot is missing or not executable."
  [ "$(getprop sys.boot_completed)" != 1 ] || _kf_fail "Recovery-only installer; Android is booted."
  for kf_service in zygote zygote_secondary; do
    case "$(getprop "init.svc.$kf_service")" in running|restarting) _kf_fail "Recovery-only installer; zygote is active.";; esac
  done
  case "$SLOT" in _a|_b) :;; *) _kf_fail "Expected active A/B slot from AnyKernel core.";; esac
  kf_active=$(getprop ro.boot.slot_suffix)
  [ -n "$kf_active" ] || kf_active=_$(getprop ro.boot.slot)
  [ "$kf_active" = "$SLOT" ] || _kf_fail "AnyKernel slot does not match the active slot."
  kf_boot=$(readlink -f "$BLOCK")
  [ -b "$kf_boot" ] || _kf_fail "Active boot block device is missing."
  kf_expected_boot=$(readlink -f "/dev/block/bootdevice/by-name/boot$SLOT")
  [ -b "$kf_expected_boot" ] || kf_expected_boot=$(readlink -f "/dev/block/by-name/boot$SLOT")
  [ "$kf_boot" = "$kf_expected_boot" ] || _kf_fail "AnyKernel BLOCK is not the active boot partition."
  kf_vendor_boot=$(readlink -f "$(dirname "$BLOCK")/vendor_boot$SLOT")
  # BLOCK may already be canonicalized by AK3; its directory then has no by-name entries.
  if [ ! -b "$kf_vendor_boot" ]; then
    kf_vendor_boot=$(readlink -f "/dev/block/bootdevice/by-name/vendor_boot$SLOT")
  fi
  [ -b "$kf_vendor_boot" ] && [ "$kf_boot" != "$kf_vendor_boot" ] || _kf_fail "Distinct active vendor_boot partition not found."
  kf_vendor_payload=$AKHOME/module-files/vendor/lib/modules
  kf_early_payload=$AKHOME/vendor_boot-files/ramdisk/lib/modules
  kf_vendor_manifest=$kf_vendor_payload/modules.sha256
  kf_early_manifest=$AKHOME/module-files/early.sha256
  [ -d /vendor/lib/modules ] && [ ! -L /vendor/lib/modules ] || _kf_fail "Vendor module directory is missing or symlinked."
  _kf_verify "$kf_vendor_payload" "$kf_vendor_manifest" || _kf_fail "Vendor payload checksum validation failed."
  _kf_verify "$kf_early_payload" "$kf_early_manifest" || _kf_fail "Early module payload checksum validation failed."
  _kf_payload_complete "$kf_vendor_payload" "$kf_vendor_manifest" && _kf_payload_complete "$kf_early_payload" "$kf_early_manifest" || _kf_fail "Incomplete module manifests."
  [ -f "$kf_early_payload/modules.load" ] && [ ! -e "$kf_vendor_payload/modules.load" ] || _kf_fail "Incorrect modules.load payload policy."
  grep -Eq '^[0-9a-f]{64}  modules\.load$' "$kf_early_manifest" || _kf_fail "Early modules.load is not checksummed."
  kf_backup=$AKHOME/kernelforge-backup
  [ ! -e "$kf_backup" ] || _kf_fail "Backup directory already exists; use a fresh extraction."
  mkdir "$kf_backup" || _kf_fail "Cannot create local backup directory."
  _kf_same_modules "$kf_vendor_payload" /vendor/lib/modules "$kf_backup/inventory" || _kf_fail "ROM vendor .ko names differ from the payload; this ZIP is not matched to this ROM."
  # Record mount information without assuming an exported recovery BOOTMODE.
  kf_vendor_source= kf_vendor_type= kf_vendor_options=
  while read -r source mountpoint fstype options rest; do
    if [ "$mountpoint" = /vendor ]; then
      kf_vendor_source=$source kf_vendor_type=$fstype kf_vendor_options=$options
      break
    fi
  done < /proc/mounts
  [ "$kf_vendor_type" = ext4 ] || _kf_fail "A separately mounted ext4 /vendor is required."
  kf_dm_name=
  _kf_check_verity
  kf_expected_vendor=
  for candidate in "/dev/block/mapper/vendor$SLOT" "/dev/block/by-name/vendor$SLOT" "/dev/block/bootdevice/by-name/vendor$SLOT"; do
    if [ -b "$candidate" ]; then
      kf_expected_vendor=$candidate
      break
    fi
  done
  _kf_vendor_slot_matches "$kf_vendor_source" "$kf_expected_vendor" "$kf_dm_name" "$SLOT" ||
    _kf_fail "Mounted vendor does not belong to the active slot; refusing a mixed-slot installation."
  kf_boot_size=$(blockdev --getsize64 "$kf_boot") || _kf_fail "Cannot size boot partition."
  kf_vendor_boot_size=$(blockdev --getsize64 "$kf_vendor_boot") || _kf_fail "Cannot size vendor_boot partition."
  kf_modules_kb=$(du -sk /vendor/lib/modules | cut -f1)
  kf_free_kb=$(df -Pk "$AKHOME" | tail -n 1 | tr -s ' ' | cut -d' ' -f4)
  case "$kf_free_kb:$kf_modules_kb:$kf_boot_size:$kf_vendor_boot_size" in *[!0-9:]*) _kf_fail "Cannot determine backup storage.";; esac
  # Originals plus independent post-write readback/unpack and conservative repack headroom.
  kf_required_kb=$(( (kf_boot_size * 3 + kf_vendor_boot_size * 4) / 1024 + kf_modules_kb * 3 + 65536 ))
  [ "$kf_free_kb" -ge "$kf_required_kb" ] || _kf_fail "Insufficient AKHOME storage for full backups and verification."
  dd if="$kf_boot" of="$kf_backup/boot.img" bs=1048576 || _kf_fail "Boot backup failed."
  dd if="$kf_vendor_boot" of="$kf_backup/vendor_boot.img" bs=1048576 || _kf_fail "Vendor boot backup failed."
  cmp -s "$kf_boot" "$kf_backup/boot.img" && cmp -s "$kf_vendor_boot" "$kf_backup/vendor_boot.img" || _kf_fail "Partition backup readback mismatch."
  cp -a /vendor/lib/modules "$kf_backup/modules" || _kf_fail "Vendor modules backup (including attributes) failed."
  _kf_save_labels /vendor/lib/modules "$kf_vendor_manifest" > "$kf_backup/labels" || _kf_fail "Cannot record original vendor SELinux labels."
  _kf_original_checksums /vendor/lib/modules "$kf_vendor_manifest" > "$kf_backup/original.sha256" || _kf_fail "Cannot checksum original vendor modules."
  _kf_verify "$kf_backup/modules" "$kf_backup/original.sha256" || _kf_fail "Vendor module backup readback mismatch."
  _kf_unpack_vendor "$kf_backup/vendor_boot.img" "$kf_backup/vendor-before" || _kf_fail "Unsupported vendor_boot; expected header v3, DTB and a single extractable ramdisk."
  _kf_same_modules "$kf_early_payload" "$kf_backup/vendor-before/extracted/lib/modules" "$kf_backup/early-inventory" || _kf_fail "Vendor boot early module names differ from this payload."
  [ "$(cat "$kf_early_payload/modules.load")" = "$(cat "$kf_backup/vendor-before/extracted/lib/modules/modules.load")" ] || _kf_fail "Original vendor_boot early load order differs from this payload."
  kf_remounted=0 kf_writes=0
  trap '_kf_exit $?' EXIT
  trap 'exit 1' HUP INT TERM
  case ",$kf_vendor_options," in *,ro,*)
    mount -o remount,rw /vendor || _kf_fail "Vendor remount failed; no boot partitions changed."
    kf_remounted=1;;
  esac
  # Confirm writable before either AnyKernel write_boot phase, without touching ROM files.
  kf_probe=/vendor/lib/modules/.kernelforge-write-probe
  [ ! -e "$kf_probe" ] && (umask 077; set -C; : > "$kf_probe") && rm "$kf_probe" || _kf_fail "Vendor is not writable."
  kf_vendor_free=$(df -Pk /vendor | tail -n 1 | tr -s ' ' | cut -d' ' -f4)
  kf_payload_kb=$(du -sk "$kf_vendor_payload" | cut -f1)
  [ "$kf_vendor_free" -ge "$kf_payload_kb" ] || _kf_fail "Vendor lacks free space for safe module replacement."
  kf_writes=1
  _kf_copy_files "$kf_vendor_payload" /vendor/lib/modules "$kf_backup/modules" "$kf_vendor_manifest" || _kf_fail "Vendor module installation failed."
  _kf_apply_labels /vendor/lib/modules "$kf_backup/labels" || _kf_fail "Cannot restore original vendor SELinux contexts."
  sync || _kf_fail "Vendor module sync failed."
}

_kf_image_digest() {
  head -c "$2" "$1" | sha256sum | cut -d' ' -f1
}

kernelforge_check_boot() {
  [ "$kf_writes" = 1 ] && [ "$(readlink -f "$BLOCK")" = "$kf_boot" ] || _kf_fail "Unexpected boot write target."
  [ -s "$AKHOME/boot-new.img" ] || _kf_fail "AnyKernel repacked boot image is missing."
  kf_new_boot_size=$(wc -c < "$AKHOME/boot-new.img")
  kf_new_boot_digest=$(sha256sum "$AKHOME/boot-new.img" | cut -d' ' -f1)
  [ "$(_kf_image_digest "$kf_boot" "$kf_new_boot_size")" = "$kf_new_boot_digest" ] || _kf_fail "Boot partition readback differs from repacked image."
}

kernelforge_prepare_vendor_boot() {
  [ "$kf_writes" = 1 ] && [ "$(readlink -f "$BLOCK")" = "$kf_vendor_boot" ] || _kf_fail "Unexpected vendor_boot write target."
  [ -d "$RAMDISK/lib/modules" ] || _kf_fail "Unpacked vendor ramdisk lacks lib/modules."
  _kf_same_modules "$kf_early_payload" "$RAMDISK/lib/modules" "$kf_backup/prepare-inventory" || _kf_fail "Unexpected early module inventory after AnyKernel unpack."
  while read -r digest name extra; do
    [ ! -L "$RAMDISK/lib/modules/$name" ] || _kf_fail "Symlink in vendor ramdisk module target."
    cp -f "$kf_early_payload/$name" "$RAMDISK/lib/modules/$name" && chown 0:0 "$RAMDISK/lib/modules/$name" && chmod 0644 "$RAMDISK/lib/modules/$name" || _kf_fail "Early module copy failed."
  done < "$kf_early_manifest"
  _kf_verify "$RAMDISK/lib/modules" "$kf_early_manifest" || _kf_fail "Early module verification failed."
  kf_prepared=1
}

kernelforge_finish_install() {
  [ "$kf_prepared" = 1 ] || _kf_fail "Vendor boot preparation was not performed."
  [ -n "$kf_new_boot_digest" ] && [ "$(_kf_image_digest "$kf_boot" "$kf_new_boot_size")" = "$kf_new_boot_digest" ] || _kf_fail "Final boot partition readback failed."
  [ -s "$AKHOME/boot-new.img" ] || _kf_fail "AnyKernel repacked vendor_boot image is missing."
  kf_new_vendor_size=$(wc -c < "$AKHOME/boot-new.img")
  kf_new_vendor_digest=$(sha256sum "$AKHOME/boot-new.img" | cut -d' ' -f1)
  [ "$(_kf_image_digest "$kf_vendor_boot" "$kf_new_vendor_size")" = "$kf_new_vendor_digest" ] || _kf_fail "Vendor boot partition readback differs from repacked image."
  _kf_verify /vendor/lib/modules "$kf_vendor_manifest" || _kf_fail "Installed vendor module readback failed."
  _kf_same_modules "$kf_vendor_payload" /vendor/lib/modules "$kf_backup/final-vendor-inventory" || _kf_fail "Installed vendor module inventory changed."
  # vendor modules.load is owned by the ROM and must remain byte-for-byte unchanged.
  if [ -e "$kf_backup/modules/modules.load" ]; then
    cmp -s "$kf_backup/modules/modules.load" /vendor/lib/modules/modules.load || _kf_fail "ROM vendor modules.load changed."
  else
    [ ! -e /vendor/lib/modules/modules.load ] || _kf_fail "Unexpected vendor modules.load was created."
  fi
  dd if="$kf_vendor_boot" of="$kf_backup/vendor_boot-readback.img" bs=1048576 || _kf_fail "Vendor boot readback failed."
  _kf_unpack_vendor "$kf_backup/vendor_boot-readback.img" "$kf_backup/vendor-after" || _kf_fail "Installed vendor_boot unpack failed."
  cmp -s "$kf_backup/vendor-before/dtb" "$kf_backup/vendor-after/dtb" || _kf_fail "Vendor boot DTB was changed."
  _kf_verify "$kf_backup/vendor-after/extracted/lib/modules" "$kf_early_manifest" || _kf_fail "Flashed early module contents or load order mismatch."
  _kf_same_modules "$kf_early_payload" "$kf_backup/vendor-after/extracted/lib/modules" "$kf_backup/final-early-inventory" || _kf_fail "Flashed early module inventory changed."
  sync || _kf_fail "Final sync failed."
  _kf_remount_ro || _kf_fail "Cannot restore vendor read-only mount."
  kf_writes=0
  trap - EXIT HUP INT TERM
  ui_print "KernelForge: matched kernel and modules installed; local backups: $kf_backup"
}
