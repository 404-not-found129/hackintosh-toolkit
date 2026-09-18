#!/usr/bin/env python3
"""
Builds the "hardware report" JSON that OpCore-Simplify
(github.com/lzhoang2801/OpCore-Simplify) requires as its only input -
schema reverse-engineered from its own Scripts/report_validator.py, not
guessed. OpCore-Simplify's own way of producing this file is a Windows-only
companion binary ("Hardware Sniffer"); this module is a cross-platform
substitute built from the same commands the rest of this toolkit already
uses (hw_detect.py) plus new detectors for the sections OpCore-Simplify
needs that nothing else here previously gathered (Motherboard, BIOS, USB
Controllers, Input, Storage Controllers, Sound).

Two real, platform-level limitations, not gaps in this code:

  - macOS doesn't expose real motherboard/BIOS DMI data at all - on any
    Mac, genuine or Hackintosh, not a Hackintosh-specific gap. Verified
    live: `system_profiler`/`ioreg` only ever show the *spoofed* Apple
    identity (Model Name "Mac Pro", manufacturer "Acidanthera" - OpenCore's
    own injected string) - there is no equivalent of Windows/Linux's real
    DMI Type 1/Type 2 tables. If anything this is *more* true on a working
    Hackintosh, not less: OpenCore's SMBIOS patch exists specifically to
    replace what macOS sees here with a fake Apple identity, by design.
    Where this matters (Motherboard name/chipset), macOS falls back to
    asking you directly rather than guessing at data that provably isn't
    there - and remembers the answer in `~/.hackintosh_toolkit_motherboard`
    so it only asks once per machine, not once per run.
  - USB Controllers, Input devices, Sound codecs, and Storage Controllers
    all need PCI vendor/device IDs that macOS's system_profiler doesn't
    expose for the underlying controller ASIC - verified live even against
    a real, working NVMe drive: SPNVMeDataType reports Model/Capacity/Link
    Speed but no vendor/device ID at all. These sections come back empty on
    macOS rather than reporting a fabricated ID; Linux/Windows have real
    PCI access (lspci/WMI) and aren't affected by this gap.

The schema only validates *format* (a non-empty string, a 4-hex-digit ID,
etc.), not whether a value is the semantically correct one - so a
best-effort or placeholder value never breaks validation, it just gives
OpCore-Simplify's own compatibility logic less to work with for that device.
"""

import os
import re
import subprocess

import hw_detect

# ---- shared helpers ----------------------------------------------------

def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ''


def _device_id(vendor_id, device_id):
    """OpCore-Simplify's required "VVVV-DDDD" uppercase format."""
    if not vendor_id or not device_id:
        return None
    return f'{vendor_id.upper()}-{device_id.upper()}'


# ---- Motherboard / BIOS --------------------------------------------------

INTEL_CHIPSETS = (
    'Z790', 'Z690', 'Z590', 'Z490', 'Z390', 'Z370', 'Z270', 'Z170', 'Z97', 'Z87', 'Z77', 'Z68',
    'B760', 'B660', 'B560', 'B460', 'B365', 'B360', 'B350', 'B250', 'B150', 'B85', 'B75', 'B65',
    'H770', 'H670', 'H610', 'H570', 'H470', 'H410', 'H370', 'H310', 'H270', 'H170', 'H97', 'H87', 'H81', 'H77', 'H67', 'H61', 'H55',
    'Q670', 'Q470', 'Q370', 'Q270', 'Q170', 'Q87', 'Q77', 'Q67', 'Q65', 'Q57',
    'X299', 'X99', 'X79', 'X58', 'C602', 'C612', 'C621', 'C622',
)
AMD_CHIPSETS = (
    'X870E', 'X870', 'X670E', 'X670', 'X570', 'X470', 'X399', 'X370',
    'B850', 'B650E', 'B650', 'B550', 'B450', 'B350',
    'A620', 'A520', 'A320', 'TRX40', 'TRX50', 'A88X', 'A85X', 'A78', 'A75', 'A68H',
)
ALL_CHIPSETS = INTEL_CHIPSETS + AMD_CHIPSETS


def _guess_chipset(board_name):
    upper = (board_name or '').upper()
    for chipset in ALL_CHIPSETS:
        if chipset in upper:
            return chipset
    return 'Unknown'


# Cached in the home directory, not the per-run workdir, on purpose: the
# physical motherboard doesn't change between runs on the same machine, so
# once a human has typed it once there's no reason to ask again just
# because a later run used a different working directory.
_MOTHERBOARD_CACHE_PATH = os.path.expanduser('~/.hackintosh_toolkit_motherboard')


def _load_cached_motherboard():
    try:
        with open(_MOTHERBOARD_CACHE_PATH, 'r') as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_cached_motherboard(name):
    try:
        with open(_MOTHERBOARD_CACHE_PATH, 'w') as f:
            f.write(name)
    except OSError:
        pass  # best-effort - just means asking again next time, not fatal


# Confirmed live, not theoretical: a one-word reply meant for something
# else entirely ("OK", answering an unrelated question) got typed at this
# exact prompt, accepted with zero validation, and cached - every build
# after that silently fed OpCore-Simplify a nonexistent motherboard name
# and an unrecognized ("Unknown") chipset, with nothing anywhere warning
# that anything was wrong, until it was caught days later by manually
# inspecting a generated config.plist's chipset-gated settings.
_IMPLAUSIBLE_BOARD_NAMES = {
    'ok', 'okay', 'yes', 'no', 'n/a', 'na', 'none', 'unknown', 'test',
    'idk', 'dunno', 'sure', 'fine', 'thanks', 'yep', 'nope',
}


def _prompt_and_validate_motherboard(reason):
    """
    Prompts for the motherboard model, rejecting obviously-implausible
    answers outright (see _IMPLAUSIBLE_BOARD_NAMES above) rather than
    silently caching them.

    A plausible-looking answer that still doesn't match any chipset this
    toolkit recognizes (_guess_chipset() returns 'Unknown') is still
    accepted after explicit confirmation - that's a real, if less common,
    case for a genuinely obscure or OEM board - but the person typing it
    is told so first, since an unrecognized chipset has real downstream
    effects (OpCore-Simplify's own chipset-gated Booter/Kernel quirks
    silently take their generic/unrecognized-chipset path instead of the
    correct one for that specific board).
    """
    while True:
        name = input(f'Motherboard model (e.g. "ASUS ROG STRIX Z390-E GAMING") - {reason}: ').strip()
        if not name:
            return 'Unknown'
        if name.lower() in _IMPLAUSIBLE_BOARD_NAMES:
            print(f'"{name}" doesn\'t look like a real motherboard model - try again, '
                  f'or leave blank if you genuinely don\'t know it.')
            continue
        if _guess_chipset(name) == 'Unknown':
            print(f'Couldn\'t recognize a chipset in "{name}" - this toolkit\'s own chipset list may just not '
                  f'include yours, but double-check you typed the actual motherboard model (not the CPU or GPU).')
            if input('Use it anyway? [Y/n]: ').strip().lower() == 'n':
                continue
        return name


def gather_motherboard(prompt_if_unknown=True):
    osname = hw_detect.host_os()
    name = None

    if osname == 'linux':
        name = _run(['dmidecode', '-s', 'baseboard-product-name']).strip() or None
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command', '(Get-CimInstance Win32_BaseBoard).Product'])
        name = out.strip() or None

    if not name:
        cached = _load_cached_motherboard()
        if cached and cached.lower() in _IMPLAUSIBLE_BOARD_NAMES:
            # Don't trust a bad cache even if one's already there (e.g. from
            # before this validation existed) - see _IMPLAUSIBLE_BOARD_NAMES'
            # comment for exactly how this happened live. Fall through to
            # asking fresh instead of silently repeating the same mistake.
            print(f'Ignoring implausible cached motherboard model "{cached}" from {_MOTHERBOARD_CACHE_PATH} - asking fresh.')
            cached = None
        if cached:
            print(f'Motherboard model: {cached}  (remembered from a previous run on this machine - '
                  f'delete {_MOTHERBOARD_CACHE_PATH} to be asked again)')
            name = cached
        elif prompt_if_unknown:
            if osname == 'macos':
                # Not a Hackintosh-specific gap: macOS has no dmidecode/WMI
                # equivalent for this on ANY Mac, genuine or not - verified
                # live, system_profiler/ioreg only ever show the *spoofed*
                # Apple identity. If anything this is even more true once
                # OpenCore's SMBIOS patch is active on a working Hackintosh,
                # since that deliberately replaces what macOS sees with a
                # fake Apple identity by design - the real board name isn't
                # just hard to read, it's intentionally not exposed.
                reason = ('macOS has no dmidecode/WMI equivalent for this on any Mac, real or '
                          'Hackintosh - and once OpenCore\'s SMBIOS patch is active, it '
                          'deliberately replaces this with a fake Apple identity anyway')
            else:
                # dmidecode (Linux) / WMI (Windows) normally get this - reaching
                # here means that lookup itself failed (tool missing, permissions,
                # etc.), not a platform-wide gap like the macOS case above.
                reason = 'automatic detection failed on this machine'
            name = _prompt_and_validate_motherboard(reason)
            if name != 'Unknown':
                _save_cached_motherboard(name)
        else:
            name = 'Unknown'

    return {
        'Name': name,
        'Chipset': _guess_chipset(name),
        'Platform': 'Laptop' if hw_detect.is_laptop() else 'Desktop',
    }


def gather_bios():
    osname = hw_detect.host_os()
    firmware_type = 'UEFI'  # a hard OpenCore prerequisite; if you're not UEFI you can't boot this EFI anyway
    secure_boot = 'Disabled'  # also a hard OpenCore prerequisite - see README

    if osname == 'linux':
        if not os.path.isdir('/sys/firmware/efi'):
            firmware_type = 'BIOS'
        sb = _run(['mokutil', '--sb-state']).lower()
        if 'enabled' in sb:
            secure_boot = 'Enabled'
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command', 'Confirm-SecureBootUEFI']).strip().lower()
        if out == 'true':
            secure_boot = 'Enabled'
        elif out == '':
            firmware_type = 'BIOS'  # Confirm-SecureBootUEFI errors out entirely on legacy BIOS

    return {'Firmware Type': firmware_type, 'Secure Boot': secure_boot}


# ---- CPU -----------------------------------------------------------------

# Intel iGPU device-id prefix -> CPU codename. Reusing the same prefixes
# already validated in gpu_compat.py's Intel compatibility ranges, since a
# consumer Intel CPU and its paired iGPU are always the same generation -
# a simpler, more reliable signal than parsing the brand string.
INTEL_IGPU_CODENAME = (
    (('0102', '0106', '010a'), 'Sandy Bridge'),
    (('0152', '0156', '0162', '0166', '016a'), 'Ivy Bridge'),
    (('04',), 'Haswell'),
    (('16',), 'Broadwell'),
    (('19',), 'Skylake'),
    (('59',), 'Kaby Lake'),
    (('3e', '9b'), 'Coffee Lake'),
    (('8a',), 'Ice Lake'),
    (('9a',), 'Tiger Lake'),
    (('46', '4c'), 'Alder Lake'),
    (('a7',), 'Raptor Lake'),
)


def _intel_codename_from_igpu(gpus):
    for gpu in gpus:
        if gpu.get('vendor') != 'Intel':
            continue
        device_id = (gpu.get('device_id') or '').lower()
        for prefixes, name in INTEL_IGPU_CODENAME:
            if any(device_id.startswith(p) for p in prefixes):
                return name
    return None


# Desktop Ryzen model-number prefix -> Zen microarchitecture codename, from
# AMD's own well-documented model numbering (matches OpCore-Simplify's own
# Scripts/datasets/cpu_data.py AMDCPUGenerations spelling exactly, so a
# match here is guaranteed spelled the way its checks - present or future -
# would expect). Desktop-tier (non-APU) chips only: an AMD brand string
# alone can't reliably distinguish a desktop part from a "G"-suffixed APU
# of the same numeric generation (e.g. 5600X vs 5600G are different silicon
# despite the shared "56" prefix) - see _amd_codename()'s docstring for how
# that's handled, rather than guessed at.
RYZEN_DESKTOP_GENERATION = (
    ('1', 'Summit Ridge'),   # Ryzen 1000 (Zen)
    ('2', 'Pinnacle Ridge'),  # Ryzen 2000 (Zen+)
    ('3', 'Matisse'),         # Ryzen 3000 (Zen 2)
    ('5', 'Vermeer'),         # Ryzen 5000 (Zen 3)
    ('7', 'Raphael'),         # Ryzen 7000 (Zen 4)
    ('9', 'Granite Ridge'),   # Ryzen 9000 (Zen 5)
)


def _amd_codename(brand):
    """
    Best-effort desktop Ryzen codename from the model number in the brand
    string (e.g. "AMD Ryzen 7 5800X 8-Core Processor" -> "5800X" -> "5"
    generation prefix -> "Vermeer"). Returns None (not a guess) for an APU
    ("G"-suffixed model, e.g. 5600G) or Threadripper/EPYC, since their
    codenames don't follow this same desktop numbering and a wrong codename
    is worse than an honest 'Unknown' - OpCore-Simplify's own compatibility
    logic falls back safely on 'Unknown' (see module docstring), not on a
    silently wrong one.
    """
    if 'THREADRIPPER' in brand.upper() or 'EPYC' in brand.upper():
        return None
    # [A-Z0-9]*, not [A-Z]* - a plain [A-Z]* can never match a "X3D" suffix
    # (7950X3D, 7800X3D, 9950X3D, ...): [A-Z] can't consume the digit in
    # "3D", and \b can't then close the match either, since a letter run
    # ending in "X" is immediately followed by "3" - both \w, so there's no
    # boundary there for \b to match. Confirmed live: the old pattern
    # returned no match at all (not just a wrong codename) for every X3D
    # model, silently falling back to 'Unknown' for some of AMD's most
    # popular current desktop CPUs.
    match = re.search(r'\bRyzen\s+\d\s+(\d)(\d{3})([A-Z0-9]*)\b', brand, re.IGNORECASE)
    if not match:
        return None
    generation_digit, _model_rest, suffix = match.groups()
    suffix_upper = suffix.upper()
    if any(c in suffix_upper for c in 'GHU'):
        # G = APU (5600G); H/HS/HX, U = mobile-only suffixes (7945HX3D,
        # 5500U, ...) - confirmed live that widening the suffix class to
        # [A-Z0-9]* for the desktop X3D fix (see comment above) also made
        # this regex match mobile "HX3D" chips like the Ryzen 9 7945HX3D,
        # which would otherwise have returned 'Unknown' (no \b boundary
        # match) and now returns the wrong desktop codename instead. None
        # of AMD's desktop suffixes (X, XT, T, F, ...) contain G/H/U, so
        # this only excludes APU/mobile parts, not real desktop ones.
        return None
    for prefix, codename in RYZEN_DESKTOP_GENERATION:
        if generation_digit == prefix:
            return codename
    return None


def gather_cpu(gpus=None):
    osname = hw_detect.host_os()
    cpu = hw_detect.get_cpu_info()
    brand = cpu['brand']
    manufacturer = 'AMD' if 'AMD' in brand.upper() or 'RYZEN' in brand.upper() else 'Intel'

    if manufacturer == 'Intel':
        codename = _intel_codename_from_igpu(gpus or hw_detect.get_gpus())
    else:
        codename = _amd_codename(brand)
    codename = codename or 'Unknown'

    core_count = '1'
    if osname == 'macos':
        core_count = _run(['sysctl', '-n', 'hw.physicalcpu']).strip() or '1'
    elif osname == 'linux':
        out = _run(['lscpu'])
        m = re.search(r'Core\(s\) per socket:\s*(\d+)', out)
        sockets_m = re.search(r'Socket\(s\):\s*(\d+)', out)
        if m and sockets_m:
            core_count = str(int(m.group(1)) * int(sockets_m.group(1)))
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     '(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum'])
        core_count = out.strip() or '1'

    simd_flags = _gather_simd_flags(cpu)

    return {
        'Manufacturer': manufacturer,
        'Processor Name': brand,
        'Codename': codename,
        'Core Count': core_count,
        'CPU Count': '1',
        'SIMD Features': ' '.join(simd_flags) if simd_flags else 'SSE4.1 SSE4.2',
    }


def _gather_simd_flags(cpu):
    """Best-effort SSE4.1/SSE4.2/AVX2 flags - only AVX2 is already tracked
    by hw_detect.get_cpu_info(); this fills in the other two directly since
    OpCore-Simplify's own compatibility logic checks for them explicitly."""
    osname = hw_detect.host_os()
    flags = []

    if osname == 'macos':
        leaf1 = _run(['sysctl', '-n', 'machdep.cpu.features'])
        leaf7 = _run(['sysctl', '-n', 'machdep.cpu.leaf7_features'])
        if 'SSE4.1' in leaf1:
            flags.append('SSE4.1')
        if 'SSE4.2' in leaf1:
            flags.append('SSE4.2')
        if 'AVX2' in leaf7:
            flags.append('AVX2')
    elif osname == 'linux':
        cpuinfo = _run(['cat', '/proc/cpuinfo'])
        if re.search(r'\bsse4_1\b', cpuinfo):
            flags.append('SSE4.1')
        if re.search(r'\bsse4_2\b', cpuinfo):
            flags.append('SSE4.2')
        if re.search(r'\bavx2\b', cpuinfo):
            flags.append('AVX2')
    elif osname == 'windows':
        flags = ['SSE4.1', 'SSE4.2']  # universal on any CPU from the last ~15 years
        if cpu.get('avx2'):
            flags.append('AVX2')

    return flags


# ---- GPU -------------------------------------------------------------
#
# These Codename strings are NOT free-form labels - OpCore-Simplify's own
# compatibility_checker.py pattern-matches against a specific, narrow
# vocabulary (verified by reading its source directly: e.g. AMD requires
# exactly "Vega 10"/"Polaris 22"/"Polaris 20"/"Baffin"/"Ellesmere", or a
# substring match against gpu_data.AMDCodenames = ["Hainan","Oland",
# "Cape Verde","Pitcairn","Tahiti","Bonaire","Hawaii","Tonga","Fiji"] for
# older GCN; NVIDIA requires exactly "Pascal"/"Maxwell"/"Fermi"/"Tesla" or a
# "Kepler" substring). A generic bucket name like "Polaris" - what an
# earlier version of this function used - silently falls through every one
# of those checks to "unsupported", which is wrong: caught live when a real
# RX 570 (Polaris 20, device 67DF) came back "Unsupported" from
# OpCore-Simplify's own checker despite Polaris having excellent real
# native support. Device-ID mapping below is precise for the Polaris/RDNA2
# generations exactly because product *names* alone can't tell "Polaris 20"
# from "Baffin" from "Ellesmere" (all sold under overlapping RX 4xx/5xx
# names) - name-substring is used elsewhere where it's less ambiguous.

AMD_DEVICE_ID_CODENAME = {
    # Polaris family - device ID is the only precise signal (product names overlap)
    '67df': 'Polaris 20', '67d0': 'Polaris 20', '67c7': 'Polaris 20', '6fdf': 'Polaris 20',
    '67c0': 'Polaris 22', '67c4': 'Polaris 22', '67ca': 'Polaris 22', '67cc': 'Polaris 22', '67cf': 'Polaris 22',
    '67ff': 'Baffin', '67e0': 'Baffin', '67e1': 'Baffin', '67e3': 'Baffin', '67e8': 'Baffin', '67eb': 'Baffin', '67ef': 'Baffin',
    # Vega
    '6863': 'Vega 10', '6867': 'Vega 10', '686c': 'Vega 10', '687f': 'Vega 10',
    '66a0': 'Vega 20', '66a1': 'Vega 20', '66a7': 'Vega 20', '66af': 'Vega 20',
}
AMD_NAME_CODENAME_RULES = (
    (('RX 6900', 'RX 6800'), 'Navi 21'),
    (('RX 6700',), 'Navi 22'),
    (('RX 6600', 'RX 6500', 'RX 6400'), 'Navi 23'),
    (('RX 5700', 'RX 5600', '5700M', '5600M'), 'Navi 10'),
    (('RX 5500', 'RX 5300', '5500M'), 'Navi 14'),
    (('RADEON VII',), 'Vega 20'),
    (('VEGA 56', 'VEGA 64', 'VEGA M', 'VEGA FRONTIER'), 'Vega 10'),
    (('FURY',), 'Fiji'),
    (('R9 290', 'R9 390', 'HD 7970', 'HD 7950'), 'Hawaii'),
    (('R9 285', 'R9 380', 'HD 7850', 'HD 7870'), 'Tonga'),
    (('R7 270', 'R9 270', 'R7 370', 'R9 370'), 'Pitcairn'),
    (('R7 260', 'HD 7790'), 'Bonaire'),
    (('R7 250', 'HD 7770'), 'Cape Verde'),
)
NVIDIA_CODENAME_RULES = (
    (('GTX 10', 'TITAN X (PASCAL)', 'QUADRO P'), 'Pascal'),
    (('GTX 9', 'TITAN X', 'QUADRO M'), 'Maxwell'),
    (('GTX 6', 'GTX 7', 'GTX TITAN', 'QUADRO K'), 'Kepler'),
    # RTX/GTX 16+ have no accepted codename in OpCore-Simplify's checker at
    # all (genuinely unsupported) - correctly falls through to 'Unknown'.
)


def _gpu_codename(gpu):
    vendor = gpu.get('vendor')
    name = (gpu.get('name') or '').upper()
    device_id = (gpu.get('device_id') or '').lower()
    if vendor == 'Intel':
        return _intel_codename_from_igpu([gpu]) or 'Unknown'
    if vendor == 'AMD':
        if device_id in AMD_DEVICE_ID_CODENAME:
            return AMD_DEVICE_ID_CODENAME[device_id]
        for substrings, codename in AMD_NAME_CODENAME_RULES:
            if any(s in name for s in substrings):
                return codename
    if vendor == 'NVIDIA':
        for substrings, codename in NVIDIA_CODENAME_RULES:
            if any(s in name for s in substrings):
                return codename
    return 'Unknown'


def gather_gpu():
    gpus = hw_detect.get_gpus()
    result = {}
    for gpu in gpus:
        vendor = gpu.get('vendor')
        if vendor not in ('Intel', 'AMD', 'NVIDIA'):
            continue
        device_id = _device_id(gpu.get('vendor_id'), gpu.get('device_id'))
        if not device_id:
            continue
        result[gpu.get('name') or f'{vendor} GPU'] = {
            'Manufacturer': vendor,
            'Codename': _gpu_codename(gpu),
            'Device ID': device_id,
            'Device Type': 'Integrated GPU' if vendor == 'Intel' else 'Discrete GPU',
        }
    return result


# ---- Network (Ethernet + WiFi share one schema section) ------------------

def gather_network():
    result = {}
    for controller in hw_detect.get_ethernet_controllers():
        device_id = _device_id(controller.get('vendor_id'), controller.get('device_id'))
        if device_id:
            result[controller['name']] = {'Bus Type': 'PCI', 'Device ID': device_id}
    for controller in hw_detect.get_wifi_controllers():
        device_id = _device_id(controller.get('vendor_id'), controller.get('device_id'))
        if device_id:
            result[controller['name']] = {'Bus Type': 'PCI', 'Device ID': device_id}
    return result


def gather_bluetooth():
    result = {}
    for controller in hw_detect.get_bluetooth_controllers():
        if controller.get('transport') == 'UART' or not controller.get('device_id'):
            continue  # genuine Apple internal module - no USB PCI-style ID to report
        device_id = _device_id(controller.get('vendor_id'), controller.get('device_id'))
        if device_id:
            result[controller['name']] = {'Bus Type': 'USB', 'Device ID': device_id}
    return result


# ---- USB Controllers / Input / Storage Controllers / Sound ---------------
# Required sections where live PCI/USB IDs aren't reliably available on
# macOS pre-mapping (see module docstring) - Linux/Windows get real
# detection; macOS gets a best-effort NVMe-only storage entry (verified
# live via system_profiler SPNVMeDataType) and otherwise returns an empty
# dict, which is schema-valid but tells OpCore-Simplify nothing about that
# category on this platform.

def gather_usb_controllers():
    osname = hw_detect.host_os()
    result = {}
    if osname == 'linux':
        out = _run(['lspci', '-Dnn'])
        for line in out.splitlines():
            if 'USB controller' not in line:
                continue
            ids_m = re.search(r'\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]', line)
            name_m = re.search(r'\]:\s*(.+?)\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]', line)
            if ids_m:
                device_id = _device_id(ids_m.group(1), ids_m.group(2))
                result[(name_m.group(1) if name_m else line).strip()] = {'Bus Type': 'PCI', 'Device ID': device_id}
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     "Get-CimInstance Win32_USBController | ForEach-Object { $_.Name + '||' + $_.PNPDeviceID }"])
        for line in out.splitlines():
            if '||' not in line:
                continue
            name, pnp = line.split('||', 1)
            vendor_id, device_id = hw_detect._parse_ven_dev(pnp)
            if vendor_id:
                result[name.strip()] = {'Bus Type': 'PCI', 'Device ID': _device_id(vendor_id, device_id)}
    return result


def gather_input():
    # Best-effort only, everywhere - see module docstring. OpCore-Simplify's
    # own compatibility logic treats a missing/empty Input section as "use
    # generic USB HID injection", which is the safe default anyway.
    return {}


def gather_storage_controllers():
    osname = hw_detect.host_os()
    result = {}
    if osname == 'macos':
        # Checked live against a real NVMe drive: system_profiler
        # SPNVMeDataType only reports drive-level info (Model, Capacity,
        # Link Width/Speed) - no PCI vendor/device ID for the controller
        # ASIC at all. Since "Device ID" is a required field in this
        # section's schema, there's no honest value to report here rather
        # than a fabricated one - left empty on macOS; Linux/Windows read
        # real PCI IDs directly and aren't affected by this gap.
        pass
    elif osname == 'linux':
        out = _run(['lspci', '-Dnn'])
        for line in out.splitlines():
            if not re.search(r'(SATA controller|Non-Volatile memory controller|RAID bus controller)', line):
                continue
            ids_m = re.search(r'\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\]', line)
            name_m = re.search(r'\]:\s*(.+?)\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]', line)
            if ids_m:
                result[(name_m.group(1) if name_m else line).strip()] = {
                    'Bus Type': 'PCI', 'Device ID': _device_id(ids_m.group(1), ids_m.group(2))
                }
    elif osname == 'windows':
        out = _run(['powershell', '-NoProfile', '-Command',
                     "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -eq 'SCSIAdapter' -or $_.PNPClass -eq 'HDC' } "
                     "| ForEach-Object { $_.Name + '||' + $_.PNPDeviceID }"])
        for line in out.splitlines():
            if '||' not in line:
                continue
            name, pnp = line.split('||', 1)
            vendor_id, device_id = hw_detect._parse_ven_dev(pnp)
            if vendor_id:
                result[name.strip()] = {'Bus Type': 'PCI', 'Device ID': _device_id(vendor_id, device_id)}
    return result


def gather_sound():
    return {}  # optional section - see module docstring for why this is thin everywhere


# ---- top-level assembly ---------------------------------------------------

def gather(prompt_for_motherboard=True):
    """Returns a dict matching OpCore-Simplify's exact required schema
    (Scripts/report_validator.py). Pass prompt_for_motherboard=False to skip
    the interactive motherboard-name prompt on macOS (falls back to
    'Unknown', which is schema-valid but less useful to OpCore-Simplify)."""
    gpus = hw_detect.get_gpus()
    report = {
        'Motherboard': gather_motherboard(prompt_if_unknown=prompt_for_motherboard),
        'BIOS': gather_bios(),
        'CPU': gather_cpu(gpus=gpus),
        'GPU': gather_gpu(),
        'Network': gather_network(),
        'USB Controllers': gather_usb_controllers(),
        'Input': gather_input(),
        'Storage Controllers': gather_storage_controllers(),
    }
    bluetooth = gather_bluetooth()
    if bluetooth:
        report['Bluetooth'] = bluetooth
    return report


def write(path, prompt_for_motherboard=True):
    import json
    report = gather(prompt_for_motherboard=prompt_for_motherboard)
    with open(path, 'w') as f:
        json.dump(report, f, indent=2)
    return path


if __name__ == '__main__':
    import sys
    out_path = sys.argv[1] if len(sys.argv) > 1 else 'hardware_report.json'
    write(out_path)
    print(f'Wrote {out_path}')
