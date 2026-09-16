#!/usr/bin/env python3
"""
Disk partitioning: creates a GPT disk with an EFI System Partition (for
OpenCore) and a second partition (for the macOS BaseSystem image).

THIS IS THE ONE DESTRUCTIVE STEP IN THE WHOLE TOOLKIT. Every function that
touches a real disk requires the caller to have already gone through
confirm_and_wipe(). By default that forces the operator to type back the
exact disk identifier and the literal word ERASE. confirm_and_wipe(auto=True)
skips the typing (for the one-click flow's "exactly one USB/SD device was
auto-detected" case - see hackintosh_setup.py's choose_disk()) but still
prints the disk identifier/size/label and holds for a few seconds so a
Ctrl+C is still possible before anything is touched - auto-confirm is not
the same as silent. There is still no "auto-pick the biggest disk"/"auto-
pick an internal drive" anywhere in here - auto-confirm only ever applies
to a disk that was independently identified as the sole connected USB/SD
device, never a disk picked out of a list of several or an internal drive.
"""

import json
import os
import subprocess
import sys
import time

import hw_detect

EFI_SIZE_MIB = 300


def _run(cmd, check=True, input_text=None, quiet=False):
    if not quiet:
        print('  $', ' '.join(cmd) if isinstance(cmd, list) else cmd)
    result = subprocess.run(cmd, input=input_text, text=True, capture_output=True,
                             shell=isinstance(cmd, str))
    if result.stdout and not quiet:
        print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        if check:
            raise RuntimeError(f'Command failed: {cmd}')
    return result


def require_admin():
    osname = hw_detect.host_os()
    if osname in ('macos', 'linux'):
        if os.geteuid() != 0:
            raise SystemExit('Partitioning needs root. Re-run this tool with sudo.')
    elif osname == 'windows':
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise SystemExit('Partitioning needs an elevated (Administrator) PowerShell/terminal.')


MACOS_REMOVABLE_BUSES = {'usb', 'secure digital'}
LINUX_REMOVABLE_TRANSPORTS = {'usb', 'mmc', 'sdio'}
WINDOWS_REMOVABLE_BUS_TYPES = {'usb', 'sd'}


def list_disks():
    """
    Returns [{'id': str, 'size_gib': float, 'label': str, 'removable': bool,
              'bus': str}] - id is what you pass back into every other
    function here.

    'removable' is derived from the actual bus/transport type, NOT the OS's
    own "internal" flag - verified live: on this real Hackintosh, internal
    NVMe drives report Internal=False (a well-known Hackintosh quirk, since
    there's no genuine Apple ACPI/PCI "internal" marker on non-Mac boards),
    while their BusProtocol correctly still reads "PCI-Express". Bus/
    transport type is the one field that stayed truthful here.
    """
    osname = hw_detect.host_os()
    disks = []

    if osname == 'macos':
        out = _run(['diskutil', 'list', '-plist'], check=False, quiet=True).stdout
        import plistlib
        data = plistlib.loads(out.encode())
        for disk_id in data.get('WholeDisks', []):
            info = _run(['diskutil', 'info', '-plist', disk_id], check=False, quiet=True).stdout
            info_data = plistlib.loads(info.encode())
            size = info_data.get('TotalSize', 0) / (1024 ** 3)
            name = info_data.get('MediaName', disk_id)
            bus = info_data.get('BusProtocol', 'Unknown')
            disks.append({
                'id': disk_id,
                'size_gib': round(size, 1),
                'label': name,
                'bus': bus,
                'removable': bus.strip().lower() in MACOS_REMOVABLE_BUSES,
            })

    elif osname == 'linux':
        out = _run(['lsblk', '-d', '-b', '-J', '-o', 'NAME,SIZE,MODEL,TYPE,RM,TRAN'], check=False, quiet=True).stdout
        data = json.loads(out) if out.strip() else {'blockdevices': []}
        for dev in data.get('blockdevices', []):
            if dev.get('type') == 'disk':
                tran = (dev.get('tran') or '').strip().lower()
                rm = str(dev.get('rm', '')).strip().lower() in ('1', 'true')
                disks.append({
                    'id': '/dev/' + dev['name'],
                    'size_gib': round(int(dev.get('size', 0)) / (1024 ** 3), 1),
                    'label': dev.get('model') or dev['name'],
                    'bus': tran or 'unknown',
                    'removable': rm or tran in LINUX_REMOVABLE_TRANSPORTS,
                })

    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     'Get-Disk | ConvertTo-Json'], check=False, quiet=True).stdout
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            bus = str(d.get('BusType', 'Unknown'))
            disks.append({
                'id': str(d.get('Number')),
                'size_gib': round(d.get('Size', 0) / (1024 ** 3), 1),
                'label': d.get('FriendlyName', 'Disk'),
                'bus': bus,
                'removable': bus.strip().lower() in WINDOWS_REMOVABLE_BUS_TYPES,
            })

    return disks


def list_removable_disks():
    """Same as list_disks(), filtered to USB/SD media only - the safe
    default for picking an installer target without risking an internal
    drive. Excludes mounted disk images too (bus reads "Disk Image" on
    macOS, matched by neither allowlist)."""
    return [d for d in list_disks() if d['removable']]


MIN_RECOMMENDED_GIB = 14  # BaseSystem + EFI + working room; below this, warn but don't block


def confirm_and_wipe(disk_id, label, size_gib=None, auto=False):
    """
    auto=True skips typing the confirmation back (see module docstring for
    exactly when this toolkit sets that - only a disk independently proven
    to be the sole connected USB/SD device). It still prints the same loud
    warning and holds for a few seconds so Ctrl+C remains a real option
    right up until the erase actually starts.
    """
    print()
    print(f'!! ABOUT TO ERASE: {disk_id}  ({label})')
    print('!! Every partition and every file on this disk will be permanently destroyed.')
    if size_gib is not None and size_gib < MIN_RECOMMENDED_GIB:
        print(f'!! This disk is only {size_gib} GiB - {MIN_RECOMMENDED_GIB}+ GiB is recommended for the')
        print('!! BaseSystem image plus working room. It may not fit.')
    print()

    if auto:
        print(f'Auto-confirming (this was the only USB/SD device connected). '
              f'Erasing {disk_id} in 5 seconds - Ctrl+C now to abort.')
        for remaining in (5, 4, 3, 2, 1):
            print(f'  {remaining}...')
            time.sleep(1)
        return

    typed = input(f'Type the disk identifier exactly ("{disk_id}") to continue: ')
    if typed.strip() != disk_id:
        raise SystemExit('Disk identifier did not match - aborting, nothing was touched.')
    typed = input('Type ERASE in capitals to confirm you want to wipe it: ')
    if typed.strip() != 'ERASE':
        raise SystemExit('Confirmation did not match - aborting, nothing was touched.')


def create_partitions(disk_id):
    """
    Wipes disk_id and lays down:
      partition 1: EFI System Partition, FAT32, EFI_SIZE_MIB
      partition 2: rest of the disk, for the macOS BaseSystem image
    Returns (efi_partition_id, target_partition_id) - OS-native identifiers
    you can pass into write_basesystem.py / opcore_simplify.py.
    """
    require_admin()
    osname = hw_detect.host_os()

    if osname == 'macos':
        _run(['diskutil', 'unmountDisk', disk_id])
        _run(['diskutil', 'partitionDisk', disk_id, 'GPT',
              'FAT32', 'EFI', f'{EFI_SIZE_MIB}M',
              'JHFS+', 'TARGET', 'R'])
        return f'{disk_id}s1', f'{disk_id}s2'

    if osname == 'linux':
        _run(['sgdisk', '--zap-all', disk_id])
        _run(['sgdisk',
              '-n', f'1:0:+{EFI_SIZE_MIB}M', '-t', '1:ef00', '-c', '1:EFI',
              '-n', '2:0:0', '-t', '2:af00', '-c', '2:TARGET',
              disk_id])
        _run(['partprobe', disk_id], check=False)
        part_prefix = disk_id + ('p' if disk_id[-1].isdigit() else '')
        efi_part = f'{part_prefix}1'
        target_part = f'{part_prefix}2'
        _run(['mkfs.vfat', '-F', '32', '-n', 'EFI', efi_part])
        return efi_part, target_part

    if osname == 'windows':
        script = (
            f'select disk {disk_id}\n'
            'clean\n'
            'convert gpt\n'
            f'create partition efi size={EFI_SIZE_MIB}\n'
            'format quick fs=fat32 label="EFI"\n'
            'assign letter=S\n'
            'create partition primary\n'
            'format quick fs=fat32 label="TARGET"\n'
            'assign letter=T\n'
        )
        script_path = os.path.join(os.environ.get('TEMP', '.'), 'hackintosh_diskpart.txt')
        with open(script_path, 'w') as f:
            f.write(script)
        _run(['diskpart', '/s', script_path])
        return 'S:', 'T:'

    raise RuntimeError(f'Unsupported OS: {osname}')


def mount_efi(efi_partition):
    """Returns a filesystem path the EFI partition is mounted at."""
    osname = hw_detect.host_os()

    if osname == 'macos':
        out = _run(['diskutil', 'mount', efi_partition]).stdout
        # "Volume EFI on /dev/diskXsY mounted at /Volumes/EFI"
        if ' at ' in out:
            return out.strip().split(' at ')[-1]
        return '/Volumes/EFI'

    if osname == 'linux':
        mount_point = '/mnt/hackintosh_efi'
        os.makedirs(mount_point, exist_ok=True)
        _run(['mount', efi_partition, mount_point])
        return mount_point

    if osname == 'windows':
        # partition.py already assigned this a drive letter via diskpart
        letter = efi_partition.rstrip('\\') + '\\'
        return letter

    raise RuntimeError(f'Unsupported OS: {osname}')


def unmount(mount_point_or_partition):
    osname = hw_detect.host_os()
    if osname == 'macos':
        _run(['diskutil', 'unmount', mount_point_or_partition], check=False)
    elif osname == 'linux':
        _run(['umount', mount_point_or_partition], check=False)
    # Windows drive letters need no explicit unmount for our purposes.


if __name__ == '__main__':
    for d in list_disks():
        print(d)
