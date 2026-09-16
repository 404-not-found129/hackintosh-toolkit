#!/usr/bin/env python3
"""
Copies the OpenCore EFI onto the actual internal target disk's own EFI
System Partition, once macOS is installed there - the step this toolkit's
own README used to skip over ("use Disk Utility to erase your actual
target disk... then reinstall macOS"), leaving the Hackintosh only able to
boot with the USB installer left permanently plugged in. Without this, the
USB's EFI partition is the only place OpenCore exists at all.

Run this from the *installed* macOS itself (not Recovery - Recovery's own
limited environment doesn't reliably expose diskutil/bless the same way,
and this only matters once you're past first boot anyway), with the USB
installer still plugged in as the source. macOS only: finding "the disk
this system actually booted from" and registering a boot entry both use
diskutil/bless, which are macOS-specific - there's no equivalent on the
Windows/Linux side of this toolkit, and no reason to run this there since
macOS isn't installed yet at that point.

What it does:
  1. Resolves the disk this macOS actually booted from down to its real
     physical whole disk - not the synthesized APFS container disk
     `diskutil info -plist /` reports as `ParentWholeDisk` (verified live:
     on a real APFS boot, that's an `Apple_APFS_Container` like "disk1",
     not the GPT disk with the actual EFI partition on it - the real one is
     found via that container's own `APFSPhysicalStores`).
  2. Finds that physical disk's `Content: "EFI"` partition (every GPT/GUID
     disk macOS's Disk Utility creates gets one automatically, whether or
     not Finder ever shows it) and mounts it.
  3. Finds the source EFI to copy - either an explicit path you pass, or
     auto-detected: first an already-mounted volume with `EFI/OC/
     config.plist` on it, then any connected USB/SD device's own EFI
     partition (the installer you just booted from, most likely still
     plugged in and not yet mounted, since macOS doesn't auto-mount EFI
     partitions in Finder).
  4. Copies it onto the internal disk's EFI partition - same copy_efi()
     this toolkit already uses to put OpenCore-Simplify's build onto the
     installer USB in the first place (opcore_simplify.py), so it's the
     exact same known-working copy, not a second implementation.
  5. Best-effort registers it as the default boot entry via `bless`. This
     is genuinely inconsistent across Hackintosh motherboards - some PC
     UEFI firmware honors the NVRAM boot entry `bless` writes, some
     doesn't and needs the boot order changed in the BIOS/UEFI setup menu
     instead (same "disable Secure Boot/CSM" screen this toolkit's README
     already sends you to). Either way this step is non-destructive: it
     only adds/changes a boot preference, never erases anything, so a
     failure here just means falling back to the BIOS's own boot menu.
"""

import os
import re
import subprocess
import sys

import hw_detect
import opcore_simplify
import partition


def _diskutil_plist(args):
    """Runs diskutil with the given args (which must already include
    '-plist' wherever diskutil expects it) and parses the output."""
    import plistlib
    out = subprocess.run(['diskutil'] + args, capture_output=True, text=False).stdout
    return plistlib.loads(out)


def _diskutil_info(device_id):
    return _diskutil_plist(['info', '-plist', device_id])


def physical_whole_disk_for(device_id):
    """Resolves any disk/partition/container identifier down to the real
    physical whole-disk identifier it ultimately lives on. Handles the
    APFS-container indirection every modern macOS boot volume has (see
    module docstring) - a container reports VirtualOrPhysical: "Virtual"
    and its own APFSPhysicalStores points at the real partition backing
    it; anything else's ParentWholeDisk is already physical."""
    info = _diskutil_info(device_id)
    if info.get('VirtualOrPhysical') == 'Virtual':
        stores = info.get('APFSPhysicalStores') or []
        if stores:
            # e.g. "disk0s2" - strip the partition suffix to get "disk0"
            return re.sub(r's\d+$', '', stores[0]['APFSPhysicalStore'])
    return info.get('ParentWholeDisk', device_id)


def find_efi_partition(whole_disk_id):
    """Returns the DeviceIdentifier of whole_disk_id's Content: "EFI"
    partition, or None if it doesn't have one (shouldn't happen on a GPT
    disk macOS itself partitioned, which is every APFS boot disk)."""
    data = _diskutil_plist(['list', '-plist', whole_disk_id])
    for disk in data.get('AllDisksAndPartitions', []):
        if disk.get('DeviceIdentifier') != whole_disk_id:
            continue
        for part in disk.get('Partitions', []):
            if part.get('Content') == 'EFI':
                return part['DeviceIdentifier']
    return None


def find_boot_disk_efi_partition():
    """Returns the EFI partition's DeviceIdentifier for the disk this
    macOS actually booted from, or None if none was found."""
    root_info = _diskutil_info('/')
    container_or_disk = root_info.get('ParentWholeDisk')
    if not container_or_disk:
        return None
    physical = physical_whole_disk_for(container_or_disk)
    return find_efi_partition(physical)


def _looks_like_efi_root(path):
    return os.path.isfile(os.path.join(path, 'OC', 'config.plist'))


def find_source_efi():
    """Auto-detects the EFI folder to copy from: an already-mounted volume
    with EFI/OC/config.plist first, then any connected USB/SD device's own
    EFI partition (mounting it if needed). Returns the EFI folder path
    (the one directly containing OC/, BOOT/ etc., matching what
    opcore_simplify.copy_efi() expects as its source), or None."""
    volumes_root = '/Volumes'
    if os.path.isdir(volumes_root):
        for name in sorted(os.listdir(volumes_root)):
            candidate = os.path.join(volumes_root, name, 'EFI')
            if _looks_like_efi_root(candidate):
                return candidate

    for disk in partition.list_removable_disks():
        efi_part = find_efi_partition(disk['id'])
        if not efi_part:
            continue
        try:
            mount_point = partition.mount_efi(efi_part)
        except Exception:
            continue
        candidate = os.path.join(mount_point, 'EFI')
        if _looks_like_efi_root(candidate):
            return candidate

    return None


def register_boot_entry(target_mount):
    """Best-effort: writes an NVRAM boot entry pointing at this EFI via
    bless, so it's the firmware's default without needing a BIOS boot-order
    change. See module docstring for why this isn't guaranteed to stick on
    every Hackintosh board, and isn't treated as a failure when it doesn't."""
    bootloader = os.path.join(target_mount, 'EFI', 'BOOT', 'BOOTx64.efi')
    if not os.path.isfile(bootloader):
        print(f'No {bootloader} found - skipping boot-entry registration.')
        return False
    result = subprocess.run(
        ['bless', '--mount', target_mount, '--setBoot', '--file', bootloader],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print('Registered as the default NVRAM boot entry via bless.')
        return True
    print('bless could not register a default boot entry (this is common on some')
    print('Hackintosh motherboards - not every PC UEFI firmware honors it):')
    print(f'  {result.stderr.strip()}')
    print('Set this disk as the default boot device in the BIOS/UEFI setup menu instead.')
    return False


def install(source_efi_root=None, do_bless=True):
    if hw_detect.host_os() != 'macos':
        raise RuntimeError('This only makes sense run from the installed macOS itself.')
    partition.require_admin()

    if source_efi_root is None:
        print('Looking for the EFI to copy...')
        source_efi_root = find_source_efi()
        if not source_efi_root:
            raise SystemExit(
                'Could not auto-detect an EFI to copy (looked for a mounted volume or '
                'USB/SD device with EFI/OC/config.plist on it). Pass its path explicitly: '
                f'{sys.argv[0]} <path to the EFI folder, e.g. /Volumes/EFI/EFI>'
            )
    elif not _looks_like_efi_root(source_efi_root):
        raise SystemExit(f'{source_efi_root} does not look like a built EFI '
                          f'(no OC/config.plist under it).')
    print(f'Copying from: {source_efi_root}')

    efi_part = find_boot_disk_efi_partition()
    if not efi_part:
        raise SystemExit('Could not find this machine\'s internal EFI System Partition.')
    print(f'Internal boot disk\'s EFI partition: {efi_part}')

    target_mount = partition.mount_efi(efi_part)
    try:
        dest = opcore_simplify.copy_efi(source_efi_root, target_mount)
        print(f'Copied {source_efi_root} -> {dest}')

        if do_bless:
            register_boot_entry(target_mount)
    finally:
        partition.unmount(target_mount)

    print()
    print('Done. This machine can now boot without the USB installer plugged in -')
    print('try rebooting with it removed. If it boots straight to the wrong OS/boot')
    print('picker instead of OpenCore, set this disk\'s EFI as the default boot device')
    print('in the BIOS/UEFI setup menu (same screen you disabled Secure Boot/CSM on).')


if __name__ == '__main__':
    _source = sys.argv[1] if len(sys.argv) > 1 else None
    install(_source)
