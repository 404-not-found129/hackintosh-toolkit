#!/usr/bin/env python3
"""
Full precision USB port mapping can't be templated - the only way to know
which physical ports exist, which are internal (Bluetooth/webcam/card
readers), and which are user-facing is to plug a device into each one and
watch it register, live, on this exact machine. For that, this module
fetches and launches USBToolBox (https://github.com/USBToolBox/tool, MIT)
via run_interactive()/apply(), which walks you through it and whose output
this collects into your EFI. USBToolBox only ships Windows/macOS builds.

On Linux, though, there's a real non-interactive option: generate() below
reads this machine's *actual* USB controller/port topology straight from
/sys/bus/usb (a stable, well-documented kernel ABI - controller PCI IDs via
sysfs, port existence via each root hub's real "maxchild" port count, and
which ports are currently populated) and builds a port-personality map
using the same technique USBToolBox itself offers as its "native" mode:
Apple's own AppleUSBHostMergeProperties driver (stock in macOS 11+, no
third-party kext binary needed) matched to your controller's real PCI
vendor/device ID. The exact personality schema below (IOParentMatch,
IOProviderMergeProperties, the "port"/"port-count" little-endian encoding,
UsbConnector codes) is verified against USBToolBox's own base.py/shared.py
source, not guessed.

Two honest limits on the Linux automatic path:
  - It's a single snapshot: ports with nothing plugged in right now still
    get mapped (we know they exist from the controller's real port count),
    but only as a generic type guess - not verified with a real device.
  - Merging the USB2 and USB3 "personalities" of the same physical xHCI
    port (a hard problem USBToolBox itself calls out as Windows-only
    "companion port binding") isn't attempted; ports are taken from
    whichever root hub(s) sysfs exposes for that controller.
For a fully precise map, or when you're not on Linux, use the interactive
USBToolBox path instead - it's still one call to apply() either way.
"""

import glob
import os
import plistlib
import re
import shutil
import subprocess
import sys

import hw_detect
import net

REPO = 'USBToolBox/tool'

# Verified from https://github.com/USBToolBox/tool/blob/master/Scripts/shared.py
USB_TYPE_A = 0
USB3_TYPE_A = 3
USB_INTERNAL = 255


def supported_here():
    return hw_detect.host_os() in ('macos', 'windows')


def fetch_tool(workdir):
    if hw_detect.host_os() == 'windows':
        zip_path = os.path.join(workdir, 'USBToolBox-Windows.zip')
        release = net.api_get(net.GITHUB_API_LATEST_RELEASE.format(repo=REPO))
        asset = next((a for a in release.get('assets', []) if a['name'] == 'Windows.zip'), None)
        if not asset:
            raise RuntimeError('Could not find Windows.zip in USBToolBox releases')
        net.download(asset['browser_download_url'], zip_path)
        return net.extract_zip(zip_path, os.path.join(workdir, 'USBToolBox'))
    return net.fetch_repo_source_zip(REPO, workdir)


def run_interactive(tool_dir):
    osname = hw_detect.host_os()
    entry = 'Windows.py' if osname == 'windows' else 'macOS.py'
    script = os.path.join(tool_dir, entry)
    if not os.path.exists(script):
        # Windows.zip ships a standalone Windows.exe instead of source
        exe = os.path.join(tool_dir, 'Windows.exe')
        if os.path.exists(exe):
            print()
            print('Handing off to USBToolBox - plug a device into each physical port when')
            print('prompted, then use its menu to build the kext when done.')
            print()
            subprocess.run([exe], cwd=tool_dir)
            return
        raise RuntimeError(f'Could not find {entry} or Windows.exe in {tool_dir}')

    if osname == 'macos':
        req = os.path.join(tool_dir, 'requirements.txt')
        if os.path.exists(req):
            print('Installing USBToolBox Python dependencies (pyobjc etc.)...')
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', req], cwd=tool_dir)

    print()
    print('Handing off to USBToolBox - plug a device into each physical port when')
    print('prompted (a USB2 device AND a USB3 device for each USB3 port on macOS),')
    print('then use its menu to build the kext when done.')
    print()
    subprocess.run([sys.executable, entry], cwd=tool_dir)


def _read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


_PCI_ADDR_RE = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$')


def _pci_address_for_usb_root(root_hub_syspath):
    real = os.path.realpath(root_hub_syspath)
    matches = [p for p in real.split(os.sep) if _PCI_ADDR_RE.match(p)]
    return matches[-1] if matches else None


def enumerate_linux_controllers():
    """
    Real, live enumeration via /sys/bus/usb (no third-party tool). Returns
    {pci_address: {'vendor': '8086', 'device': 'a12f',
                    'port_count': int, 'ports': {index: {'superspeed': bool}}}}
    - only ports sysfs reports as directly on the root hub (single "N-M"
    path segment) are included; devices behind an external hub are ignored,
    since those aren't physical motherboard ports.
    """
    controllers = {}
    for root_hub in sorted(glob.glob('/sys/bus/usb/devices/usb*')):
        bus_num = _read_sysfs(os.path.join(root_hub, 'busnum'))
        maxchild = _read_sysfs(os.path.join(root_hub, 'maxchild'))
        bcd_usb = _read_sysfs(os.path.join(root_hub, 'version'))
        if not bus_num or not maxchild:
            continue
        pci_addr = _pci_address_for_usb_root(root_hub)
        if not pci_addr:
            continue
        vendor = _read_sysfs(f'/sys/bus/pci/devices/{pci_addr}/vendor')
        device = _read_sysfs(f'/sys/bus/pci/devices/{pci_addr}/device')
        if not vendor or not device:
            continue
        vendor = vendor.replace('0x', '').zfill(4)
        device = device.replace('0x', '').zfill(4)
        is_superspeed_bus = bool(bcd_usb) and float(bcd_usb) >= 3.0

        entry = controllers.setdefault(pci_addr, {
            'vendor': vendor, 'device': device, 'port_count': 0, 'ports': {}
        })
        entry['port_count'] = max(entry['port_count'], int(maxchild))

        for dev_dir in glob.glob(f'/sys/bus/usb/devices/{bus_num}-*'):
            suffix = os.path.basename(dev_dir).split('-', 1)[1]
            if '.' in suffix or ':' in suffix or not suffix.isdigit():
                continue  # behind an external hub, or an interface node - not a root port
            port_index = int(suffix)
            state = entry['ports'].setdefault(port_index, {'superspeed': False, 'populated': False})
            state['populated'] = True
            state['superspeed'] = state['superspeed'] or is_superspeed_bus

    return controllers


def _little_endian_u32(value):
    return value.to_bytes(4, 'little')


def build_native_personalities(controllers, smbios_model):
    """Builds the IOKitPersonalities dict for a data-only USBMap.kext using
    Apple's stock AppleUSBHostMergeProperties driver - schema verified
    against USBToolBox/tool's base.py build_kext()."""
    personalities = {}
    for i, (pci_addr, info) in enumerate(sorted(controllers.items())):
        name = 'XHC' if i == 0 else f'XHC{i}'
        ports = {}
        for index in range(1, info['port_count'] + 1):
            state = info['ports'].get(index, {'superspeed': True, 'populated': False})
            prefix = 'SS' if state['superspeed'] else 'HS'
            connector = USB3_TYPE_A if state['superspeed'] else USB_TYPE_A
            ports[f'{prefix}{index:02d}'] = {
                'port': _little_endian_u32(index),
                'UsbConnector': connector,
            }
        personalities[name] = {
            'CFBundleIdentifier': 'com.apple.driver.AppleUSBHostMergeProperties',
            'IOClass': 'AppleUSBHostMergeProperties',
            'IOProviderClass': 'AppleUSBHostController',
            'IOParentMatch': {'IOPCIPrimaryMatch': f'0x{info["device"]}{info["vendor"]}'},
            'model': smbios_model,
            'IOProviderMergeProperties': {
                'ports': ports,
                'port-count': _little_endian_u32(info['port_count']),
            },
        }
    return personalities


def write_native_kext(oc_dir, personalities):
    """Writes a data-only (no compiled binary) USBMap.kext, matching the
    real Info.plist template USBToolBox itself uses for this method."""
    kext_dir = os.path.join(oc_dir, 'Kexts', 'USBMap.kext', 'Contents')
    os.makedirs(kext_dir, exist_ok=True)
    info_plist = {
        'CFBundleDevelopmentRegion': 'English',
        'CFBundleGetInfoString': 'v1.1',
        'CFBundleIdentifier': 'com.hackintosh-toolkit.USBMap',
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'USBMap',
        'CFBundlePackageType': 'KEXT',
        'CFBundleShortVersionString': '1.1',
        'CFBundleSignature': '????',
        'CFBundleVersion': '1.1',
        'IOKitPersonalities': personalities,
        'OSBundleRequired': 'Root',
    }
    with open(os.path.join(kext_dir, 'Info.plist'), 'wb') as f:
        plistlib.dump(info_plist, f)
    return 'USBMap.kext'


def generate_linux(efi_dest, smbios_model):
    """Non-interactive: enumerates this machine's real USB topology via
    sysfs and writes a USBMap.kext using Apple's stock native driver.
    Returns the kext bundle name, or None if no controllers were found."""
    controllers = enumerate_linux_controllers()
    if not controllers:
        print('No USB controllers found under /sys/bus/usb - are you running as root, and is this the target machine?')
        return None

    total_ports = sum(c['port_count'] for c in controllers.values())
    populated = sum(1 for c in controllers.values() for p in c['ports'].values() if p['populated'])
    print(f'Found {len(controllers)} USB controller(s), {total_ports} total ports ({populated} currently populated).')

    personalities = build_native_personalities(controllers, smbios_model)
    oc_dir = os.path.join(efi_dest, 'OC')
    kext_name = write_native_kext(oc_dir, personalities)
    register_in_config(os.path.join(oc_dir, 'config.plist'), kext_name)
    print(f'Wrote {kext_name} and registered it in config.plist; disabled the XhciPortLimit fallback.')
    print('This is a one-shot snapshot, not a verified per-port walk - for full precision, also run')
    print('the interactive USBToolBox path (from Windows or this drive\'s macOS Base System).')
    return kext_name


def collect_kext(tool_dir, kexts_dir):
    """USBToolBox writes its built kext somewhere under the tool directory
    (naming/location has varied by version) - search for the first *.kext
    produced and copy it in."""
    kext_path = net.find_first(tool_dir, lambda p: p.endswith('.kext') and os.path.isdir(p))
    if not kext_path:
        print('No .kext found - did you press "Build" before exiting USBToolBox?')
        return None
    dest = os.path.join(kexts_dir, os.path.basename(kext_path))
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(kext_path, dest)
    return os.path.basename(dest)


def register_in_config(config_path, kext_bundle_name):
    with open(config_path, 'rb') as f:
        config = plistlib.load(f)

    kext_add = config.setdefault('Kernel', {}).setdefault('Add', [])
    if not any(entry.get('BundlePath') == kext_bundle_name for entry in kext_add):
        kext_add.append({
            'Arch': 'Any',
            'BundlePath': kext_bundle_name,
            'Comment': 'USB port map (usb_map.py)',
            'Enabled': True,
            'ExecutablePath': '',
            'MaxKernel': '',
            'MinKernel': '',
            'PlistPath': 'Contents/Info.plist',
        })

    # A real port map replaces the need for the blunt firmware-limit quirk.
    config.setdefault('Kernel', {}).setdefault('Quirks', {})['XhciPortLimit'] = False

    with open(config_path, 'wb') as f:
        plistlib.dump(config, f)


def apply(efi_dest, workdir, smbios_model='iMac19,1'):
    """
    efi_dest: the 'EFI' folder produced by opencore_build.build_efi().
    On Linux, generates a real port map automatically from sysfs (see
    generate_linux()). On Windows/macOS, hands off to the real USBToolBox
    tool interactively - the better-supported path on those platforms.
    Returns the kext bundle name added, or None if mapping wasn't completed.
    """
    if hw_detect.host_os() == 'linux':
        return generate_linux(efi_dest, smbios_model)

    if not supported_here():
        print('No automatic path for this OS and USBToolBox has no Linux build either. '
              'Leaving the XhciPortLimit quirk enabled as a fallback.')
        return None

    oc_dir = os.path.join(efi_dest, 'OC')
    kexts_dir = os.path.join(oc_dir, 'Kexts')
    os.makedirs(kexts_dir, exist_ok=True)

    tool_dir = fetch_tool(workdir)
    run_interactive(tool_dir)
    kext_name = collect_kext(tool_dir, kexts_dir)
    if kext_name:
        register_in_config(os.path.join(oc_dir, 'config.plist'), kext_name)
        print(f'Registered {kext_name} in config.plist and disabled the XhciPortLimit fallback.')
    return kext_name


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} <path to EFI/EFI folder produced by opencore_build.py>')
        sys.exit(1)
    apply(sys.argv[1], os.path.join(os.getcwd(), 'usb_work'))
