#!/usr/bin/env python3
"""
Hackintosh EFI/installer builder - main entry point.

Runs on Windows, Linux or macOS. Walks through:
  1. Detect this machine's CPU/GPU and work out which macOS versions will
     have native graphics acceleration.
  2. Let you pick a macOS version - either one your GPU natively supports,
     or explicitly force any version with no acceleration.
  3. Fetch that version's Recovery/BaseSystem image straight from Apple.
  4. Pick a target disk and, after a strict typed confirmation, partition
     it: EFI System Partition + a partition for the BaseSystem image.
  5. Write the BaseSystem image onto the target partition.
  6. Build an OpenCore EFI (OpenCorePkg + Lilu/VirtualSMC/WhateverGreen/
     AppleALC) onto the EFI partition.

Read README.md first - in particular the section on what this can't
automate (ACPI, USB mapping, real SMBIOS) and the BIOS settings you still
have to set by hand.
"""

import datetime
import os
import sys

import hw_detect
import gpu_compat
import macrecovery
import partition
import write_basesystem
import opencore_build
import acpi_patches
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
    print(' Downloads macOS straight from Apple; builds a real OpenCore EFI.')
    print(' This WILL erase a disk you choose. Nothing happens without you')
    print(' typing an explicit confirmation for the exact disk selected.')
    print('=' * 70)
    print()
    typed = input('Type "I UNDERSTAND" to continue: ')
    if typed.strip() != 'I UNDERSTAND':
        raise SystemExit('Aborted.')


def choose_macos_version(cpu, gpus):
    result = gpu_compat.evaluate(cpu, gpus)
    native = result['native']

    print()
    print('Detected GPU(s):')
    for entry in result['per_gpu']:
        gpu = entry['gpu']
        rng = entry['range']
        verdict = 'unknown / unsupported' if not rng else f'darwin {rng[0]}-{rng[1]}'
        print(f'  - {gpu["name"]} ({gpu["vendor"]})  ->  {verdict}')
    for note in result['notes']:
        print(f'  note: {note}')
    print()

    print('Choose a macOS version:')
    options = []
    for v in macrecovery.MACOS_VERSIONS:
        supported = native and native[0] <= v['darwin'] <= native[1]
        tag = '[native acceleration]' if supported else '[NO acceleration on this GPU]'
        options.append(v)
        print(f'  {len(options)}. {v["name"]} ({v["version"]}) {tag}')

    print()
    print('Whatever you pick, you can also force it through with no GPU')
    print('acceleration at all (basic display only) - useful for very new or')
    print('very old/unlisted GPUs.')
    print()

    while True:
        choice = input(f'Pick a version (1-{len(options)}): ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            version = options[int(choice) - 1]
            break
        print('Invalid choice.')

    supported = native and native[0] <= version['darwin'] <= native[1]
    no_accel = False
    if not supported:
        ans = input(f'{version["name"]} has no known native acceleration for your GPU. '
                     'Force it anyway with no acceleration? [y/N]: ').strip().lower()
        if ans != 'y':
            raise SystemExit('Pick a different version, or confirm the no-acceleration override.')
        no_accel = True
    else:
        ans = input('Force no-acceleration mode anyway (skip GPU kexts)? [y/N]: ').strip().lower()
        no_accel = ans == 'y'

    return version, no_accel


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


def main():
    log_path = start_logging()
    print(f'Logging this session to {log_path}')
    banner()
    osname = hw_detect.host_os()
    print(f'Host OS: {osname}')

    cpu = hw_detect.get_cpu_info()
    gpus = hw_detect.get_gpus()
    print(f'CPU: {cpu["brand"]}')

    version, no_accel = choose_macos_version(cpu, gpus)

    disk_id, label, size_gib = choose_disk()
    partition.confirm_and_wipe(disk_id, label, size_gib=size_gib)

    os.makedirs(WORKDIR, exist_ok=True)

    print()
    print('== Partitioning ==')
    efi_part, target_part = partition.create_partitions(disk_id)

    print()
    print('== Downloading macOS from Apple ==')
    dmg_path = macrecovery.download_recovery(version, os.path.join(WORKDIR, 'recovery'))

    print()
    print('== Writing BaseSystem to target partition ==')
    write_basesystem.write(dmg_path, target_part)

    smbios_model = input('SMBIOS model to generate an identity for [iMac19,1]: ').strip() or 'iMac19,1'

    detected_laptop = hw_detect.is_laptop()
    default_label = 'Y/n' if detected_laptop else 'y/N'
    laptop_ans = input(f'Is this a laptop? (detected: {"yes" if detected_laptop else "no"}) [{default_label}]: ').strip().lower()
    laptop = detected_laptop if not laptop_ans else laptop_ans == 'y'

    enable_cpufriend = False
    if laptop:
        print()
        print('CPUFriend fixes CPU power-management data mismatches from a spoofed SMBIOS -')
        print('mainly a battery-life/thermal issue. Its own docs say: "most likely NOT required')
        print('when not sure whether to use it." Staging it now is free either way - the actual')
        print('data (CPUFriendDataProvider.kext) can only be generated AFTER you successfully')
        print('boot this drive into macOS (via cpufriend.py), not during this pre-boot build.')
        ans = input('Stage CPUFriend.kext now for that follow-up step? [y/N]: ').strip().lower()
        enable_cpufriend = ans == 'y'

    print()
    print('== Building OpenCore EFI ==')
    efi_mount = partition.mount_efi(efi_part)
    try:
        efi_dest = opencore_build.build_efi(efi_mount, version, gpus, no_accel,
                                             os.path.join(WORKDIR, 'opencore'), smbios_model=smbios_model,
                                             enable_cpufriend=enable_cpufriend)

        print()
        ans = input('Generate ACPI SSDT patches now (dumps this machine\'s real ACPI tables)? [Y/n]: ').strip().lower()
        if ans != 'n':
            print('== ACPI SSDT patches ==')
            if osname in ('windows', 'linux'):
                acpi_patches.apply(efi_dest, os.path.join(WORKDIR, 'acpi'), laptop=laptop)
            else:
                print('Automatic dumping needs Windows or Linux - handing off to SSDTTime\'s own menu instead.')
                acpi_patches.apply(efi_dest, os.path.join(WORKDIR, 'acpi'), interactive_followup=True)

        print()
        ans = input('Generate a USB port map now? [Y/n]: ').strip().lower()
        if ans != 'n':
            print('== USB port mapping ==')
            if osname == 'linux':
                print('Reading this machine\'s real USB topology from sysfs (one-shot snapshot)...')
            else:
                print('Handing off to USBToolBox - you\'ll plug a device into each physical port.')
            usb_map.apply(efi_dest, os.path.join(WORKDIR, 'usb'), smbios_model=smbios_model)
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
    print('  - If you skipped ACPI/USB steps above, re-run acpi_patches.py /')
    print('    usb_map.py directly against the EFI partition once you can')
    print('    boot to Recovery - see README.md.')
    if enable_cpufriend:
        print('  - Once macOS is fully installed and booted (not just Recovery),')
        print('    run cpufriend.py against the EFI partition to generate')
        print('    CPUFriendDataProvider.kext - CPUFriend.kext alone does nothing.')
    print('=' * 70)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('\nAborted.')
