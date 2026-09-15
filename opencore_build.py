#!/usr/bin/env python3
"""
Downloads the latest OpenCorePkg release plus the standard baseline kexts
(Lilu, VirtualSMC, WhateverGreen, AppleALC) straight from Acidanthera's
official GitHub releases, and lays out a bootable EFI folder with a
config.plist templated off OpenCore's own Sample.plist.

What this automates: OpenCore + kext binaries, kext load-order entries, a
sane SecureBootModel/boot-args baseline, GPU-driver selection (including
the "no native acceleration" override), a real (non-placeholder) SMBIOS
serial/MLB/UUID/ROM via smbios.py, and a safe XhciPortLimit baseline for
USB until you run the real per-port mapping step.

ACPI SSDT patches and precise USB port mapping are genuinely per-motherboard
- they need a live dump/probe of the exact target hardware, so they are
handled as separate post-build steps (acpi_patches.py, usb_map.py) that
hackintosh_setup.py offers to run against the EFI this module just built.
"""

import os
import plistlib
import shutil

import bluetooth_compat
import hw_detect
import imessage
import net
import smbios
import wifi_compat

_fetch_release_zip = net.fetch_latest_release_zip
_find_first = net.find_first
_extract = net.extract_zip

KEXT_REPOS = {
    'Lilu': 'acidanthera/Lilu',
    'VirtualSMC': 'acidanthera/VirtualSMC',
    'WhateverGreen': 'acidanthera/WhateverGreen',
    'AppleALC': 'acidanthera/AppleALC',
    'RestrictEvents': 'acidanthera/RestrictEvents',
    'NVMeFix': 'acidanthera/NVMeFix',
    'CPUFriend': 'acidanthera/CPUFriend',
}

BRCM_REPO = 'acidanthera/BrcmPatchRAM'
# BrcmBluetoothInjector is a codeless/data-only kext (no Mach-O binary) -
# verified against the actual release zip, not assumed.
BRCM_DATA_ONLY_KEXTS = {'BrcmBluetoothInjector'}


def _fetch_kext(name, repo, workdir):
    zip_path = _fetch_release_zip(repo, workdir)
    extract_dir = os.path.join(workdir, name)
    _extract(zip_path, extract_dir)
    kext_path = _find_first(extract_dir, lambda p: p.endswith(f'{name}.kext') and os.path.isdir(p))
    if not kext_path:
        raise RuntimeError(f'Could not locate {name}.kext inside downloaded release')
    return kext_path


def _fetch_brcm_kexts(names, workdir):
    """BrcmPatchRAM/BrcmFirmwareData/BrcmBluetoothInjector/BlueToolFixup all
    ship together in one release zip - fetch it once for however many of
    them are needed, instead of once per kext."""
    zip_path = _fetch_release_zip(BRCM_REPO, workdir)
    extract_dir = os.path.join(workdir, 'BrcmPatchRAM')
    _extract(zip_path, extract_dir)
    paths = {}
    for name in names:
        kext_path = _find_first(extract_dir, lambda p, n=name: p.endswith(f'{n}.kext') and os.path.isdir(p))
        if not kext_path:
            raise RuntimeError(f'Could not locate {name}.kext inside BrcmPatchRAM release')
        paths[name] = kext_path
    return paths


ITLWM_REPO = 'OpenIntelWireless/itlwm'


def _fetch_itlwm(workdir):
    """
    itlwm's release assets don't follow the {name}-{version}-RELEASE.zip
    pattern the generic fetcher expects (e.g. "itlwm_v2.3.0_stable.kext.zip",
    plus several per-macOS-version "AirportItlwm_..." variants) - verified
    against the actual release. This grabs the plain, version-agnostic
    itlwm.kext build (see wifi_compat.py's docstring for why itlwm.kext
    rather than AirportItlwm.kext is the one staged automatically).
    """
    os.makedirs(workdir, exist_ok=True)
    release = net.api_get(net.GITHUB_API_LATEST_RELEASE.format(repo=ITLWM_REPO))
    asset = next((a for a in release.get('assets', [])
                  if a['name'].lower().startswith('itlwm_') and a['name'].lower().endswith('.kext.zip')), None)
    if not asset:
        raise RuntimeError(f'Could not find a plain itlwm.kext asset in {ITLWM_REPO} release {release.get("tag_name")}')
    zip_path = os.path.join(workdir, asset['name'])
    print(f'Downloading {asset["name"]} ({asset["size"] / 1024:.0f} KB)...')
    net.download(asset['browser_download_url'], zip_path)
    extract_dir = os.path.join(workdir, 'itlwm')
    _extract(zip_path, extract_dir)
    kext_path = _find_first(extract_dir, lambda p: p.endswith('itlwm.kext') and os.path.isdir(p))
    if not kext_path:
        raise RuntimeError('Could not locate itlwm.kext inside downloaded release')
    return kext_path


def _kext_entry(bundle_name, executable=True):
    return {
        'Arch': 'Any',
        'BundlePath': f'{bundle_name}.kext',
        'Comment': '',
        'Enabled': True,
        'ExecutablePath': f'Contents/MacOS/{bundle_name}' if executable else '',
        'MaxKernel': '',
        'MinKernel': '',
        'PlistPath': 'Contents/Info.plist',
    }


def build_efi(efi_mount_path, macos_version, gpus, no_acceleration, workdir, smbios_model='iMac19,1',
               enable_cpufriend=False):
    """
    efi_mount_path: filesystem path where the EFI partition is mounted
                    (e.g. '/Volumes/EFI', '/mnt/efi', 'S:\\').
    macos_version:  one entry from macrecovery.MACOS_VERSIONS.
    gpus:           list from hw_detect.get_gpus().
    no_acceleration: bool - if True, skip GPU-vendor kexts/patches and add
                      the appropriate "just give me a picture" boot-args.
    smbios_model:   Mac model identifier to generate a real SMBIOS identity
                    for (see smbios.py). Pick one whose CPU generation and
                    discrete-GPU presence roughly match your hardware.
    enable_cpufriend: stages CPUFriend.kext (opt-in, off by default per the
                    kext's own docs: most builds don't need it - it's inert
                    without a matching CPUFriendDataProvider.kext, which can
                    only be generated after first boot; see cpufriend.py).
    """
    os.makedirs(workdir, exist_ok=True)

    oc_zip = _fetch_release_zip('acidanthera/OpenCorePkg', workdir)
    oc_extract = _extract(oc_zip, os.path.join(workdir, 'OpenCorePkg'))
    efi_src = _find_first(oc_extract, lambda p: p.endswith(os.path.join('X64', 'EFI')) and os.path.isdir(p))
    if not efi_src:
        raise RuntimeError('Could not find X64/EFI folder in OpenCorePkg release')
    sample_plist = _find_first(oc_extract, lambda p: p.endswith('Sample.plist'))
    if not sample_plist:
        raise RuntimeError('Could not find Sample.plist in OpenCorePkg release')

    efi_dest = os.path.join(efi_mount_path, 'EFI')
    if os.path.exists(efi_dest):
        shutil.rmtree(efi_dest)
    shutil.copytree(efi_src, efi_dest)

    oc_dir = os.path.join(efi_dest, 'OC')
    kexts_dir = os.path.join(oc_dir, 'Kexts')
    os.makedirs(kexts_dir, exist_ok=True)

    with open(sample_plist, 'rb') as f:
        config = plistlib.load(f)

    kext_add = []
    vendor = gpus[0]['vendor'] if gpus else None

    for name in ('Lilu', 'VirtualSMC'):
        path = _fetch_kext(name, KEXT_REPOS[name], workdir)
        shutil.copytree(path, os.path.join(kexts_dir, f'{name}.kext'), dirs_exist_ok=True)
        kext_add.append(_kext_entry(name))

    if not no_acceleration:
        path = _fetch_kext('WhateverGreen', KEXT_REPOS['WhateverGreen'], workdir)
        shutil.copytree(path, os.path.join(kexts_dir, 'WhateverGreen.kext'), dirs_exist_ok=True)
        kext_add.append(_kext_entry('WhateverGreen'))

    path = _fetch_kext('AppleALC', KEXT_REPOS['AppleALC'], workdir)
    shutil.copytree(path, os.path.join(kexts_dir, 'AppleALC.kext'), dirs_exist_ok=True)
    kext_add.append(_kext_entry('AppleALC'))

    # Widely-recommended baseline: fixes model-check panics in various Apple
    # daemons/frameworks on unsupported SMBIOS models. No config needed.
    path = _fetch_kext('RestrictEvents', KEXT_REPOS['RestrictEvents'], workdir)
    shutil.copytree(path, os.path.join(kexts_dir, 'RestrictEvents.kext'), dirs_exist_ok=True)
    kext_add.append(_kext_entry('RestrictEvents'))

    if hw_detect.has_nvme():
        print('NVMe storage detected - adding NVMeFix (power-state/sleep fixes).')
        path = _fetch_kext('NVMeFix', KEXT_REPOS['NVMeFix'], workdir)
        shutil.copytree(path, os.path.join(kexts_dir, 'NVMeFix.kext'), dirs_exist_ok=True)
        kext_add.append(_kext_entry('NVMeFix'))

    if enable_cpufriend:
        # CPUFriend.kext alone does nothing (see its own docs: "if nothing is
        # provided, CPUFriend does nothing"); the data half
        # (CPUFriendDataProvider.kext) can only be generated after first
        # boot into macOS - see cpufriend.py. Staging the driver now just
        # means it's ready and waiting for that follow-up step.
        path = _fetch_kext('CPUFriend', KEXT_REPOS['CPUFriend'], workdir)
        shutil.copytree(path, os.path.join(kexts_dir, 'CPUFriend.kext'), dirs_exist_ok=True)
        kext_add.append(_kext_entry('CPUFriend'))

    bt_controllers = hw_detect.get_bluetooth_controllers()
    bt_result = bluetooth_compat.evaluate(bt_controllers, macos_version['darwin'])
    for note in bt_result['notes']:
        print(f'Bluetooth: {note}')
    if bt_result['action'] == 'patch':
        brcm_paths = _fetch_brcm_kexts(bt_result['kexts'], workdir)
        for name, path in brcm_paths.items():
            shutil.copytree(path, os.path.join(kexts_dir, f'{name}.kext'), dirs_exist_ok=True)
            kext_add.append(_kext_entry(name, executable=name not in BRCM_DATA_ONLY_KEXTS))

    wifi_controllers = hw_detect.get_wifi_controllers()
    wifi_result = wifi_compat.evaluate(wifi_controllers)
    for note in wifi_result['notes']:
        print(f'WiFi: {note}')
    if wifi_result['action'] == 'broadcom_fixup':
        path = _fetch_kext('AirportBrcmFixup', 'acidanthera/AirportBrcmFixup', workdir)
        shutil.copytree(path, os.path.join(kexts_dir, 'AirportBrcmFixup.kext'), dirs_exist_ok=True)
        kext_add.append(_kext_entry('AirportBrcmFixup'))
    elif wifi_result['action'] == 'intel_itlwm':
        path = _fetch_itlwm(workdir)
        shutil.copytree(path, os.path.join(kexts_dir, 'itlwm.kext'), dirs_exist_ok=True)
        kext_add.append(_kext_entry('itlwm'))
        print('WiFi: itlwm.kext staged - install HeliPort.app (github.com/OpenIntelWireless/HeliPort)')
        print('      on macOS to actually connect; it will not appear in the normal WiFi menu.')

    config['Kernel']['Add'] = kext_add

    boot_args = ['-v']  # verbose boot for every first attempt - makes failures diagnosable
    if no_acceleration:
        if vendor == 'NVIDIA':
            boot_args.append('nv_disable=1')
        elif vendor == 'AMD':
            boot_args.append('-radvesa')
        elif vendor == 'Intel':
            boot_args.append('-igfxvesa')
        print('NOTE: "no acceleration" mode gives you a boot/console picture at best on many setups.')
        print('      If you have a working Intel iGPU, wire your monitor to it instead for a usable desktop.')

    nvram_add = config.setdefault('NVRAM', {}).setdefault('Add', {})
    nvram_add.setdefault('7C436110-AB2A-4BBB-A880-FE41995C9F82', {})['boot-args'] = ' '.join(boot_args)
    if bt_result['nvram_vars']:
        nvram_add['7C436110-AB2A-4BBB-A880-FE41995C9F82'].update(bt_result['nvram_vars'])

    config.setdefault('Misc', {}).setdefault('Security', {})['SecureBootModel'] = 'Disabled'

    # Safe baseline until the real per-port map (usb_map.py) is run: raises the
    # firmware's 15-port limit without needing to know the real port layout.
    config.setdefault('Kernel', {}).setdefault('Quirks', {})['XhciPortLimit'] = True

    # Same controller drives both ROM (via real_mac=) and the built-in
    # DeviceProperties patch below - they must agree, or en0 won't end up
    # matching the MAC baked into ROM. See imessage.py.
    builtin_controller = imessage.find_builtin_candidate()
    identity = smbios.generate(smbios_model, os.path.join(workdir, 'smbios'), oc_extract_dir=oc_extract,
                                real_mac=(builtin_controller or {}).get('mac'))
    smbios.apply_to_config(config, identity)
    print(f'Generated SMBIOS identity for {smbios_model}: serial {identity["SystemSerialNumber"]}')
    imessage.apply_builtin_property(config, builtin_controller)
    imessage.print_checklist(identity, builtin_controller)

    config_path = os.path.join(oc_dir, 'config.plist')
    with open(config_path, 'wb') as f:
        plistlib.dump(config, f)

    print(f'EFI folder written to {efi_dest}')
    print(f'Target macOS: {macos_version["name"]} ({macos_version["version"]})')

    problems = verify_efi(efi_dest)
    if problems:
        print('')
        print('!! EFI sanity check found problems - fix these before rebooting into this drive:')
        for p in problems:
            print(f'   - {p}')
    else:
        print('EFI sanity check passed: bootloader, config.plist, and all kexts are in place.')

    print('Recommended follow-up steps (hackintosh_setup.py can run these for you):')
    print('  - acpi_patches.py: dump this machine\'s real ACPI tables and generate SSDT-PLUG/EC/AWAC/etc.')
    print('  - usb_map.py: map real USB ports and replace the XhciPortLimit quirk with a precise map')
    if enable_cpufriend:
        print('  - cpufriend.py: AFTER you successfully boot this drive into macOS, generate')
        print('    CPUFriendDataProvider.kext (CPUFriend.kext alone does nothing until then)')
    return efi_dest


def verify_efi(efi_dest):
    """
    Basic structural sanity check on a built EFI folder - catches an
    interrupted download, a missing kext, or a malformed config.plist before
    you find out the hard way at boot. Returns a list of problem strings
    (empty if everything looks fine).
    """
    problems = []

    def require(rel_path, kind='file'):
        full = os.path.join(efi_dest, rel_path)
        exists = os.path.isdir(full) if kind == 'dir' else os.path.isfile(full)
        if not exists:
            problems.append(f'Missing {rel_path}')
        return exists

    require(os.path.join('BOOT', 'BOOTx64.efi'))
    require(os.path.join('OC', 'OpenCore.efi'))
    if not require(os.path.join('OC', 'config.plist')):
        return problems  # nothing further to check without a config

    config_path = os.path.join(efi_dest, 'OC', 'config.plist')
    try:
        with open(config_path, 'rb') as f:
            config = plistlib.load(f)
    except Exception as e:
        problems.append(f'config.plist is not a valid plist: {e}')
        return problems

    kext_add = config.get('Kernel', {}).get('Add', [])
    if not kext_add:
        problems.append('config.plist has no Kernel -> Add entries')
    elif kext_add[0].get('BundlePath') != 'Lilu.kext':
        problems.append('Lilu.kext must be the first entry in Kernel -> Add (plugins depend on it loading first)')

    for entry in kext_add:
        bundle = entry.get('BundlePath', '')
        if not os.path.isdir(os.path.join(efi_dest, 'OC', 'Kexts', bundle)):
            problems.append(f'config.plist references {bundle} but it is not in OC/Kexts')

    platform_info = config.get('PlatformInfo', {}).get('Generic', {})
    if not platform_info.get('SystemSerialNumber'):
        problems.append('No SystemSerialNumber set in PlatformInfo (SMBIOS identity missing)')

    return problems


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print(f'Usage: {sys.argv[0]} <efi_mount_path>')
        sys.exit(1)
    import macrecovery
    build_efi(sys.argv[1], macrecovery.MACOS_VERSIONS[-1], hw_detect.get_gpus(), False, os.path.join(os.getcwd(), 'work'))
