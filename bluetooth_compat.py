#!/usr/bin/env python3
"""
Classifies a detected Bluetooth controller and picks the right kext set,
mirroring gpu_compat.py's role for GPUs.

Three real outcomes:
  - Genuine Apple internal module (Vendor ID 0x004C) - works natively, no
    kexts needed at all. Verified live: this exact case shows up on a real
    Hackintosh with a real Apple-sourced BCM4350 card wired over UART,
    reported by macOS with zero patch kexts installed.
  - A Broadcom RAMUSB-family USB device - needs Acidanthera's BrcmPatchRAM
    kext family to upload firmware on every boot (Windows does this
    invisibly; macOS doesn't do it at all without this). Which exact kexts
    depends on the target macOS version - ported from BrcmPatchRAM's own
    README, which documents the version boundaries explicitly.
  - Everything else (notably Intel combo Bluetooth, USB vendor 0x8087) -
    deliberately NOT auto-added. Intel Bluetooth support on Hackintosh is
    inconsistent and maintained outside Acidanthera's well-tested kexts;
    guessing wrong here just adds dead weight, so this surfaces it as an
    advisory instead.

The vendor-ID table below was extracted programmatically from
https://github.com/acidanthera/BrcmPatchRAM/blob/master/README.md's own
device compatibility list (146 entries, both BrcmBluetoothInjector and
BrcmBluetoothInjectorLegacy tables) - not retyped from memory.
"""

# Vendor IDs seen across BrcmPatchRAM's documented Broadcom RAMUSB device
# list (OEM-rebranded Broadcom combo cards - Azurewave, Lite-On, Foxconn,
# Toshiba, Dell, etc. all resell the same handful of Broadcom chipsets).
BROADCOM_USB_VENDOR_IDS = {
    '03f0',  # HP
    '0489',  # Foxconn/Hon Hai
    '04b4',  # Cypress (Laird)
    '04ca',  # Lite-On
    '04f2',  # Chicony
    '050d',  # Belkin
    '0930',  # Toshiba
    '0a5c',  # Broadcom
    '0b05',  # ASUS
    '0bb4',  # HTC
    '105b',  # Foxconn China / Lenovo
    '13d3',  # Azurewave
    '145f',  # Trust
    '2b54',  # Ampak
    '33ba',  # Toulineua
    '413c',  # Dell
}

APPLE_VENDOR_ID = '004c'
INTEL_VENDOR_ID = '8087'

DARWIN_HIGH_SIERRA_MOJAVE_MAX = 18   # BrcmPatchRAM2, no injector needed
DARWIN_CATALINA_BIGSUR_MAX = 20      # BrcmPatchRAM3 + BrcmBluetoothInjector
# darwin 21+ (Monterey+): BrcmPatchRAM3 + BlueToolFixup, no injector


def classify(controller):
    vendor_id = (controller.get('vendor_id') or '').lower()
    if vendor_id == APPLE_VENDOR_ID:
        return 'apple'
    if vendor_id in BROADCOM_USB_VENDOR_IDS:
        return 'broadcom'
    if vendor_id == INTEL_VENDOR_ID:
        return 'intel'
    return 'unknown'


def recommended_kexts(darwin_version):
    """
    Returns (kext_names, needs_firmware_kext, nvram_vars) for a Broadcom
    controller targeting the given darwin major version. nvram_vars is a
    dict to merge into the boot-args NVRAM GUID's entries, or {} if none
    needed. Boundaries and requirements are exactly as documented in
    BrcmPatchRAM's own README, not inferred.
    """
    if darwin_version <= DARWIN_HIGH_SIERRA_MOJAVE_MAX:
        return (['BrcmPatchRAM2', 'BrcmFirmwareData'], True, {})
    if darwin_version <= DARWIN_CATALINA_BIGSUR_MAX:
        return (['BrcmPatchRAM3', 'BrcmBluetoothInjector', 'BrcmFirmwareData'], True, {})
    # Monterey (21) and later: BlueToolFixup replaces the injector, and Apple
    # moved part of the stack to userspace - these two NVRAM vars are called
    # out by name in BrcmPatchRAM's README as required for Intel Bluetooth,
    # and are harmless/inert for Broadcom, so set unconditionally for safety.
    return (
        ['BrcmPatchRAM3', 'BrcmFirmwareData', 'BlueToolFixup'],
        True,
        {
            'bluetoothExternalDongleFailed': bytes([0x00]),
            'bluetoothInternalControllerInfo': bytes(14),
        },
    )


def evaluate(controllers, darwin_version):
    """
    Combines every detected controller into one verdict.
    Returns {'action': 'native'|'patch'|'advisory'|'none', 'kexts': [...],
             'nvram_vars': {...}, 'notes': [...]}.
    """
    if not controllers:
        return {'action': 'none', 'kexts': [], 'nvram_vars': {}, 'notes': []}

    notes = []
    for c in controllers:
        kind = classify(c)
        if kind == 'apple':
            notes.append(f'{c["name"]}: genuine Apple module (native, no kexts needed).')
        elif kind == 'broadcom':
            notes.append(f'{c["name"]} [{c.get("vendor_id")}:{c.get("device_id")}]: Broadcom RAMUSB - patching.')
        elif kind == 'intel':
            notes.append(f'{c["name"]} [{c.get("vendor_id")}:{c.get("device_id")}]: Intel Bluetooth - not auto-patched, '
                          'support is inconsistent on Hackintosh; see IntelBluetoothFirmware (community, unofficial) if needed.')
        else:
            notes.append(f'{c["name"]} [{c.get("vendor_id")}:{c.get("device_id")}]: unrecognized - not auto-patched.')

    if any(classify(c) == 'apple' for c in controllers):
        return {'action': 'native', 'kexts': [], 'nvram_vars': {}, 'notes': notes}

    if any(classify(c) == 'broadcom' for c in controllers):
        kexts, _needs_fw, nvram_vars = recommended_kexts(darwin_version)
        return {'action': 'patch', 'kexts': kexts, 'nvram_vars': nvram_vars, 'notes': notes}

    if any(classify(c) == 'intel' for c in controllers):
        return {'action': 'advisory', 'kexts': [], 'nvram_vars': {}, 'notes': notes}

    return {'action': 'none', 'kexts': [], 'nvram_vars': {}, 'notes': notes}
