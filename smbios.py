#!/usr/bin/env python3
"""
Generates a real, valid-format SMBIOS identity (serial / board serial (MLB) /
system UUID / ROM) for a chosen Mac model, using Acidanthera's own official
`macserial` tool - the same generator GenSMBIOS drives, prebuilt for macOS,
Linux and Windows and shipped directly inside every OpenCorePkg release zip
(Utilities/macserial/{macserial,macserial.linux,macserial.exe}).

Unlike ACPI patches or USB port maps, this needs no data from your specific
motherboard - it's a self-contained generator - so it's fully automatic.

ROM generation follows the method documented in Dortania's own iMessage/
iServices activation guide (github.com/dortania/OpenCore-Post-Install,
universal/iservices.md): ROM should be a real network adapter's actual MAC
address (lowercase, no colons), not random bytes - Apple's activation
servers key off it, and a real adapter's real MAC is what the guide's whole
worked example uses. This module auto-detects the machine's primary
Ethernet MAC (hw_detect.get_primary_mac()) and uses that; if none is found
(no wired adapter), it falls back to a synthetic address under a real
Apple-registered OUI (00:16:CB, confirmed in the same guide) with a random
tail - the guide's own documented alternative when you don't want to tie
ROM to a specific physical adapter.

One caveat inherited from every hackintosh guide: a generated serial is
*valid-format*, not a serial Apple actually issued to a real Mac you own.
That is fine for local use, but don't expect Apple's online tools (Check
Coverage, etc.) to recognise it, and be aware Apple can flag services if a
generated identity collides with, or too closely imitates, a real customer's.
See imessage.py for the fuller iMessage-specific checklist (candidate
serial selection, marking the right adapter built-in, NVRAM, and what to do
if an account gets flagged).
"""

import os
import platform
import random
import re
import subprocess
import uuid

import net

MACSERIAL_ASSET_BY_OS = {
    'macos': 'Utilities/macserial/macserial',
    'linux': 'Utilities/macserial/macserial.linux',
    'windows': 'Utilities/macserial/macserial.exe',
}


def _macserial_binary(workdir, oc_extract_dir=None):
    import hw_detect
    osname = hw_detect.host_os()
    rel_path = MACSERIAL_ASSET_BY_OS[osname]

    if oc_extract_dir:
        candidate = net.find_first(oc_extract_dir, lambda p: p.replace('\\', '/').endswith(rel_path))
        if candidate:
            if osname != 'windows':
                os.chmod(candidate, 0o755)
            return candidate

    # No OpenCorePkg tree handed to us (e.g. running smbios.py standalone) - fetch our own.
    oc_zip = net.fetch_latest_release_zip('acidanthera/OpenCorePkg', workdir)
    extract_dir = net.extract_zip(oc_zip, os.path.join(workdir, 'OpenCorePkg_for_smbios'))
    candidate = net.find_first(extract_dir, lambda p: p.replace('\\', '/').endswith(rel_path))
    if not candidate:
        raise RuntimeError(f'Could not find macserial for {osname} in OpenCorePkg release')
    if osname != 'windows':
        os.chmod(candidate, 0o755)
    return candidate


APPLE_OUI_FALLBACK = ('00', '16', 'cb')  # confirmed real Apple OUI, per Dortania's iServices guide


def _synthetic_apple_mac():
    tail = [random.randint(0, 255) for _ in range(3)]
    return ':'.join(APPLE_OUI_FALLBACK + tuple(f'{b:02x}' for b in tail))


def _generate_serials(macserial, model, count):
    result = subprocess.run([macserial, '-g', '-m', model, '-n', str(count)],
                             capture_output=True, text=True, timeout=15)
    pairs = []
    for line in result.stdout.splitlines():
        if '|' not in line:
            continue
        serial, mlb = (part.strip() for part in line.split('|', 1))
        if re.fullmatch(r'[A-Z0-9]{11,12}', serial):
            pairs.append((serial, mlb))
    if not pairs:
        raise RuntimeError(f'macserial did not return a serial for model {model}: {result.stdout} {result.stderr}')
    return pairs


def generate(model, workdir, oc_extract_dir=None, real_mac=None):
    """
    Returns {'SystemProductName': model, 'SystemSerialNumber': str,
             'MLB': str, 'SystemUUID': str, 'ROM': bytes, 'ROM_mac': str,
             'ROM_is_real': bool}.

    real_mac: a real MAC string ("aa:bb:cc:dd:ee:ff") to base ROM on. Pass
    explicitly to pin it to a specific adapter (see imessage.py); if omitted,
    auto-detects the machine's primary Ethernet MAC, falling back to a
    synthetic Apple-OUI address if none is found - see module docstring.
    """
    os.makedirs(workdir, exist_ok=True)
    macserial = _macserial_binary(workdir, oc_extract_dir)

    serial, mlb = _generate_serials(macserial, model, 1)[0]

    if real_mac is None:
        import hw_detect
        real_mac = hw_detect.get_primary_mac()
    rom_is_real = real_mac is not None
    mac = real_mac or _synthetic_apple_mac()
    rom = bytes.fromhex(mac.replace(':', '').lower())

    system_uuid = str(uuid.uuid4()).upper()

    return {
        'SystemProductName': model,
        'SystemSerialNumber': serial,
        'MLB': mlb,
        'SystemUUID': system_uuid,
        'ROM': rom,
        'ROM_mac': mac,
        'ROM_is_real': rom_is_real,
    }


def generate_candidates(model, workdir, count=10, oc_extract_dir=None, real_mac=None):
    """
    Like generate(), but returns `count` serial/MLB candidates instead of
    one, for the iMessage workflow of manually checking each at
    https://checkcoverage.apple.com/ and picking one Apple reports as
    unrecognised (see imessage.py). Every candidate shares the same
    ROM/UUID - only the serial/MLB pair differs between them.
    """
    os.makedirs(workdir, exist_ok=True)
    macserial = _macserial_binary(workdir, oc_extract_dir)
    pairs = _generate_serials(macserial, model, count)

    if real_mac is None:
        import hw_detect
        real_mac = hw_detect.get_primary_mac()
    rom_is_real = real_mac is not None
    mac = real_mac or _synthetic_apple_mac()
    rom = bytes.fromhex(mac.replace(':', '').lower())
    system_uuid = str(uuid.uuid4()).upper()

    return [
        {
            'SystemProductName': model,
            'SystemSerialNumber': serial,
            'MLB': mlb,
            'SystemUUID': system_uuid,
            'ROM': rom,
            'ROM_mac': mac,
            'ROM_is_real': rom_is_real,
        }
        for serial, mlb in pairs
    ]


def apply_to_config(config, identity):
    """Writes the generated identity into an already-loaded config.plist dict's PlatformInfo."""
    generic = config.setdefault('PlatformInfo', {}).setdefault('Generic', {})
    generic['SystemProductName'] = identity['SystemProductName']
    generic['SystemSerialNumber'] = identity['SystemSerialNumber']
    generic['MLB'] = identity['MLB']
    generic['SystemUUID'] = identity['SystemUUID']
    generic['ROM'] = identity['ROM']
    config.setdefault('PlatformInfo', {})['UpdateSMBIOS'] = True
    config.setdefault('PlatformInfo', {})['Automatic'] = True
    return config


if __name__ == '__main__':
    import sys
    import plistlib
    model = sys.argv[1] if len(sys.argv) > 1 else 'iMac19,1'
    identity = generate(model, os.path.join(os.getcwd(), 'smbios_work'))
    printable = dict(identity)
    printable['ROM'] = printable['ROM'].hex()
    print(printable)
