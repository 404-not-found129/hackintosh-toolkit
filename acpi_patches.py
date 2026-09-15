#!/usr/bin/env python3
"""
Automatic ACPI SSDT generation - dumps *this machine's* real ACPI tables and
generates the standard, well-tested set of generic patches, without needing
you to navigate a menu.

This drives corpnewt's SSDTTime (MIT, https://github.com/corpnewt/SSDTTime)
as a LIBRARY rather than launching its interactive tool: its dump/disassemble
plumbing (dsdt.DSDT.check_iasl/dump_tables/load) is already non-interactive,
and its five most commonly-needed generators are, on inspection of their
source, fully deterministic given a loaded DSDT:

  - plugin_type()  -> SSDT-PLUG / SSDT-PLUG-ALT (CPU power management)
  - fake_ec(laptop)-> SSDT-EC / SSDT-EC-USBX (fake embedded controller)
  - ssdt_awac()    -> SSDT-AWAC or SSDT-RTC0 (RTC/AWAC clock fix)
  - fix_hpet()     -> SSDT-HPET (IRQ conflict fix)
  - ssdt_usbx()    -> SSDT-USBX (generic USB power properties)

Each ends its interactive run with a "press [enter] to continue" pause and
nothing else - verified by reading SSDTTime.py directly - so this module
monkeypatches that single prompt function to a no-op and calls them back to
back. Patches that genuinely require a judgment call SSDTTime can't infer on
its own (laptop backlight/PNLF target selection, XOSI Windows-version
ranges, IMEI bridge, custom PCI bridges) are NOT auto-generated - those stay
behind run_interactive() below, which hands you SSDTTime's real menu.

Two real limitations, inherited from SSDTTime itself, not introduced here:
  - Automatic ACPI dumping only works on Windows and Linux (SSDTTime has no
    macOS dump path - Apple doesn't expose it the same way). On macOS, use
    run_interactive() with an existing dump, or run this from Windows/Linux
    against the same physical machine instead.
  - This all needs to run ON the target machine. An SSDT generated from a
    different computer's ACPI tables is worthless (and can hang that other
    machine's boot if used there instead of the one it came from).
"""

import os
import plistlib
import shutil
import subprocess
import sys

import hw_detect
import net

REPO = 'corpnewt/SSDTTime'


def fetch_tool(workdir):
    return net.fetch_repo_source_zip(REPO, workdir)


def _load_ssdt_class(tool_dir):
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    # SSDTTime ships its own local "Scripts" package - evict any other
    # tool's same-named package (e.g. CPUFriendFriend's) that may already be
    # cached in this process, or we'd silently load the wrong one.
    net.isolate_module_cache('Scripts', 'SSDTTime')
    import SSDTTime  # the module defines a class named SSDT
    return SSDTTime.SSDT


def _dump_and_load(ssdt, tool_dir):
    """Non-interactive dump+disassemble. Raises RuntimeError with a clear
    reason on failure rather than falling through to an interactive prompt."""
    osname = hw_detect.host_os()
    if osname not in ('windows', 'linux'):
        raise RuntimeError(
            'Automatic ACPI dumping only works on Windows/Linux (no macOS path in SSDTTime). '
            'Use run_interactive() with an existing dump, or run this step from Windows/Linux instead.'
        )

    print('Fetching iasl (and acpidump on Windows) if not already present...')
    if not ssdt.d.check_iasl():
        raise RuntimeError('Could not obtain iasl - check your internet connection and try again.')

    output_root = os.path.join(tool_dir, ssdt.output)
    name = ssdt.get_unique_name('OEM', output_root, name_append='')
    print('Dumping this machine\'s real ACPI tables...')
    dumped_dir = ssdt.d.dump_tables(os.path.join(output_root, name))
    if not dumped_dir:
        raise RuntimeError('ACPI table dump failed - see the output above for the reason.')

    print('Disassembling...')
    path = ssdt.load_dsdt(dumped_dir)
    if not path:
        raise RuntimeError('Failed to disassemble the dumped ACPI tables.')
    ssdt.dsdt = path
    if not ssdt._ensure_dsdt(allow_any=True):
        raise RuntimeError('DSDT did not load correctly after disassembly.')


def generate(efi_dest, workdir, laptop=False, tool_dir=None):
    """
    Non-interactive: dumps this machine's real ACPI tables and generates the
    five deterministic SSDTs listed in the module docstring.

    efi_dest: the 'EFI' folder produced by opencore_build.build_efi().
    laptop:   pass True on a laptop so fake_ec() builds the laptop-safe
              variant (leaves a real embedded controller's _STA alone
              instead of unconditionally overriding it).
    tool_dir: reuse an already-fetched SSDTTime checkout instead of
              downloading a fresh one.
    Returns the list of .aml filenames added.
    """
    oc_dir = os.path.join(efi_dest, 'OC')
    tool_dir = tool_dir or fetch_tool(workdir)
    ssdt_cls = _load_ssdt_class(tool_dir)
    ssdt = ssdt_cls()

    # Every one of the five generators below ends with a bare
    # "press [enter] to continue" whose return value is discarded - verified
    # by reading SSDTTime.py's source for each. Silencing it just removes an
    # unnecessary keypress; it does not skip any decision logic.
    ssdt.u.grab = lambda *a, **k: ''

    _dump_and_load(ssdt, tool_dir)

    print('Generating SSDT-PLUG (CPU power management)...')
    ssdt.plugin_type()
    print('Generating SSDT-EC (fake embedded controller)...')
    ssdt.fake_ec(laptop=laptop)
    print('Generating SSDT-AWAC/RTC0 (RTC clock fix)...')
    ssdt.ssdt_awac()
    print('Generating SSDT-HPET fix (timer IRQ conflicts)...')
    ssdt.fix_hpet()
    print('Generating SSDT-USBX (generic USB power properties)...')
    ssdt.ssdt_usbx()

    added = collect_results(tool_dir, oc_dir)
    if added:
        register_in_config(os.path.join(oc_dir, 'config.plist'), added)
        print(f'Registered in config.plist: {", ".join(added)}')
    else:
        print('None of the generators produced a new SSDT - your board may not need these patches.')
    return added


def run_interactive(tool_dir):
    """
    Launches SSDTTime's real menu with inherited stdio, for the patches that
    need a judgment call: laptop backlight (PNLF), XOSI Windows-version
    spoofing, IMEI bridge, custom PCI bridges, or re-doing anything the
    automatic pass above skipped.
    """
    script = os.path.join(tool_dir, 'SSDTTime.py')
    if not os.path.exists(script):
        raise RuntimeError(f'SSDTTime.py not found in {tool_dir}')
    print()
    print('Handing off to SSDTTime\'s own menu for the patches that need a judgment call.')
    print('When you are done, choose its exit option to return here.')
    print()
    subprocess.run([sys.executable, script], cwd=tool_dir)


def collect_results(tool_dir, oc_dir):
    """SSDTTime writes finished .aml files under <tool_dir>/Results. Copies
    every .aml found there into EFI/OC/ACPI/Add and returns the filenames
    added, so the caller can register them in config.plist."""
    results_dir = os.path.join(tool_dir, 'Results')
    acpi_add_dir = os.path.join(oc_dir, 'ACPI', 'Add')
    os.makedirs(acpi_add_dir, exist_ok=True)

    added = []
    if os.path.isdir(results_dir):
        for name in sorted(os.listdir(results_dir)):
            if name.lower().endswith('.aml'):
                dest = os.path.join(acpi_add_dir, name)
                if not os.path.exists(dest):
                    shutil.copy2(os.path.join(results_dir, name), dest)
                    added.append(name)
    return added


def register_in_config(config_path, aml_filenames):
    """Adds an ACPI->Add entry for each new SSDT filename, skipping duplicates."""
    with open(config_path, 'rb') as f:
        config = plistlib.load(f)

    acpi_add = config.setdefault('ACPI', {}).setdefault('Add', [])
    existing = {entry.get('Path') for entry in acpi_add}

    for name in aml_filenames:
        if name in existing:
            continue
        acpi_add.append({
            'Comment': name,
            'Enabled': True,
            'Path': name,
        })

    with open(config_path, 'wb') as f:
        plistlib.dump(config, f)

    return acpi_add


def apply(efi_dest, workdir, laptop=False, interactive_followup=False):
    """
    Runs the automatic generator where supported, then optionally hands off
    to SSDTTime's real menu for the judgment-call patches too.
    Returns the combined list of SSDT filenames added.
    """
    added = []
    tool_dir = fetch_tool(workdir)

    if hw_detect.host_os() in ('windows', 'linux'):
        added = generate(efi_dest, workdir, laptop=laptop, tool_dir=tool_dir)
    else:
        print('Automatic ACPI dumping needs Windows or Linux - see module docstring. '
              'Falling back to the interactive menu (bring an existing DSDT dump if you have one).')
        interactive_followup = True

    if interactive_followup:
        oc_dir = os.path.join(efi_dest, 'OC')
        run_interactive(tool_dir)
        more = collect_results(tool_dir, oc_dir)
        if more:
            register_in_config(os.path.join(oc_dir, 'config.plist'), more)
            print(f'Registered in config.plist: {", ".join(more)}')
        added += more

    return added


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f'Usage: {sys.argv[0]} <path to EFI/EFI folder produced by opencore_build.py> [--laptop] [--interactive]')
        sys.exit(1)
    apply(sys.argv[1], os.path.join(os.getcwd(), 'acpi_work'),
          laptop='--laptop' in sys.argv, interactive_followup='--interactive' in sys.argv)
