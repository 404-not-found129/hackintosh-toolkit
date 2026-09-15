#!/usr/bin/env python3
"""
Decide which macOS versions will have *native, accelerated* graphics on the
detected GPU(s), independent of the "just boot it anyway with no
acceleration" option (which always works, on anything, and is offered
separately by the wizard).

Darwin major versions used throughout:
  17 High Sierra 18 Mojave  19 Catalina 20 Big Sur   21 Monterey
  22 Ventura     23 Sonoma  24 Sequoia  25 Tahoe

The Intel integrated-GPU rules below are ported near verbatim from the
device-ID logic in lzhoang2801/OpCore-Simplify (BSD-3-Clause), which encodes
years of community testing precisely by PCI device ID and is the most
reliable part of this whole space.

Discrete AMD/NVIDIA support is expressed as a *name-substring* heuristic
instead, because a byte-accurate device-ID table is a multi-thousand-line
moving target (Acidanthera/WhateverGreen and OCLP maintain the canonical
ones). Treat the AMD/NVIDIA verdicts here as "best guess, verify before you
commit a lot of time" - the wizard always still offers the no-acceleration
override regardless of what this module concludes.
"""

LATEST_DARWIN = 25  # Tahoe
OLDEST_DARWIN = 17  # High Sierra


def _intel_igpu_range(device_id, cpu):
    d = device_id.upper()
    if cpu.get('low_end'):
        return None  # Celeron/Pentium iGPU variants are unsupported outright
    if d[:4] in ('0042', '0046'):
        return (OLDEST_DARWIN, 17)  # Arrandale mobile
    if d.startswith('01') and d[-2] not in ('5', '6') and d not in ('0102', '0106', '010A'):
        return (OLDEST_DARWIN, 17)  # odd Sandy Bridge SKUs
    if d.startswith('01') and d not in ('0152', '0156'):
        return (OLDEST_DARWIN, 20)  # Sandy/Ivy Bridge -> Big Sur
    if d[:2] in ('04', '0A', '0C', '0D', '0B', '16'):
        return (OLDEST_DARWIN, 21)  # Haswell/Broadwell -> Monterey
    if d[:2] in ('09', '19', '59', '3E', '87') or d[:2] == '9B':
        excluded = ('3E90', '3E93', '3E99', '3E9C', '3EA1', '3EA4',
                    '9B21', '9BA0', '9BA2', '9BA4', '9BA5', '9BA8', '9BAA', '9BAB', '9BAC')
        if d not in excluded:
            return (OLDEST_DARWIN, LATEST_DARWIN)  # Skylake..Comet Lake, full run
    if d.startswith('8A'):
        return (19, LATEST_DARWIN)  # Ice Lake, needs Catalina 10.15.4+
    return None


# (name substrings, min_darwin, max_darwin) - first match wins, checked in order.
_AMD_RULES = [
    (('RX 7', 'RX 9'), None, None),                      # RDNA3+ - no macOS driver at all
    (('RX 6',), 20, LATEST_DARWIN),                        # RDNA2 (Navi 2x) - Big Sur+
    (('RX 5500', 'RX 5600', 'RX 5700', '5600M', '5700M'), 19, LATEST_DARWIN),  # RDNA1 (Navi 1x)
    (('VEGA 20', 'RADEON VII'), 18, LATEST_DARWIN),
    (('VEGA 10', 'VEGA 56', 'VEGA 64', 'VEGA M'), 17, LATEST_DARWIN),
    (('RX 460', 'RX 470', 'RX 480', 'RX 560', 'RX 570', 'RX 580', 'RX 590',
      'POLARIS', '560X', '570X', '580X'), 17, LATEST_DARWIN),
    (('R9 ', 'R7 ', 'HD 7', 'HD 8', 'FIREPRO', 'FURY', 'TAHITI', 'TONGA',
      'HAWAII', 'BONAIRE', 'PITCAIRN', 'CAPE VERDE', 'OLAND', 'HAINAN'), 17, 21),  # GCN 1-3
]

_NVIDIA_RULES = [
    (('RTX', 'GTX 16', 'GTX 20', 'TITAN V', 'TITAN RTX', 'MX'), None, None),  # Pascal-successor - no driver
    (('GTX 10', 'TITAN X (PASCAL)', 'QUADRO P', 'TESLA P'), 17, 17),          # Pascal - High Sierra only, fragile
    (('GTX 9', 'TITAN X', 'QUADRO M'), 17, 17),                               # Maxwell - High Sierra only, fragile
    (('GTX 6', 'GTX 7', 'GTX TITAN', 'QUADRO K', 'TESLA K'), 17, 19),         # Kepler - through Catalina
]


def _discrete_gpu_range(name, rules):
    upper = name.upper()
    for substrings, lo, hi in rules:
        if any(s in upper for s in substrings):
            return None if lo is None else (lo, hi)
    return None  # unknown model -> can't vouch for it


def gpu_native_range(gpu, cpu):
    """Returns (min_darwin, max_darwin) of native acceleration, or None if unsupported/unknown."""
    vendor = gpu.get('vendor')
    if vendor == 'Intel':
        return _intel_igpu_range(gpu.get('device_id', ''), cpu)
    if vendor == 'AMD':
        r = _discrete_gpu_range(gpu.get('name', ''), _AMD_RULES)
        if r and 'NAVI 2' in gpu.get('name', '').upper() and not cpu.get('avx2', True):
            return (r[0], 21)  # Navi 2x needs AVX2 for full run; without it, capped at Monterey
        return r
    if vendor == 'NVIDIA':
        return _discrete_gpu_range(gpu.get('name', ''), _NVIDIA_RULES)
    return None


def evaluate(cpu, gpus):
    """
    Combine every detected GPU's native range into one overall verdict.
    Returns dict: {'native': (lo,hi) or None, 'per_gpu': [...], 'notes': [...]}
    If there are multiple GPUs (e.g. iGPU + dGPU), native support is the
    *widest* union a user could realistically pick by choosing which GPU
    drives the display - not their intersection - since only one needs to
    output video.
    """
    if not gpus:
        return {'native': None, 'per_gpu': [], 'notes': ['No GPU detected - run this on the target machine, not a VM without GPU passthrough.']}

    per_gpu = []
    best = None
    for gpu in gpus:
        rng = gpu_native_range(gpu, cpu)
        per_gpu.append({'gpu': gpu, 'range': rng})
        if rng:
            if best is None:
                best = rng
            else:
                best = (min(best[0], rng[0]), max(best[1], rng[1]))

    notes = []
    if not cpu.get('avx2', True):
        notes.append('CPU lacks AVX2 - this rules out Monterey (12) and later entirely, regardless of GPU.')
    if cpu.get('low_end'):
        notes.append('Celeron/Pentium CPUs are not supported by any modern macOS release.')

    return {'native': best, 'per_gpu': per_gpu, 'notes': notes}
