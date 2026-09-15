#!/usr/bin/env python3
"""
iMessage/FaceTime/iCloud activation checklist, following the method
documented in Dortania's own guide (github.com/dortania/OpenCore-Post-Install,
universal/iservices.md) - reproduced here because it's the actual, current
community-standard checklist, not reinvented:

  1. ROM must be a real network adapter's MAC address (smbios.py handles
     this automatically now - see its docstring for the fix this drove).
  2. That SAME adapter must be marked "built-in" via a DeviceProperties
     patch, so macOS's `en0` assignment converges on it and its MAC matches
     ROM. This module does that - see apply_builtin_property().
  3. A valid, uniquely-generated serial (smbios.py, already handled) -
     ideally one Apple's Check Coverage page reports as unrecognized rather
     than already registered to a real device. See generate_candidates() in
     smbios.py and print_checklist() below for the manual verification step
     (deliberately NOT automated - see the note in print_checklist).
  4. Working NVRAM (real or emulated) - iMessage keys live there.
  5. If activation still fails or gets flagged, there's a documented
     account-side cleanup process - see reset_caches() below for the
     Terminal commands, run from the installed macOS itself.

**Nobody can guarantee iMessage will activate** - Apple's server-side fraud
detection is opaque and changes over time, and even a perfectly-generated
identity sometimes gets flagged, especially on Apple IDs with no real Apple
hardware history. This module automates the mechanical, well-documented
parts; the account-standing part is inherently outside any tool's control.
Read the full guide before you rely on any of this: exactly the same
disclaimer it carries applies here - you are responsible for your own
Apple ID.
"""

import os
import plistlib
import subprocess
import sys

import hw_detect


def find_builtin_candidate():
    """
    Picks the Ethernet controller smbios.py's ROM should be based on, and
    that config.plist's DeviceProperties should mark built-in. Prefers a
    controller with a resolvable bus-0 PCI path (see
    hw_detect.get_ethernet_controllers() for why only bus-0 resolves);
    falls back to the first controller with a real MAC even without a path,
    since ROM can still be set correctly even when built-in can't be
    auto-injected (matching upstream's Hackintool-based manual fallback).
    """
    controllers = hw_detect.get_ethernet_controllers()
    with_path = [c for c in controllers if c.get('pci_path') and c.get('mac')]
    if with_path:
        return with_path[0]
    with_mac = [c for c in controllers if c.get('mac')]
    return with_mac[0] if with_mac else None


def apply_builtin_property(config, controller):
    """
    Adds DeviceProperties -> Add -> <pci_path> -> built-in = 0x01 for the
    given controller (from find_builtin_candidate()). No-op if the
    controller has no resolvable PCI path - see module/hw_detect docstrings
    for why (not every platform/bus layout can cheaply resolve one); in
    that case, use Hackintool to find and set it manually, exactly as
    upstream's guide describes.
    """
    if not controller or not controller.get('pci_path'):
        return False
    device_props = config.setdefault('DeviceProperties', {}).setdefault('Add', {})
    device_props.setdefault(controller['pci_path'], {})['built-in'] = bytes([0x01])
    return True


def print_checklist(identity, controller):
    print()
    print('=' * 70)
    print('iMessage/iCloud activation checklist')
    print('=' * 70)
    if controller and controller.get('mac'):
        source = 'this real adapter\'s' if identity.get('ROM_is_real') else 'a synthetic Apple-OUI'
        print(f'- ROM is set to {source} MAC: {identity.get("ROM_mac")}')
    else:
        print(f'- ROM is set to a synthetic Apple-OUI MAC: {identity.get("ROM_mac")} (no wired adapter detected)')
    if controller and controller.get('pci_path'):
        print(f'- Marked {controller["name"]} ({controller["pci_path"]}) as built-in in DeviceProperties.')
    elif controller:
        print(f'- Found {controller["name"]} but could not auto-resolve its PCI path.')
        print('  Use Hackintool (System -> Peripherals, or the PCI tab\'s DeviceProperties export)')
        print('  to find it and add built-in=01 (Data) yourself - see the guide linked below.')
    else:
        print('- No wired Ethernet adapter detected at all.')
        print('  If WiFi will be your only connection, en0 can still be a WiFi/Thunderbolt port -')
        print('  the "built-in" step is the same, just target that adapter\'s PCI path instead.')
        print('  If you have no working network adapter of any kind, the documented fallback is')
        print('  NullEthernet.kext + ssdt-rmne.aml - see:')
        print('  https://github.com/RehabMan/OS-X-Null-Ethernet (source; precompiled kext linked from its README)')
    print()
    print('Still needed - genuine judgment calls this tool will not make for you:')
    print('1. Check your generated serial(s) at https://checkcoverage.apple.com/')
    print('   Ideally pick one showing "unable to check coverage" (never registered).')
    print('   NOTE: this toolkit does NOT query that page for you - it is a normal Apple')
    print('   web form, checking a handful by hand (as every guide describes) stays well')
    print('   clear of anything that looks like scripted/automated querying.')
    print('2. Verify NVRAM works (real or emulated) - see OpenCore-Post-Install\'s')
    print('   misc/nvram.md. iMessage keys live in NVRAM; without it, nothing persists.')
    print('3. Your Apple ID matters more than any of the above: an ID with real Apple')
    print('   hardware/purchase history activates far more reliably than a brand-new one.')
    print()
    print('Full guide (read before relying on any of this):')
    print('https://github.com/dortania/OpenCore-Post-Install/blob/master/universal/iservices.md')
    print('=' * 70)


CACHE_PATHS = (
    '~/Library/Caches/com.apple.iCloudHelper*',
    '~/Library/Caches/com.apple.Messages*',
    '~/Library/Caches/com.apple.imfoundation.IMRemoteURLConnectionAgent*',
    '~/Library/Preferences/com.apple.iChat*',
    '~/Library/Preferences/com.apple.icloud*',
    '~/Library/Preferences/com.apple.imagent*',
    '~/Library/Preferences/com.apple.imessage*',
    '~/Library/Preferences/com.apple.imservice*',
    '~/Library/Preferences/com.apple.ids.service*',
    '~/Library/Preferences/com.apple.madrid.plist*',
    '~/Library/Preferences/com.apple.imessage.bag.plist*',
    '~/Library/Preferences/com.apple.identityserviced*',
    '~/Library/Preferences/com.apple.security*',
    '~/Library/Messages',
)


def reset_caches(dry_run=True):
    """
    Reproduces the cache/preference cleanup from the same iservices.md guide,
    for retrying activation after a failed attempt. macOS only, and only
    meaningful run from the actual installed system (the account, not this
    pre-boot toolkit) - see __main__ below.

    dry_run=True (default) only prints what would be removed. Pass
    dry_run=False to actually delete - this is real, user-owned data
    (cached iMessage/iCloud state), not system files, but it's still
    irreversible, so the default stays non-destructive.
    """
    if hw_detect.host_os() != 'macos':
        raise RuntimeError('This only makes sense run from the installed macOS itself.')

    import glob
    removed = []
    for pattern in CACHE_PATHS:
        expanded = os.path.expanduser(pattern)
        for path in glob.glob(expanded):
            removed.append(path)
            if dry_run:
                print(f'Would remove: {path}')
            else:
                print(f'Removing: {path}')
                subprocess.run(['rm', '-rf', path])
    if not removed:
        print('Nothing found to remove (already clean, or nothing has been attempted yet).')
    return removed


if __name__ == '__main__':
    if '--apply' in sys.argv:
        reset_caches(dry_run=False)
    else:
        print('Dry run (nothing will be deleted) - pass --apply to actually remove these:')
        print()
        reset_caches(dry_run=True)
