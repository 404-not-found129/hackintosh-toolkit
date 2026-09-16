#!/usr/bin/env python3
"""
CPUFriend fixes a real problem: on a spoofed SMBIOS, macOS's native CPU power
management data (from X86PlatformPlugin/ACPI_SMC_PlatformPlugin) is keyed to
the *spoofed* model, not your actual CPU, and reliably attaching correct data
to a Hackintosh's mismatched identity is exactly what CPUFriend's injection
mechanism was built to work around. It matters most on laptops, where wrong
power/frequency data shows up directly as bad battery life and thermal
behavior - hence "for laptops" - though the same mismatch can happen on a
spoofed desktop model too.

Straight from CPUFriend's own docs, though: "CPUFriend is most likely NOT
required when not sure whether or not to use it... do not use it for data
customization until one knows clearly what the power management data really
is... if nothing is provided, CPUFriend does nothing and the original data is
used as if this kext is not installed." This is opt-in for a reason. Whether
CPUFriend.kext itself gets staged is now up to what you select in
OpCore-Simplify's own kext customization menu (option 4) - this module only
handles the data half, which nothing else can generate ahead of time anyway
(see below), and works regardless of how CPUFriend.kext got there.

The data half (CPUFriendDataProvider.kext) genuinely cannot be built ahead of
time, for the same reason ACPI/USB generation can't be templated: the source
data - your target SMBIOS model's real FrequencyVectors table - only exists
inside `/System/Library/Extensions/IOPlatformPluginFamily.kext/.../Resources/
<board-id>.plist` on an *already-booted* macOS running with that exact
SMBIOS active. There is no pre-boot equivalent. So this module is a
post-first-boot step, run from macOS itself once this drive's EFI has
successfully gotten you into the OS with your chosen SMBIOS model.

It drives corpnewt's CPUFriendFriend (MIT, github.com/corpnewt/CPUFriendFriend)
as a library. Reading its source: every one of its interactive prompts
(Low Frequency Mode, EPP, Perf Bias) explicitly falls back to the value
already present in Apple's own shipped table when given no input - so this
calls it with every prompt answered "accept the stock value", changing
nothing about the actual numbers, only how they get delivered. The one
exception (an "enable extra MacBook Air power-saving features" yes/no with
no safe blank default) is answered "no" explicitly, since that changes
device behavior and should be an informed opt-in, not a default.
"""

import os
import plistlib
import sys

import hw_detect
import net

REPO = 'corpnewt/CPUFriendFriend'


def fetch_tool(workdir):
    return net.fetch_repo_source_zip(REPO, workdir)


def _safe_grab(prompt, **kwargs):
    # CPUFriendFriend.py's "enable extra power-saving features" y/N prompt
    # loops forever on a blank answer (no safe default baked in) - everything
    # else falls back to Apple's own stock value on blank input.
    if 'Enable these features' in prompt:
        return 'N'
    return ''


def generate(efi_dest, workdir):
    """
    Non-interactive: reads this machine's real, currently-active
    X86PlatformPlugin resource file for its live SMBIOS/board-id and
    generates CPUFriendDataProvider.kext from it, unmodified from Apple's
    own stock values. Must run on the actual target machine, already booted
    into macOS with the EFI this toolkit built.

    efi_dest: the 'EFI' folder produced by opcore_simplify.py.
    Returns the kext bundle name added, or None on failure (reason printed).
    """
    if hw_detect.host_os() != 'macos':
        raise RuntimeError(
            'CPUFriendDataProvider can only be generated from a live, already-booted macOS '
            '(it reads /System/Library/Extensions/IOPlatformPluginFamily.kext/... directly). '
            'Boot this drive into macOS first, then run this from there.'
        )

    # CPUFriend.kext presence is no longer guaranteed by this toolkit - it's
    # whatever was selected in OpCore-Simplify's own kext customization menu -
    # so check before doing the expensive part (live ACPI read, ResourceConverter)
    # to produce a CPUFriendDataProvider.kext that would silently do nothing.
    if not os.path.isdir(os.path.join(efi_dest, 'OC', 'Kexts', 'CPUFriend.kext')):
        print(f'CPUFriend.kext not found in {efi_dest}/OC/Kexts - add it there first '
              '(https://github.com/acidanthera/CPUFriend/releases) and register it in '
              'config.plist, or this generated data will have nothing to attach to.')
        return None

    tool_dir = fetch_tool(workdir)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)

    # CPUFriendFriend ships its own local "Scripts" package - evict any other
    # tool's same-named package (e.g. SSDTTime's) that may already be cached
    # in this process, or we'd silently load the wrong one (verified: this is
    # a real collision, not theoretical - both tools use this package name).
    net.isolate_module_cache('Scripts', 'CPUFriendFriend')

    # CPUFriendFriend.py instantiates and runs itself at import time
    # ("c = CPUFF(); c.main()"), and every exit path in main() calls
    # exit()/exit(1) rather than returning - both are SystemExit, which
    # we need to catch here rather than let kill our own process.
    import importlib
    module_name = 'CPUFriendFriend'

    # Patch the prompt function before import triggers main() to run.
    from Scripts import utils as _utils  # the tool's own Scripts package, now on sys.path
    _orig_grab = _utils.Utils.grab
    _utils.Utils.grab = lambda self, prompt, **kwargs: _safe_grab(prompt, **kwargs)

    try:
        try:
            importlib.import_module(module_name)
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        _utils.Utils.grab = _orig_grab

    if exit_code:
        print('CPUFriendFriend did not complete successfully - see its output above for why '
              '(common cause: board-id not found, meaning the currently running SMBIOS/board-id '
              "resource file isn't present - this must run on the exact machine/SMBIOS you built for).")
        return None

    results_dir = os.path.join(tool_dir, 'Results')
    kext_path = net.find_first(results_dir, lambda p: p.endswith('CPUFriendDataProvider.kext') and os.path.isdir(p))
    if not kext_path:
        print(f'CPUFriendFriend finished but no CPUFriendDataProvider.kext was found in {results_dir}.')
        return None

    import shutil
    oc_dir = os.path.join(efi_dest, 'OC')
    kexts_dir = os.path.join(oc_dir, 'Kexts')
    dest = os.path.join(kexts_dir, 'CPUFriendDataProvider.kext')
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(kext_path, dest)

    register_in_config(os.path.join(oc_dir, 'config.plist'))
    print('Wrote CPUFriendDataProvider.kext and registered it (after CPUFriend.kext) in config.plist.')
    return 'CPUFriendDataProvider.kext'


def register_in_config(config_path):
    """Ensures both CPUFriend.kext and CPUFriendDataProvider.kext are present
    in Kernel->Add, with CPUFriend loading first (it's a Lilu plugin;
    DataProvider is pure data CPUFriend reads at runtime, order between the
    two doesn't functionally matter, but keeping CPUFriend first mirrors how
    every published EFI in the wild orders it)."""
    with open(config_path, 'rb') as f:
        config = plistlib.load(f)

    kext_add = config.setdefault('Kernel', {}).setdefault('Add', [])
    existing = {entry.get('BundlePath') for entry in kext_add}

    if 'CPUFriend.kext' not in existing:
        kext_add.append({
            'Arch': 'Any', 'BundlePath': 'CPUFriend.kext', 'Comment': '',
            'Enabled': True, 'ExecutablePath': 'Contents/MacOS/CPUFriend',
            'MaxKernel': '', 'MinKernel': '', 'PlistPath': 'Contents/Info.plist',
        })
    if 'CPUFriendDataProvider.kext' not in existing:
        kext_add.append({
            'Arch': 'Any', 'BundlePath': 'CPUFriendDataProvider.kext', 'Comment': '',
            'Enabled': True, 'ExecutablePath': '',
            'MaxKernel': '', 'MinKernel': '', 'PlistPath': 'Contents/Info.plist',
        })

    with open(config_path, 'wb') as f:
        plistlib.dump(config, f)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} <path to EFI/EFI folder produced by opcore_simplify.py>')
        print('Run this from macOS, already booted with this EFI\'s SMBIOS model.')
        sys.exit(1)
    generate(sys.argv[1], os.path.join(os.getcwd(), 'cpufriend_work'))
