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
import shutil
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


def _dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def _backup_one_volume(mount_point, dest_dir, label):
    """Copies mount_point's contents into dest_dir. Returns bytes actually
    copied (0 if the volume was empty or the backup was skipped). Checks
    free space at the backup destination against what's used on the source
    volume first - same reasoning as macrecovery.py's disk-space guard on
    the Recovery download: fail before writing anything instead of partway
    through a large copy."""
    used = shutil.disk_usage(mount_point).used
    backup_root = os.path.dirname(os.path.abspath(dest_dir))
    os.makedirs(backup_root, exist_ok=True)
    free = shutil.disk_usage(backup_root).free
    if free < used * 1.05:
        print(f'  Not enough free space to back up "{label}" (~{used / 2**30:.1f} GB used on that '
              f'volume, {free / 2**30:.1f} GB free at {backup_root}) - skipping this volume\'s backup.')
        return 0
    shutil.copytree(mount_point, dest_dir, symlinks=True, ignore_dangling_symlinks=True)
    return _dir_size(dest_dir)


def backup_existing_data(disk_id, backup_dir):
    """
    Best-effort backup of every file already on disk_id's existing
    partitions into backup_dir - call this after confirm_and_wipe() but
    before create_partitions() actually erases the disk for good. Until
    now nothing preserved what was already there if the wrong disk got
    picked, or you just wanted the old contents back afterward - this is
    THE ONE DESTRUCTIVE STEP IN THE WHOLE TOOLKIT (see module docstring),
    so it's worth a real safety net, not just a confirmation prompt.

    Returns backup_dir if anything was actually copied, None if there was
    nothing worth backing up (a blank/unpartitioned disk, or every
    partition on it was empty). Never raises - not for a partition it can't
    read (an unmountable/unsupported filesystem, a permissions error
    partway through a copy - handled per-partition, see _backup_partition())
    and not for a genuinely unexpected failure discovering the disk's
    partitions at all (e.g. diskutil/lsblk/Get-Partition itself failing or
    returning something unparseable - confirmed live that an empty/invalid
    diskutil response makes plistlib itself raise, which would otherwise
    propagate straight out of this function and crash the whole install
    over a failed *backup*, exactly the outcome this function's whole
    purpose is to avoid). Either way, the eventual wipe still proceeds -
    a missed backup isn't something this toolkit can work around, only
    report honestly, and it was never a hard requirement to begin with.
    """
    osname = hw_detect.host_os()
    try:
        if osname == 'macos':
            return _backup_existing_data_macos(disk_id, backup_dir)
        if osname == 'linux':
            return _backup_existing_data_linux(disk_id, backup_dir)
        if osname == 'windows':
            return _backup_existing_data_windows(disk_id, backup_dir)
        return None
    except Exception as e:
        print(f'Could not check {disk_id} for existing data to back up ({e}) - proceeding without '
              f'one. Nothing on the real disk has been touched yet.')
        return None


def _backup_partition(mount_point, we_mounted_it, unmount_fn, label, backup_dir):
    dest = os.path.join(backup_dir, label)
    if os.path.exists(dest):
        # Two partitions on the same disk can genuinely share a label - not
        # theoretical, see _find_macos_partitions()'s own docstring for a
        # real case of two partitions both literally named "EFI" on one
        # disk - so disambiguate instead of colliding with the first
        # backup (copytree requires its destination not already exist).
        suffix = 2
        while os.path.exists(f'{dest}_{suffix}'):
            suffix += 1
        dest = f'{dest}_{suffix}'
    print(f'Checking {mount_point} ("{label}") for existing data to back up...')
    try:
        copied = _backup_one_volume(mount_point, dest, label)
        if copied:
            print(f'  Backed up {copied / 2**20:.1f} MB to {dest}')
        else:
            print('  Nothing to back up (empty, or skipped - see above).')
        return copied
    except Exception as e:
        print(f'  Backup of "{label}" failed ({e}) - continuing with the other partitions, if any; '
              f'nothing on the real disk has been touched yet.')
        return 0
    finally:
        if we_mounted_it:
            unmount_fn()


def _resolve_apfs_container_volumes(container_partition_id, all_disks_and_partitions):
    """A partition with Content "Apple_APFS" isn't a mountable filesystem
    itself - it's a physical store backing a *separate*, synthesized whole-
    disk entry (own DeviceIdentifier, Content "Apple_APFS_Container") whose
    real, mountable volumes live in THAT entry's own "APFSVolumes" list, not
    the "Partitions" list disk_id's own entry has. install_efi.py's
    physical_whole_disk_for() resolves this same indirection in the other
    direction (a volume back to its physical disk); this is volume-
    resolution the other way, done fresh here since backup_existing_data()
    starts from the disk about to be wiped, not a specific mounted volume.
    Verified live against this session's own real APFS boot disk - the
    plist key names/shapes here (APFSPhysicalStores' entries keyed
    "DeviceIdentifier" in this global `diskutil list` view, vs. the
    differently-shaped "APFSPhysicalStore" key `diskutil info` on a single
    disk returns) were confirmed exactly, not assumed to match.
    Returns [(volume_id, volume_label)], or [] if nothing resolved (a
    container disk-utility itself failed to report, e.g.)."""
    for d in all_disks_and_partitions:
        stores = d.get('APFSPhysicalStores') or []
        if any(s.get('DeviceIdentifier') == container_partition_id for s in stores):
            return [(v['DeviceIdentifier'], v.get('VolumeName')) for v in d.get('APFSVolumes', [])]
    return []


def _backup_existing_data_macos(disk_id, backup_dir):
    out = _run(['diskutil', 'list', '-plist'], check=False, quiet=True).stdout
    import plistlib
    all_data = plistlib.loads(out.encode())
    all_disks_and_partitions = all_data.get('AllDisksAndPartitions', [])
    partitions = [
        p for d in all_disks_and_partitions
        if d.get('DeviceIdentifier') == disk_id
        for p in d.get('Partitions', [])
    ]
    if not partitions:
        return None

    # A partition that's really an APFS container isn't mountable itself -
    # see _resolve_apfs_container_volumes() - so expand it into its real
    # volumes here rather than trying (and failing) to mount the container
    # partition directly, which would silently skip everything inside it.
    expanded = []
    for p in partitions:
        if p.get('Content') == 'Apple_APFS':
            volumes = _resolve_apfs_container_volumes(p['DeviceIdentifier'], all_disks_and_partitions)
            expanded.extend({'DeviceIdentifier': vid, 'VolumeName': vname} for vid, vname in volumes)
            if not volumes:
                print(f'{p["DeviceIdentifier"]} is an APFS container but its volumes could not be '
                      f'resolved - skipping (unsupported/corrupt container, or nothing in it).')
        else:
            expanded.append(p)
    partitions = expanded

    backed_up_any = False
    for part in partitions:
        part_id = part['DeviceIdentifier']
        label = part.get('VolumeName') or part_id
        info = _diskutil_info(part_id)
        mount_point = info.get('MountPoint')
        we_mounted_it = False
        if not mount_point:
            result = _run(['diskutil', 'mount', part_id], check=False, quiet=True)
            if result.returncode != 0:
                print(f'Could not mount {part_id} ("{label}") to check it for existing data - '
                      f'skipping (unsupported filesystem, or nothing there to mount).')
                continue
            we_mounted_it = True
            mount_point = _diskutil_info(part_id).get('MountPoint')
            if not mount_point:
                continue

        copied = _backup_partition(mount_point, we_mounted_it,
                                    lambda pid=part_id: _run(['diskutil', 'unmount', pid], check=False, quiet=True),
                                    label, backup_dir)
        backed_up_any = backed_up_any or bool(copied)

    return backup_dir if backed_up_any else None


def _backup_existing_data_linux(disk_id, backup_dir):
    """disk_id is a whole-disk path like /dev/sdb. Finds its existing
    partitions via lsblk, mounts (read-only) any that aren't already
    mounted - best effort, since a filesystem this host has no kernel
    driver for simply can't be mounted, and is skipped with a warning
    rather than treated as empty."""
    out = _run(['lsblk', '-J', '-o', 'NAME,MOUNTPOINT,FSTYPE,LABEL', disk_id], check=False, quiet=True).stdout
    try:
        data = json.loads(out) if out.strip() else {'blockdevices': []}
    except ValueError:
        return None
    devices = data.get('blockdevices', [])
    if not devices:
        return None
    partitions = devices[0].get('children') or []
    if not partitions:
        return None

    backed_up_any = False
    for part in partitions:
        name = part.get('name')
        fstype = part.get('fstype')
        if not name or not fstype:
            continue  # no filesystem on this entry - nothing to read
        part_path = f'/dev/{name}'
        label = part.get('label') or name
        mount_point = part.get('mountpoint')
        we_mounted_it = False
        if not mount_point:
            mount_point = f'/mnt/hackintosh_backup_{name}'
            os.makedirs(mount_point, exist_ok=True)
            result = _run(['mount', '-o', 'ro', part_path, mount_point], check=False, quiet=True)
            if result.returncode != 0:
                print(f'Could not mount {part_path} ("{label}") to check it for existing data - '
                      f'skipping (unsupported filesystem, or nothing there to mount).')
                continue
            we_mounted_it = True

        copied = _backup_partition(mount_point, we_mounted_it,
                                    lambda mp=mount_point: _run(['umount', mp], check=False, quiet=True),
                                    label, backup_dir)
        backed_up_any = backed_up_any or bool(copied)

    return backup_dir if backed_up_any else None


def _backup_existing_data_windows(disk_id, backup_dir):
    """disk_id is the disk Number Get-Disk reports (see list_disks()).
    Finds its existing partitions via Get-Partition and backs up whatever
    already has a drive letter - one Windows hasn't assigned a letter to
    (or whose filesystem it can't read at all, e.g. a Linux-only one) is
    skipped with a warning, not silently treated as empty."""
    out = _run(['powershell', '-NoProfile', '-Command',
                f'Get-Partition -DiskNumber {disk_id} | ConvertTo-Json'], check=False, quiet=True).stdout
    try:
        data = json.loads(out) if out.strip() else []
    except ValueError:
        return None
    if isinstance(data, dict):
        data = [data]
    if not data:
        return None

    backed_up_any = False
    for part in data:
        letter = part.get('DriveLetter')
        part_number = part.get('PartitionNumber')
        if not letter or letter in ('\x00',):
            print(f'Partition {part_number} on disk {disk_id} has no drive letter assigned - '
                  f'skipping (can\'t read it without one).')
            continue
        label = f'{letter}_drive'
        mount_point = f'{letter}:\\'

        copied = _backup_partition(mount_point, False, lambda: None, label, backup_dir)
        backed_up_any = backed_up_any or bool(copied)

    return backup_dir if backed_up_any else None


def _find_macos_partitions(disk_id):
    """
    Finds the real EFI and TARGET partition identifiers on disk_id after
    partitioning it, by querying `diskutil list -plist` and matching on
    VolumeName/Content - NOT by assuming disk_id+'s1'/'s2', which this
    function replaced after that assumption broke a real installer run.

    Verified live: on at least one real SD card (an internal disk image
    does *not* reproduce this - only tested and confirmed on physical
    media), `diskutil partitionDisk <disk> GPT FAT32 EFI 300M JHFS+ TARGET R`
    silently prepended its own extra, genuinely-typed EFI System Partition
    ahead of the two partitions actually requested, producing THREE
    partitions instead of two: the real auto-created EFI first, the
    requested FAT32 one demoted to generic "Microsoft Basic Data" content
    (despite being named "EFI"), then TARGET last. The old disk_id+'s1'/'s2'
    code happened to still get a working EFI partition (macOS's own
    auto-created one landed on s1) but pointed target_partition at the
    wrong partition - the demoted FAT32 one, not TARGET - which then made
    write_basesystem.py's `asr restore --target` fail outright ("is not a
    volume"), confirmed against the traceback that reproduced this.

    When only one "EFI"-named partition exists (the case an internal disk
    image *does* reproduce, and evidently some real media too), that one is
    used regardless of its reported Content type. When more than one
    exists, the one actually typed "EFI" (a real GPT ESP, which is what
    firmware needs to recognize it as bootable) is preferred over a
    same-named but differently-typed impostor.
    """
    import plistlib
    out = _run(['diskutil', 'list', '-plist', disk_id], check=False, quiet=True).stdout
    data = plistlib.loads(out.encode())
    partitions = [
        p
        for d in data.get('AllDisksAndPartitions', [])
        if d.get('DeviceIdentifier') == disk_id
        for p in d.get('Partitions', [])
    ]

    target = next((p for p in partitions if p.get('VolumeName') == 'TARGET'), None)
    if not target:
        raise RuntimeError(f'No TARGET partition found on {disk_id} after partitioning: {partitions}')

    efi_candidates = [p for p in partitions if p.get('VolumeName') == 'EFI']
    if not efi_candidates:
        raise RuntimeError(f'No EFI partition found on {disk_id} after partitioning: {partitions}')
    efi = next((p for p in efi_candidates if p.get('Content') == 'EFI'), efi_candidates[0])

    return efi['DeviceIdentifier'], target['DeviceIdentifier']


def create_partitions(disk_id):
    """
    Wipes disk_id and lays down an EFI System Partition (FAT32,
    EFI_SIZE_MIB) and a second partition (for the macOS BaseSystem image).
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
        return _find_macos_partitions(disk_id)

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


def _diskutil_info(device_id):
    import plistlib
    return plistlib.loads(_run(['diskutil', 'info', '-plist', device_id], check=False, quiet=True).stdout.encode())


def mount_efi(efi_partition):
    """Returns a filesystem path the EFI partition is mounted at."""
    osname = hw_detect.host_os()

    if osname == 'macos':
        _run(['diskutil', 'mount', efi_partition])
        # Ask diskutil directly for the real mount point rather than parsing
        # `diskutil mount`'s own text output or assuming one - verified live
        # that output doesn't actually contain a path at all on current
        # diskutil ("Volume EFI on disk6s1 mounted", no "mounted at ..."
        # clause), and a hardcoded "/Volumes/EFI" fallback is wrong whenever
        # something else named "EFI" is already mounted: reproduced live,
        # a second EFI-named volume mounts as "/Volumes/EFI 1", and the old
        # code would have silently returned the *first* one's path instead -
        # copying/reading the wrong disk entirely with no error at all.
        info = _diskutil_info(efi_partition)
        mount_point = info.get('MountPoint')
        if not mount_point:
            raise RuntimeError(f'{efi_partition} does not appear to be mounted after `diskutil mount` - '
                                f'diskutil info reports no MountPoint.')
        return mount_point

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
