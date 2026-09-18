#!/usr/bin/env python3
"""
Cross-platform hardware inventory (CPU/GPU/network/storage/etc). Returns
plain dicts so callers (hardware_report.py, imessage.py, usb_map.py, ...)
can stay OS-agnostic. Every function degrades to an empty/"unknown" result
instead of raising, so a missing tool on a stripped-down system never
crashes the wizard.
"""

import ctypes
import platform
import re
import subprocess


def host_os():
    system = platform.system()
    if system == 'Darwin':
        return 'macos'
    if system == 'Windows':
        return 'windows'
    if system == 'Linux':
        return 'linux'
    raise RuntimeError(f'Unsupported host OS: {system}')


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ''


def _has_avx2_windows():
    try:
        kernel32 = ctypes.windll.kernel32
        PF_AVX2_INSTRUCTIONS_AVAILABLE = 17
        return bool(kernel32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE))
    except Exception:
        return True  # assume modern CPU rather than block the flow


def get_cpu_info():
    """Returns {'brand': str, 'avx2': bool}."""
    osname = host_os()
    brand = 'Unknown CPU'
    avx2 = True

    if osname == 'macos':
        brand = _run(['sysctl', '-n', 'machdep.cpu.brand_string']).strip() or brand
        leaf7 = _run(['sysctl', '-n', 'machdep.cpu.leaf7_features'])
        avx2 = 'AVX2' in leaf7
    elif osname == 'linux':
        cpuinfo = _run(['cat', '/proc/cpuinfo'])
        m = re.search(r'model name\s*:\s*(.+)', cpuinfo)
        if m:
            brand = m.group(1).strip()
        avx2 = bool(re.search(r'\bavx2\b', cpuinfo))
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command', '(Get-CimInstance Win32_Processor).Name'])
        if out.strip():
            brand = out.strip()
        avx2 = _has_avx2_windows()

    return {'brand': brand, 'avx2': avx2, 'low_end': any(x in brand for x in ('Celeron', 'Pentium'))}


def _parse_ven_dev(text):
    """Extract (vendor_id, device_id) 4-hex-digit pairs from a Windows PNPDeviceID/InstanceId
    or lspci-style text. Matches PCI's "VEN_xxxx&DEV_yyyy" AND USB's "VID_xxxx&PID_yyyy" -
    Microsoft's own two separate, invariant device-ID-string conventions (see "Device
    Identification Strings" docs) - not just the PCI one. Confirmed live-shaped: this used to
    only recognize VEN_/DEV_, so get_bluetooth_controllers()'s Windows branch (InstanceId from
    `Get-PnpDevice -Class Bluetooth`) silently returned nothing for every USB-attached Bluetooth
    adapter - the overwhelming majority of them - since a real one's InstanceId reads
    "USB\\VID_xxxx&PID_yyyy\\..." with no "VEN_"/"DEV_" or bare ":" anywhere in it at all.
    """
    m = re.search(r'(?:VEN_|VID_|:)([0-9A-Fa-f]{4}).*?(?:DEV_|PID_|:)([0-9A-Fa-f]{4})', text)
    if m:
        return m.group(1).lower(), m.group(2).lower()
    return None, None


VENDOR_NAMES = {'10de': 'NVIDIA', '1002': 'AMD', '8086': 'Intel'}


def get_gpus():
    """Returns a list of {'name', 'vendor', 'vendor_id', 'device_id'}."""
    osname = host_os()
    gpus = []

    if osname == 'macos':
        out = _run(['system_profiler', 'SPDisplaysDataType'])
        blocks = re.split(r'\n(?=\s{4}\S)', out)
        for block in blocks:
            name_m = re.search(r'^\s{4}(.+):\s*$', block, re.MULTILINE)
            vend_m = re.search(r'Vendor(?: ID)?:\s*(?:\S+\s*)?\(?0x([0-9a-fA-F]{1,4})\)?', block)
            dev_m = re.search(r'Device ID:\s*0x([0-9a-fA-F]{1,4})', block)
            if name_m and vend_m and dev_m:
                vendor_id = vend_m.group(1).lower().zfill(4)
                chipset_m = re.search(r'Chipset Model:\s*(.+)', block)
                display_name = chipset_m.group(1).strip() if chipset_m else name_m.group(1).strip()
                gpus.append({
                    'name': display_name,
                    'vendor': VENDOR_NAMES.get(vendor_id, vendor_id),
                    'vendor_id': vendor_id,
                    'device_id': dev_m.group(1).lower().zfill(4),
                })
    elif osname == 'linux':
        out = _run(['lspci', '-nn'])
        for line in out.splitlines():
            if re.search(r'\b(VGA compatible controller|3D controller|Display controller)\b', line):
                ids_m = re.search(r'\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]', line)
                # Anchor on the closing "]:" of the class-code bracket (e.g. "[0300]:"),
                # not a bare ":" - lspci lines start with "BB:DD.F ..." which has its
                # own colon and would otherwise wrongly split the name there.
                name_m = re.search(r'\]:\s*(.+?)\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]', line)
                if ids_m:
                    vendor_id = ids_m.group(1).lower()
                    gpus.append({
                        'name': (name_m.group(1) if name_m else line).strip(),
                        'vendor': VENDOR_NAMES.get(vendor_id, vendor_id),
                        'vendor_id': vendor_id,
                        'device_id': ids_m.group(2).lower(),
                    })
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '||' + $_.PNPDeviceID }"])
        for line in out.splitlines():
            if '||' not in line:
                continue
            name, pnp = line.split('||', 1)
            vendor_id, device_id = _parse_ven_dev(pnp)
            if vendor_id:
                gpus.append({
                    'name': name.strip(),
                    'vendor': VENDOR_NAMES.get(vendor_id, vendor_id),
                    'vendor_id': vendor_id,
                    'device_id': device_id,
                })

    return gpus


def get_ethernet_controllers():
    """
    Returns a list of {'name', 'vendor_id', 'device_id', 'mac', 'pci_path'}.
    'pci_path' is an OpenCore-style "PciRoot(0x0)/Pci(0xDD,0xFF)" device path
    where it can be determined cheaply (Linux via sysfs; Windows via WMI
    bus/address properties) - None otherwise. Only bus-0 devices resolve a
    path (the overwhelming common case for an onboard/chipset NIC); anything
    behind a secondary bridge is left for manual Hackintool export, same as
    upstream guides recommend.
    """
    osname = host_os()
    controllers = []

    if osname == 'macos':
        out = _run(['system_profiler', 'SPEthernetDataType'])
        blocks = re.split(r'\n(?=\s{4}\S)', out)
        for block in blocks:
            vend_m = re.search(r'Vendor ID:\s*0x([0-9a-fA-F]{1,4})', block)
            dev_m = re.search(r'Device ID:\s*0x([0-9a-fA-F]{1,4})', block)
            mac_m = re.search(r'MAC Address:\s*([0-9a-fA-F:]{17})', block)
            name_m = re.search(r'^\s{4}(.+):\s*$', block, re.MULTILINE)
            if vend_m and mac_m:
                controllers.append({
                    'name': name_m.group(1).strip() if name_m else 'Ethernet Controller',
                    'vendor_id': vend_m.group(1).lower().zfill(4),
                    'device_id': dev_m.group(1).lower().zfill(4) if dev_m else None,
                    'mac': mac_m.group(1).lower(),
                    'pci_path': None,  # no cheap, reliable B/D/F source on macOS - see docstring
                })
    elif osname == 'linux':
        out = _run(['lspci', '-Dnn'])
        for line in out.splitlines():
            if 'Ethernet controller' not in line:
                continue
            bdf_m = re.match(r'([0-9a-fA-F]{4}:[0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-9a-fA-F])', line)
            ids_m = re.search(r'\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]', line)
            name_m = re.search(r'\]:\s*(.+?)\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]', line)
            if not (bdf_m and ids_m):
                continue
            domain_bus, device, function = bdf_m.groups()
            bus = domain_bus.split(':')[1]
            pci_path = None
            if bus == '00':
                pci_path = f'PciRoot(0x0)/Pci(0x{int(device, 16):x},0x{int(function, 16):x})'
            iface_mac = None
            net_dir = f'/sys/bus/pci/devices/{domain_bus}:{device}.{function}/net'
            try:
                import os as _os
                ifaces = _os.listdir(net_dir)
                if ifaces:
                    iface_mac = _read_sysfs_addr(f'{net_dir}/{ifaces[0]}/address')
            except OSError:
                pass
            controllers.append({
                'name': (name_m.group(1) if name_m else line).strip(),
                'vendor_id': ids_m.group(1).lower(),
                'device_id': ids_m.group(2).lower(),
                'mac': iface_mac,
                'pci_path': pci_path,
            })
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     "Get-CimInstance Win32_NetworkAdapter | Where-Object { $_.PNPDeviceID -like 'PCI*' -and $_.PhysicalAdapter -eq $true -and $_.Name -notmatch 'Wireless|Wi-Fi|WiFi|802.11' } "
                     "| ForEach-Object { "
                     "$bus = (Get-PnpDeviceProperty -InstanceId $_.PNPDeviceID -KeyName DEVPKEY_Device_BusNumber -ErrorAction SilentlyContinue).Data; "
                     "$addr = (Get-PnpDeviceProperty -InstanceId $_.PNPDeviceID -KeyName DEVPKEY_Device_Address -ErrorAction SilentlyContinue).Data; "
                     "$_.Name + '||' + $_.PNPDeviceID + '||' + $_.MACAddress + '||' + $bus + '||' + $addr }"])
        for line in out.splitlines():
            parts = line.split('||')
            if len(parts) != 5:
                continue
            name, pnp, mac, bus, addr = parts
            vendor_id, device_id = _parse_ven_dev(pnp)
            if not vendor_id:
                continue
            pci_path = None
            if bus.strip() == '0' and addr.strip().isdigit():
                addr_int = int(addr.strip())
                device, function = (addr_int >> 16) & 0xFFFF, addr_int & 0xFFFF
                pci_path = f'PciRoot(0x0)/Pci(0x{device:x},0x{function:x})'
            controllers.append({
                'name': name.strip(),
                'vendor_id': vendor_id,
                'device_id': device_id,
                'mac': mac.strip().lower().replace('-', ':') if mac.strip() else None,
                'pci_path': pci_path,
            })

    return controllers


def _read_sysfs_addr(path):
    try:
        with open(path) as f:
            return f.read().strip().lower()
    except OSError:
        return None


def get_primary_mac():
    """
    Best real MAC address to base the SMBIOS ROM value on - a genuine
    physical Ethernet adapter's MAC, per the community-standard iMessage
    activation method (see imessage.py). Returns None if no wired controller
    with a readable MAC was found; callers should fall back to a synthetic
    Apple-OUI-prefixed MAC in that case rather than leaving ROM unset.
    """
    for controller in get_ethernet_controllers():
        if controller.get('mac'):
            return controller['mac']
    return None


def get_wifi_controllers():
    """
    Returns a list of {'name', 'vendor_id', 'device_id'} for PCI/PCIe WiFi
    controllers. Same caveat as get_bluetooth_controllers(): on macOS this
    only reflects what's already recognized by some driver - a card with no
    working kext at all may not show up in system_profiler. Check Linux
    `lspci` or Windows Device Manager first if this comes back empty but you
    know there's a card physically present.
    """
    osname = host_os()
    wifis = []

    if osname == 'macos':
        out = _run(['system_profiler', 'SPAirPortDataType'])
        # Apple's standard display format: "Card Type: AirPort Extreme  (0x14E4, 0x4364)"
        m = re.search(r'Card Type:\s*(.+?)\s*\(0x([0-9A-Fa-f]+),\s*0x([0-9A-Fa-f]+)\)', out)
        if m:
            wifis.append({
                'name': m.group(1).strip(),
                'vendor_id': m.group(2).lower().zfill(4),
                'device_id': m.group(3).lower().zfill(4),
            })
    elif osname == 'linux':
        out = _run(['lspci', '-nn'])
        for line in out.splitlines():
            if 'Network controller' not in line:
                continue
            ids_m = re.search(r'\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]', line)
            name_m = re.search(r'\]:\s*(.+?)\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]', line)
            if ids_m:
                wifis.append({
                    'name': (name_m.group(1) if name_m else line).strip(),
                    'vendor_id': ids_m.group(1).lower(),
                    'device_id': ids_m.group(2).lower(),
                })
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     "Get-CimInstance Win32_NetworkAdapter | Where-Object { $_.PNPDeviceID -like 'PCI*' -and ($_.Name -match 'Wireless|Wi-Fi|WiFi|802.11') } "
                     "| ForEach-Object { $_.Name + '||' + $_.PNPDeviceID }"])
        for line in out.splitlines():
            if '||' not in line:
                continue
            name, pnp = line.split('||', 1)
            vendor_id, device_id = _parse_ven_dev(pnp)
            if vendor_id:
                wifis.append({
                    'name': name.strip(),
                    'vendor_id': vendor_id,
                    'device_id': device_id,
                })

    return wifis


def get_bluetooth_controllers():
    """
    Returns a list of {'name', 'vendor_id', 'device_id', 'transport'}.
    vendor_id/device_id are 4-hex-digit USB IDs where known (None for a
    genuine Apple internal module, which reports over UART with no USB
    VID:PID at all, vendor_id '004c' being Apple's own - see
    hardware_report.py's gather_bluetooth(), which skips those entries
    since they need no USB-style ID reported).

    Caveat inherited from the USB-mapping module: on macOS, this only sees
    what the OS has *already* matched to some Bluetooth-aware driver path -
    a totally unrecognized third-party USB dongle with zero kexts loaded may
    not show up at all. If it comes up empty, check Windows Device Manager
    or Linux `lsusb` instead to identify the chipset before ever touching
    macOS.
    """
    osname = host_os()
    controllers = []

    if osname == 'macos':
        out = _run(['system_profiler', 'SPBluetoothDataType'])
        blocks = re.split(r'\n(?=\s{6}\S)', out)
        for block in blocks:
            vend_m = re.search(r'Vendor ID:\s*0x([0-9a-fA-F]{1,4})', block)
            chip_m = re.search(r'Chipset:\s*(.+)', block)
            transport_m = re.search(r'Transport:\s*(.+)', block)
            if vend_m:
                vendor_id = vend_m.group(1).lower().zfill(4)
                controllers.append({
                    'name': chip_m.group(1).strip() if chip_m else 'Bluetooth Controller',
                    'vendor_id': vendor_id,
                    'device_id': None,
                    'transport': transport_m.group(1).strip() if transport_m else 'Unknown',
                })
    elif osname == 'linux':
        out = _run(['lsusb', '-nn'])
        for line in out.splitlines():
            if 'bluetooth' not in line.lower():
                continue
            ids_m = re.search(r'\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]', line)
            name_m = re.search(r'\]:\s*(.+?)\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]', line)
            if ids_m:
                controllers.append({
                    'name': (name_m.group(1) if name_m else line).strip(),
                    'vendor_id': ids_m.group(1).lower(),
                    'device_id': ids_m.group(2).lower(),
                    'transport': 'USB',
                })
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     "Get-PnpDevice -Class Bluetooth -Status OK | ForEach-Object { $_.FriendlyName + '||' + $_.InstanceId }"])
        for line in out.splitlines():
            if '||' not in line:
                continue
            name, instance_id = line.split('||', 1)
            vendor_id, device_id = _parse_ven_dev(instance_id)
            if vendor_id:
                controllers.append({
                    'name': name.strip(),
                    'vendor_id': vendor_id,
                    'device_id': device_id,
                    'transport': 'USB',
                })

    return controllers


def is_laptop():
    """Battery presence is a simpler, more reliable cross-platform proxy for
    "is this a laptop" than model-name string matching (which breaks the
    moment a fake/generic SMBIOS is in play, as it always is pre-mapping)."""
    osname = host_os()
    if osname == 'macos':
        return 'InternalBattery' in _run(['pmset', '-g', 'batt'])
    if osname == 'linux':
        import glob
        return len(glob.glob('/sys/class/power_supply/BAT*')) > 0
    if osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     '(Get-CimInstance Win32_Battery | Measure-Object).Count'])
        return out.strip().isdigit() and int(out.strip()) > 0
    return False


def has_nvme():
    """Whether an NVMe storage controller is present - used to decide
    whether to bundle NVMeFix (fixes power-state/sleep issues some NVMe
    SSDs have under macOS)."""
    osname = host_os()
    if osname == 'macos':
        out = _run(['system_profiler', 'SPNVMeDataType'])
        return bool(out.strip()) and 'Capacity' in out
    if osname == 'linux':
        import glob
        return len(glob.glob('/sys/class/nvme/nvme*')) > 0
    if osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     "(Get-PhysicalDisk | Where-Object BusType -eq 'NVMe' | Measure-Object).Count"])
        return out.strip().isdigit() and int(out.strip()) > 0
    return False


if __name__ == '__main__':
    print('Host OS:', host_os())
    print('CPU:', get_cpu_info())
    print('GPUs:')
    for g in get_gpus():
        print(' -', g)
    print('Laptop:', is_laptop())
    print('NVMe storage present:', has_nvme())
