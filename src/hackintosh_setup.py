#!/usr/bin/env python3
"""
Hackintosh EFI/installer builder - main entry point, one-click by default.

Runs on Windows or Linux (see opcore_simplify.py for why macOS can't run
the EFI-build stage - checked immediately on startup, before the root/
Administrator prompt or any hardware questions, so a macOS run fails fast
with a clear explanation instead of wasting your time first). If you
already have a real ACPI dump from this same physical machine's Windows/
Linux side, pass it with --acpi-dir <path> to run the rest from macOS
anyway - see opcore_simplify.py's module docstring. Walks through, with no
keypresses needed for the ordinary case:
  1. Build a hardware report for this machine (hardware_report.py) and
     drive OpCore-Simplify's own menu end to end, answering its prompts
     with its own recommended defaults - compatibility checking, ACPI
     patches, kext selection, and SMBIOS are all OpCore-Simplify's own
     tested logic, just no longer typed in by hand (opcore_simplify.py).
     Two kinds of question still stop for a real answer: which GPU/WiFi/
     Bluetooth device to use on hardware with more than one, and whether to
     accept OpenCore Legacy Patcher's SIP/AMFI tradeoff - see
     opcore_simplify.py's module docstring for why those aren't defaulted.
  2. Fetch the Recovery/BaseSystem image for whichever macOS version
     OpCore-Simplify picked, straight from Apple (macrecovery.py).
  3. Auto-detect a USB/SD card already plugged in (or wait for one if none
     is), partition it (EFI System Partition + a partition for the
     BaseSystem image), and write the image on. If exactly one USB/SD
     device is present, wiping it is auto-confirmed after a 5-second
     countdown (Ctrl+C to abort) instead of typing a confirmation phrase;
     with zero or multiple candidates it still asks, since there's no safe
     default for "which disk" (partition.py, write_basesystem.py).
  4. Copy OpCore-Simplify's EFI onto the EFI partition, patch it for
     iMessage (real ROM MAC + built-in DeviceProperty), and run USB port
     mapping (immediate on Linux; hands off to USBToolBox on Windows/macOS).

Read README.md first - in particular what still needs you at the keyboard
afterward (USB mapping precision on Windows/macOS, CPUFriend's post-boot
step, the BIOS settings you still have to set by hand).
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
    print(' Hackintosh EFI / installer builder - one-click')
    print(' Builds an EFI with OpCore-Simplify; downloads macOS straight from Apple.')
    print(' This WILL erase a disk. If exactly one USB/SD device is connected,')
    print(' wiping it is auto-confirmed after a countdown (Ctrl+C to abort) -')
    print(' see README.md. With zero or multiple candidates it still asks,')
    print(' since there is no safe default for "which disk".')
    print('=' * 70)
    print()


def macos_version_from_darwin(darwin_version):
    """
    opcore_simplify.run() now returns the darwin version string OpCore-
    Simplify itself actually selected (e.g. "23.99.99") - captured directly
    from its own select_macos_version(), not re-derived here - so
    macrecovery.py can fetch the matching Recovery image without asking a
    second time. Returns None if it can't be matched (shouldn't happen
    since OpCore-Simplify's own version list and macrecovery.MACOS_VERSIONS
    both key off the same Darwin major number).
    """
    if not darwin_version:
        return None
    major = int(str(darwin_version).split('.')[0])
    return next((v for v in macrecovery.MACOS_VERSIONS if v['darwin'] == major), None)


def choose_macos_version_fetched():
    """Fallback only - used if opcore_simplify.run() somehow didn't capture
    a version (e.g. the whole build had to fall back to manual prompts in a
    way that skipped select_macos_version's wrapper). Asks directly."""
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
    Only ever offers USB/SD media, never an internal drive (bus/transport-
    based, not the OS's own "internal" flag - see partition.list_disks()'s
    docstring for why that flag alone isn't trustworthy on Hackintosh
    hardware).

    Returns (disk_id, label, size_gib, auto_confirmed). auto_confirmed is
    True only when exactly one USB/SD device was found already connected -
    the one case main() lets confirm_and_wipe() skip typing the
    confirmation back (see its docstring). Zero or multiple candidates
    always fall back to picking/confirming by hand, since there's no safe
    default for "which disk".
    """
    already = partition.list_removable_disks()
    if len(already) == 1:
        d = already[0]
        print()
        print(f'Auto-detected the only connected USB/SD device: '
              f'{d["id"]}  -  {d["size_gib"]} GiB  -  {d["label"]}  ({d["bus"]})')
        return d['id'], d['label'], d['size_gib'], True

    if len(already) > 1:
        print()
        print('Multiple USB/SD devices are already connected - pick the right one:')
        disk_id, label, size_gib = _pick_from(already)
        return disk_id, label, size_gib, False

    print()
    print('No USB/SD media detected.')
    input('Plug in the USB drive or SD card to use as the installer target, then press Enter...')
    after = partition.list_removable_disks()

    if len(after) == 1:
        d = after[0]
        print(f'Detected: {d["id"]}  -  {d["size_gib"]} GiB  -  {d["label"]}  ({d["bus"]})')
        return d['id'], d['label'], d['size_gib'], True

    if len(after) > 1:
        print('Detected more than one device - pick the right one:')
        disk_id, label, size_gib = _pick_from(after)
        return disk_id, label, size_gib, False

    print('No USB/SD media detected at all. Falling back to the full disk list -')
    print('BE CAREFUL: this includes internal drives. Only proceed if you know exactly')
    print('which one is your removable media.')
    all_disks = partition.list_disks()
    if not all_disks:
        raise SystemExit('No disks detected at all.')
    disk_id, label, size_gib = _pick_from(all_disks)
    return disk_id, label, size_gib, False


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

    # Checked before *anything* else, including the banner and the root/
    # Administrator check right after it - this can't be worked around by
    # elevating, and there's no reason to ask for a sudo password (or make
    # someone type in hardware details) on a host that's about to fail
    # right after anyway. See opcore_simplify.py's own docstring for why
    # this specific check can't just be deferred to later.
    acpi_dir_override = opcore_simplify.parse_acpi_dir_arg(sys.argv[1:])
    opcore_simplify.check_host_supports_efi_build(acpi_dir_override)

    banner()
    # Checked early on purpose: create_partitions() also enforces this, but
    # not until after the entire OpCore-Simplify session, disk selection,
    # and download - a non-root run would otherwise burn all of that (often
    # many minutes) before failing at the very last, most destructive step.
    partition.require_admin()
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
    built_efi_dir, darwin_version = opcore_simplify.run(
        workdir=os.path.join(WORKDIR, 'opcore_simplify'),
        prompt_for_motherboard=True,
        acpi_dir_override=acpi_dir_override,
    )
    if not built_efi_dir:
        raise SystemExit('No EFI was built (looks like it got stuck on an unrecognized prompt '
                          'and was quit before step 6) - nothing left to do.')

    version = macos_version_from_darwin(darwin_version) or choose_macos_version_fetched()

    disk_id, label, size_gib, auto_confirmed = choose_disk()
    partition.confirm_and_wipe(disk_id, label, size_gib=size_gib, auto=auto_confirmed)

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
        print('== USB port mapping ==')
        if osname == 'linux':
            # One-shot sysfs snapshot, no interaction needed - safe to just run.
            print('Reading this machine\'s real USB topology from sysfs (one-shot snapshot)...')
            usb_map.apply(efi_dest_root, os.path.join(WORKDIR, 'usb'), smbios_model=smbios_model)
        else:
            # USBToolBox itself needs a human to plug something into each physical
            # port and click Build - there's no one-click path for that, so this
            # step is skipped here rather than blocking on an external GUI tool.
            print('Skipped: USB port mapping on Windows/macOS needs USBToolBox\'s own GUI')
            print('(you plug something into each physical port and click Build there) -')
            print(f'run it yourself later: python3 usb_map.py {efi_dest_root}')

        # Just a note for the end-of-run summary - real data can only be generated
        # after first boot into macOS (cpufriend.py), so there's nothing to decide now.
        enable_cpufriend = laptop
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
