#!/usr/bin/env python3
"""
Hackintosh EFI/installer builder - main entry point.

Runs on Windows, Linux or macOS. Walks through:
  1. Build a hardware report for this machine (hardware_report.py) and hand
     off to OpCore-Simplify's own interactive tool to build the EFI folder -
     compatibility checking, ACPI patches, kext selection, and SMBIOS are
     all OpCore-Simplify's own tested logic (opcore_simplify.py).
  2. Fetch the Recovery/BaseSystem image for whichever macOS version you
     picked in OpCore-Simplify, straight from Apple (macrecovery.py).
  3. Auto-detect a USB/SD card, partition it (EFI System Partition + a
     partition for the BaseSystem image), and write the image on
     (partition.py, write_basesystem.py).
  4. Copy OpCore-Simplify's EFI onto the EFI partition, patch it for
     iMessage (real ROM MAC + built-in DeviceProperty), and optionally run
     USB port mapping / stage CPUFriend for a follow-up step.

Read README.md first - in particular what still needs you at the keyboard
afterward (USB mapping precision, CPUFriend's post-boot step, the BIOS
settings you still have to set by hand).
"""

import datetime
import os
import sys

import hw_detect
import macrecovery
import partition
import write_basesystem
import opcore_simplify
import imessage
import usb_map

WORKDIR = os.path.join(os.getcwd(), 'hackintosh_build')


class _Tee:
    """Mirrors everything printed to the terminal into a log file too, so a
    failed run can be diagnosed after the fact instead of only by scrollback."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log_file = log_file

    def write(self, data):
        self._stream.write(data)
        self._log_file.write(data)

    def flush(self):
        self._stream.flush()
        self._log_file.flush()


def start_logging():
    os.makedirs(WORKDIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log_path = os.path.join(WORKDIR, f'session_{timestamp}.log')
    log_file = open(log_path, 'w')
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    return log_path


def banner():
    print('=' * 70)
    print(' Hackintosh EFI / installer builder')
    print(' Builds an EFI with OpCore-Simplify; downloads macOS straight from Apple.')
    print(' This WILL erase a disk you choose. Nothing happens without you')
    print(' typing an explicit confirmation for the exact disk selected.')
    print('=' * 70)
    print()
    typed = input('Type "I UNDERSTAND" to continue: ')
    if typed.strip() != 'I UNDERSTAND':
        raise SystemExit('Aborted.')


def choose_macos_version_fetched():
    """
    OpCore-Simplify picks/confirms the macOS version itself, inside its own
    menu, using its own compatibility check against the hardware report -
    there's no need (or way) for this script to duplicate that decision.
    This just asks which one you picked, so macrecovery.py can fetch the
    matching Recovery image - same Darwin-major-number format OpCore-
    Simplify's own menu already showed you.
    """
    print()
    print('Which macOS version did you select in OpCore-Simplify?')
    for v in macrecovery.MACOS_VERSIONS:
        print(f'  {v["darwin"]}. {v["name"]} ({v["version"]})')
    while True:
        choice = input('Darwin version number: ').strip()
        match = next((v for v in macrecovery.MACOS_VERSIONS if str(v['darwin']) == choice), None)
        if match:
            return match
        print('Not one of the listed Darwin version numbers, try again.')


def _pick_from(disks):
    print()
    for d in disks:
        print(f'  {d["id"]}  -  {d["size_gib"]} GiB  -  {d["label"]}  ({d["bus"]})')
    print()
    ids = {d['id'] for d in disks}
    while True:
        chosen = input('Type the identifier of the disk to use as the installer target: ').strip()
        if chosen in ids:
            disk = next(d for d in disks if d['id'] == chosen)
            return disk['id'], disk['label'], disk['size_gib']
        print('Not a listed disk identifier, try again.')


def choose_disk():
    """
    Auto-detects the target by diffing the removable-disk list before/after
    asking you to plug it in - only ever offers USB/SD media, never an
    internal drive (bus/transport-based, not the OS's own "internal" flag -
    see partition.list_disks()'s docstring for why that flag alone isn't
    trustworthy on Hackintosh hardware). This only speeds up *finding* the
    right disk; confirm_and_wipe()'s typed confirmation still gates the
    actual erase regardless of how the disk was picked.
    """
    before = {d['id']: d for d in partition.list_removable_disks()}
    if before:
        print()
        print('Already-connected USB/SD media:')
        for d in before.values():
            print(f'  {d["id"]}  -  {d["size_gib"]} GiB  -  {d["label"]}  ({d["bus"]})')

    print()
    input('Plug in the USB drive or SD card to use as the installer target, then press Enter...')
    after = {d['id']: d for d in partition.list_removable_disks()}
    new_ids = set(after) - set(before)

    if len(new_ids) == 1:
        d = after[next(iter(new_ids))]
        print(f'Detected: {d["id"]}  -  {d["size_gib"]} GiB  -  {d["label"]}  ({d["bus"]})')
        if input('Use this disk? [Y/n]: ').strip().lower() != 'n':
            return d['id'], d['label'], d['size_gib']

    if len(new_ids) > 1:
        print('Detected more than one new device at once - pick the right one:')
        return _pick_from([after[i] for i in new_ids])

    if after:
        print('No newly-connected device detected - choose from currently visible USB/SD media:')
        return _pick_from(list(after.values()))

    print('No USB/SD media detected at all. Falling back to the full disk list -')
    print('BE CAREFUL: this includes internal drives. Only proceed if you know exactly')
    print('which one is your removable media.')
    all_disks = partition.list_disks()
    if not all_disks:
        raise SystemExit('No disks detected at all.')
    return _pick_from(all_disks)


def _read_smbios_model(efi_dest):
    import plistlib
    config_path = os.path.join(efi_dest, 'OC', 'config.plist')
    try:
        with open(config_path, 'rb') as f:
            config = plistlib.load(f)
        return config.get('PlatformInfo', {}).get('Generic', {}).get('SystemProductName')
    except (OSError, ValueError):
        return None


def main():
    log_path = start_logging()
    print(f'Logging this session to {log_path}')
    banner()
    osname = hw_detect.host_os()
    print(f'Host OS: {osname}')

    cpu = hw_detect.get_cpu_info()
    print(f'CPU: {cpu["brand"]}')
    laptop = hw_detect.is_laptop()

    os.makedirs(WORKDIR, exist_ok=True)

    print()
    print('== Building OpenCore EFI with OpCore-Simplify ==')
    # Runs before partitioning on purpose - OpCore-Simplify builds into its
    # own tool directory regardless of any disk, so there's no need to wait
    # for one to exist first. copy_efi() moves the result on once we do.
    built_efi_dir = opcore_simplify.run(
        workdir=os.path.join(WORKDIR, 'opcore_simplify'),
        prompt_for_motherboard=True,
    )
    if not built_efi_dir:
        raise SystemExit('No EFI was built (looks like you quit before step 6) - nothing left to do.')

    version = choose_macos_version_fetched()

    disk_id, label, size_gib = choose_disk()
    partition.confirm_and_wipe(disk_id, label, size_gib=size_gib)

    print()
    print('== Partitioning ==')
    efi_part, target_part = partition.create_partitions(disk_id)

    print()
    print('== Downloading macOS from Apple ==')
    dmg_path = macrecovery.download_recovery(version, os.path.join(WORKDIR, 'recovery'))

    print()
    print('== Writing BaseSystem to target partition ==')
    write_basesystem.write(dmg_path, target_part)

    print()
    print('== Copying EFI onto the EFI partition ==')
    efi_mount = partition.mount_efi(efi_part)
    try:
        efi_dest_root = opcore_simplify.copy_efi(built_efi_dir, efi_mount)
        print(f'Copied {built_efi_dir} -> {efi_dest_root}')

        smbios_model = _read_smbios_model(efi_dest_root) or 'iMac19,1'

        imessage.apply_to_efi(efi_dest_root)

        print()
        ans = input('Generate a USB port map now? [Y/n]: ').strip().lower()
        if ans != 'n':
            print('== USB port mapping ==')
            if osname == 'linux':
                print('Reading this machine\'s real USB topology from sysfs (one-shot snapshot)...')
            else:
                print('Handing off to USBToolBox - you\'ll plug a device into each physical port.')
            usb_map.apply(efi_dest_root, os.path.join(WORKDIR, 'usb'), smbios_model=smbios_model)

        enable_cpufriend = False
        if laptop:
            print()
            print('CPUFriend fixes CPU power-management data mismatches from a spoofed SMBIOS -')
            print('mainly a battery-life/thermal issue. Its own docs say: "most likely NOT required')
            print('when not sure whether to use it." The data half (CPUFriendDataProvider.kext) can')
            print('only be generated AFTER you successfully boot this drive into macOS (cpufriend.py),')
            print('and needs CPUFriend.kext already present - OpCore-Simplify may or may not have')
            print('included it depending on what you selected in its kext customization step.')
            ans = input('Remind you to run cpufriend.py after first boot? [y/N]: ').strip().lower()
            enable_cpufriend = ans == 'y'
    finally:
        partition.unmount(efi_mount)

    print()
    print('=' * 70)
    print('Done. Before you boot this on the target machine:')
    print('  - BIOS: disable Secure Boot, disable CSM/enable UEFI-only boot,')
    print('    disable VT-d/Intel VMD if present, set boot mode to UEFI.')
    print('  - Boot from this drive, choose the OpenCore boot entry, then')
    print('    the "macOS Base System" entry to reach Recovery.')
    print('  - Use Disk Utility in Recovery to erase your real target disk')
    print('    as APFS, then reinstall macOS onto it from Recovery.')
    print('  - If you skipped USB mapping above, re-run usb_map.py directly')
    print('    against the EFI partition once you can boot to Recovery.')
    if enable_cpufriend:
        print('  - Once macOS is fully installed and booted (not just Recovery),')
        print('    run cpufriend.py against the EFI partition to generate')
        print('    CPUFriendDataProvider.kext (make sure CPUFriend.kext is present first).')
    print('=' * 70)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('\nAborted.')
