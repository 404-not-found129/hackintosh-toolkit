#!/usr/bin/env python3
"""
Copies a backup made by partition.py's backup_existing_data() (see
hackintosh_setup.py's "Backing up anything already on the disk" step) back
onto a real destination - the other half of that safety net, since a
backup nobody can get back isn't much of one.

The backup itself is nothing special - plain folders and files, one
subfolder per source partition (named after its volume label, listed in
MANIFEST.txt) sitting under hackintosh_build/usb_backup_<timestamp>/ - so
you can already just browse and copy out of it directly with Finder/
Explorer/a file manager. This exists for the same reason usb_map.py/
cpufriend.py do: a one-command way to do the same thing, with the same
disk-space guard already used everywhere else in this toolkit (see
macrecovery.py's _save_image(), partition.py's _backup_one_volume()) so it
fails before writing anything instead of partway through a large copy, and
without silently overwriting something already at the destination.
"""

import os
import shutil
import sys

import partition


def restore_backup(backup_dir, target_dir):
    """Copies every partition subfolder in backup_dir (everything except
    MANIFEST.txt) into target_dir, each under its own subfolder name
    unchanged. Returns the list of subfolder names actually restored (a
    name already present at the destination is skipped, not overwritten -
    printed either way)."""
    if not os.path.isdir(backup_dir):
        raise SystemExit(f'{backup_dir} is not a directory - point this at a real '
                          f'hackintosh_build/usb_backup_<timestamp> folder.')

    entries = sorted(
        name for name in os.listdir(backup_dir)
        if name != 'MANIFEST.txt' and os.path.isdir(os.path.join(backup_dir, name))
    )
    if not entries:
        raise SystemExit(f'Nothing to restore - {backup_dir} has no backed-up partition folders in it.')

    manifest_path = os.path.join(backup_dir, 'MANIFEST.txt')
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            print(f.read())

    os.makedirs(target_dir, exist_ok=True)
    needed = sum(partition.dir_size(os.path.join(backup_dir, name)) for name in entries)
    free = shutil.disk_usage(target_dir).free
    if free < needed * 1.05:
        raise SystemExit(
            f'Not enough free space at {target_dir}: restoring needs ~{needed / 2**30:.1f} GB '
            f'but only {free / 2**30:.1f} GB is free. Free up space and try again.'
        )

    restored = []
    for name in entries:
        source = os.path.join(backup_dir, name)
        dest = os.path.join(target_dir, name)
        if os.path.exists(dest):
            print(f'Skipping "{name}" - {dest} already exists (won\'t overwrite it).')
            continue
        print(f'Restoring "{name}" -> {dest} ...')
        shutil.copytree(source, dest, symlinks=True, ignore_dangling_symlinks=True)
        restored.append(name)

    return restored


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f'Usage: {sys.argv[0]} <backup folder, e.g. hackintosh_build/usb_backup_20260101_120000> '
              f'<destination folder to restore into>')
        sys.exit(1)
    _restored = restore_backup(sys.argv[1], sys.argv[2])
    if _restored:
        print(f"\nRestored {len(_restored)} partition folder(s): {', '.join(_restored)}")
    else:
        print('\nNothing restored - every folder already existed at the destination.')
