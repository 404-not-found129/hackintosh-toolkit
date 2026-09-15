#!/usr/bin/env python3
"""
Classifies a detected WiFi controller and picks a response, mirroring
bluetooth_compat.py's role for Bluetooth. WiFi has a wider spread of support
tiers than Bluetooth, though:

  - Broadcom (PCI vendor 0x14e4) - macOS ships a NATIVE driver
    (AirPortBrcmNIC.kext) for Broadcom Wi-Fi because real Macs used Broadcom
    for years. A card exactly matching Apple's whitelist works with zero
    extra kexts. One that doesn't (most OEM Broadcom cards - different
    subsystem ID than what Apple shipped) needs Acidanthera's
    AirportBrcmFixup to patch the whitelist check. That kext is documented
    as safe to include either way (it only patches what needs patching, and
    ships boot-args to disable it), so it's staged whenever any Broadcom
    WiFi PCI device is detected, regardless of exact match.

  - Intel (PCI vendor 0x8086) - Apple never shipped Intel WiFi in any real
    Mac, so there is no native path at all; the only option is a full
    third-party driver, OpenIntelWireless's itlwm. That project actually
    ships two kexts with a real tradeoff, not one:
      - AirportItlwm.kext: integrates into System Preferences like a real
        AirPort card, but per its own install docs needs either Apple
        Secure Boot enabled or an OpenCore "Force" config that injects
        IO80211Family extracted from a matching macOS - a live-system
        dependency in the same class as CPUFriend/GenSMBIOS, not something
        this toolkit can safely template pre-boot.
      - itlwm.kext: simpler, no special OpenCore config, "unzip and go" per
        the docs - but has no System Preferences integration; you connect
        via a separate menu-bar app, HeliPort.
    This module stages the simple itlwm.kext by default for exactly that
    reason, and prints the AirportItlwm tradeoff as an advisory rather than
    attempting the Force-injection config automatically.

  - Everything else (Atheros, Realtek, MediaTek, ...) - real but
    inconsistently-maintained community drivers exist for some of these;
    none are auto-staged here, only flagged.
"""

BROADCOM_PCI_VENDOR_ID = '14e4'
INTEL_PCI_VENDOR_ID = '8086'


def classify(controller):
    vendor_id = (controller.get('vendor_id') or '').lower()
    if vendor_id == BROADCOM_PCI_VENDOR_ID:
        return 'broadcom'
    if vendor_id == INTEL_PCI_VENDOR_ID:
        return 'intel'
    return 'unknown'


def evaluate(controllers):
    """
    Returns {'action': 'broadcom_fixup'|'intel_itlwm'|'advisory'|'none',
              'kexts': [...], 'notes': [...]}.
    'kexts' lists Acidanthera-style kext names fetchable via the normal
    per-repo release-zip path; itlwm is fetched separately (see
    opencore_build.py) since its release asset naming doesn't match that
    pattern.
    """
    if not controllers:
        return {'action': 'none', 'kexts': [], 'notes': []}

    notes = []
    kinds = set()
    for c in controllers:
        kind = classify(c)
        kinds.add(kind)
        tag = f'[{c.get("vendor_id")}:{c.get("device_id")}]'
        if kind == 'broadcom':
            notes.append(f'{c["name"]} {tag}: Broadcom - staging AirportBrcmFixup (safe even if already native).')
        elif kind == 'intel':
            notes.append(f'{c["name"]} {tag}: Intel - no native macOS driver exists; staging itlwm.kext '
                          '(connect via HeliPort). AirportItlwm.kext integrates with System Preferences '
                          'instead, but needs extra OpenCore Kernel->Force config after first boot - not auto-set-up.')
        else:
            notes.append(f'{c["name"]} {tag}: unrecognized chipset - not auto-patched.')

    if 'broadcom' in kinds:
        return {'action': 'broadcom_fixup', 'kexts': ['AirportBrcmFixup'], 'notes': notes}
    if 'intel' in kinds:
        return {'action': 'intel_itlwm', 'kexts': [], 'notes': notes}
    return {'action': 'advisory', 'kexts': [], 'notes': notes}
