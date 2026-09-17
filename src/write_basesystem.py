#!/usr/bin/env python3
"""
Writes the downloaded BaseSystem.dmg onto the target partition created by
partition.py, so it becomes bootable.

- macOS: uses Apple's own `asr` (Apple Software Restore) - the exact tool
  `createinstallmedia` itself uses. No conversion needed, most reliable path.
- Linux/Windows: macOS's .dmg (UDIF) format isn't natively writable outside
  macOS. The standard, widely-documented community method is to convert it
  to a raw image with the open-source `dmg2img` tool, then block-copy that
  raw image onto the partition with `dd`. This toolkit shells out to those
  external tools rather than reimplementing UDIF parsing - install them
  first:
    Linux:   sudo apt install dmg2img   (or your distro's equivalent)
    Windows: dmg2img and a Windows dd build must be on PATH, e.g. via
             `choco install dmg2img dd`
"""

import os
import shutil
import subprocess
import sys

import hw_detect


def _run(cmd):
    print('  $', ' '.join(cmd))
    subprocess.run(cmd, check=True)


def write(dmg_path, target_partition):
    osname = hw_detect.host_os()

    if osname == 'macos':
        # asr's own man page: "--target can be a /dev entry, or volume
        # mountpoint" - a bare diskutil identifier like "disk5s3" is
        # neither, and asr silently resolves it as a *relative filesystem
        # path* from the current directory instead of a disk identifier
        # (confirmed live: the exact error was '"<cwd>/disk5s3" is not a
        # volume', not any kind of "unknown device" message - a bare id
        # doesn't start with "/", so plain path resolution kicks in).
        # partition.py's create_partitions()/mount_efi() intentionally
        # return/use bare identifiers throughout (that's diskutil's own
        # convention, matched consistently there), so it's this call site's
        # job to add the /dev/ prefix asr specifically requires, not
        # partition.py's.
        target = target_partition if target_partition.startswith('/dev/') else f'/dev/{target_partition}'
        print(f'Restoring {dmg_path} -> {target} with asr (this can take a while)...')
        _run(['asr', 'restore', '--source', dmg_path, '--target', target,
              '--erase', '--noprompt'])
        return

    if osname in ('linux', 'windows'):
        dmg2img = shutil.which('dmg2img')
        dd = shutil.which('dd')
        if not dmg2img or not dd:
            missing = []
            if not dmg2img:
                missing.append('dmg2img')
            if not dd:
                missing.append('dd')
            raise SystemExit(
                f'Missing required tool(s): {", ".join(missing)}. Install them and re-run. '
                'See the module docstring for the platform-specific install command.'
            )

        img_path = os.path.splitext(dmg_path)[0] + '.img'
        print(f'Converting {dmg_path} -> {img_path} with dmg2img...')
        _run([dmg2img, dmg_path, img_path])

        if osname == 'windows':
            target = target_partition
            if not target.startswith('\\\\.\\'):
                target = '\\\\.\\' + target.rstrip('\\')
        else:
            target = target_partition

        print(f'Writing {img_path} -> {target} with dd (this can take a while)...')
        _run([dd, f'if={img_path}', f'of={target}', 'bs=4M', 'status=progress', 'conv=fsync'])
        return

    raise RuntimeError(f'Unsupported OS: {osname}')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f'Usage: {sys.argv[0]} <BaseSystem.dmg> <target partition>')
        sys.exit(1)
    write(sys.argv[1], sys.argv[2])
