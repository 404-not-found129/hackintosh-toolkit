#!/usr/bin/env python3
"""
Drives lzhoang2801/OpCore-Simplify (github.com/lzhoang2801/OpCore-Simplify,
BSD-3-Clause) as this toolkit's EFI builder, replacing the previous
custom-built opencore_build.py/gpu_compat.py/bluetooth_compat.py/
wifi_compat.py/acpi_patches.py/smbios.py pipeline.

OpCore-Simplify only does one thing (build the EFI folder from a hardware
description) and does it as one big, deeply stateful, menu-driven tool - its
own README documents no library-safe way to call individual steps the way
SSDTTime's generators turned out to be (see acpi_patches.py). So this module
does exactly what it does for USBToolBox: fetch the real tool, hand off to
its own real menu with inherited stdio, and harvest whatever it produces
into this toolkit's EFI partition afterward.

Two things it doesn't get from you interactively, both normally supplied by
"Hardware Sniffer" (Windows-only) alongside each other:

  - The hardware report JSON describing CPU/GPU/motherboard/etc. -
    hardware_report.py builds a cross-platform equivalent, verified against
    OpCore-Simplify's own real validator (Scripts/report_validator.py).
  - Real ACPI table dumps - OpCore-Simplify's own ACPI customization step
    (Scripts/acpi_guru.py select_acpi_tables()) only accepts a folder path
    to already-dumped tables; verified by reading its source, it has no
    dump capability of its own, so skipping this crashes that step with
    an AttributeError the moment it tries to read a DSDT that was never
    loaded. dump_acpi_tables() below fetches corpnewt's SSDTTime (MIT) for
    just its dump_tables() utility (the same non-interactive mechanism
    acpi_patches.py used before this rewrite) - Windows/Linux only, same
    platform limitation as everywhere else ACPI dumping comes up in this
    toolkit (SSDTTime has no macOS dump path either).

At OpCore-Simplify's own menu, pick "1. Select Hardware Report" and paste
the hardware-report path, then when it asks for the ACPI tables folder,
paste that path too. Everything after that (macOS version, ACPI/kext
customization, SMBIOS model, build) is its own real, tested logic - this
module does not attempt to drive or second-guess any of those choices.
"""

import os
import shutil
import subprocess
import sys

import hardware_report
import hw_detect
import net

REPO = 'lzhoang2801/OpCore-Simplify'


def fetch_tool(workdir):
    return net.fetch_repo_source_zip(REPO, workdir, branch='main')


def dump_acpi_tables(workdir):
    """
    Windows/Linux only (see module docstring). Returns the folder path
    containing the dumped .aml tables, or None if unsupported here / the
    dump failed.

    Unlike this toolkit's old acpi_patches.py, this is NOT optional: reading
    OpCore-Simplify's own OpCore-Simplify.py directly shows selecting a
    hardware report unconditionally calls ensure_dsdt() -> select_acpi_tables()
    right afterward, with no way to skip it from the menu. Confirmed live -
    with no tables available, that step doesn't just skip gracefully, it
    loops once ("No valid .aml files were found!") and then hard-crashes
    with AttributeError. So on macOS, where this can't produce anything (see
    module docstring), OpCore-Simplify's hardware-report path is not usable
    at all - not degraded, blocked - unless you already have a real ACPI
    dump from this same physical machine's Windows/Linux side to supply by
    hand. This is a real limitation of the tool being integrated, not
    something this toolkit's own code is choosing not to support.
    """
    if hw_detect.host_os() not in ('windows', 'linux'):
        print()
        print('!! ACPI table dumping needs Windows or Linux, and this step is NOT optional -')
        print('!! OpCore-Simplify hard-crashes without real ACPI tables the moment you select')
        print('!! a hardware report (verified against its own source). Run this from Windows')
        print('!! or Linux instead, or supply an existing ACPI dump from this same physical')
        print('!! machine\'s Windows/Linux side if you already have one.')
        print()
        return None

    net.isolate_module_cache('Scripts', 'SSDTTime')
    tool_dir = net.fetch_repo_source_zip('corpnewt/SSDTTime', workdir)
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)
    import SSDTTime
    ssdt = SSDTTime.SSDT()
    ssdt.u.grab = lambda *a, **k: ''  # silence "press enter" pauses only - see acpi_patches.py's old rationale

    if not ssdt.d.check_iasl():
        print('Could not obtain iasl - check your internet connection.')
        return None

    output_root = os.path.join(tool_dir, ssdt.output)
    name = ssdt.get_unique_name('OEM', output_root, name_append='')
    dumped_dir = ssdt.d.dump_tables(os.path.join(output_root, name))
    if not dumped_dir:
        print('ACPI table dump failed - see the output above for why.')
        return None
    return dumped_dir


def run_interactive(tool_dir, hardware_report_path, acpi_tables_dir):
    entry = os.path.join(tool_dir, 'OpCore-Simplify.py')
    if not os.path.exists(entry):
        raise RuntimeError(f'OpCore-Simplify.py not found in {tool_dir}')

    print()
    print('Handing off to OpCore-Simplify\'s own menu.')
    print('  0. It first asks "Do you want to skip the update process? (yes/No)" - this copy was')
    print('     fetched fresh as a zip (no git history for it to diff against), so its own update')
    print('     check cannot succeed here regardless of answer. Type "yes" to skip it.')
    print(f'  1. Choose "1. Select Hardware Report" and paste this path:')
    print(f'       {hardware_report_path}')
    if acpi_tables_dir:
        print('     When it then asks for the ACPI Tables folder (mandatory, not skippable), paste:')
        print(f'       {acpi_tables_dir}')
    else:
        print('     It will then ask for an ACPI Tables folder - this is NOT skippable, and with no')
        print('     dump available on this OS it will loop once and then hard-crash. See the warning')
        print('     printed above before this - you need to be on Windows/Linux, or already have a')
        print('     real ACPI dump from this same machine to paste when it asks.')
    print('  2. Work through macOS version / ACPI / kext / SMBIOS customization as you prefer -')
    print('     this is OpCore-Simplify\'s own tested logic, not anything this toolkit second-guesses.')
    print('  3. Choose "6. Build OpenCore EFI" when ready, then quit ("Q") once it finishes.')
    print()
    subprocess.run([sys.executable, entry], cwd=tool_dir)


def find_built_efi(tool_dir):
    """
    OpCore-Simplify writes its output to <tool_dir>/Results/EFI (verified
    against its own OpCore-Simplify.py: build_opencore_efi() copies
    OpenCorePkg into self.result_dir, then writes config.plist under
    <result_dir>/EFI/OC/). Returns that path, or None if nothing was built
    (e.g. you quit before "6. Build OpenCore EFI").
    """
    results_efi = os.path.join(tool_dir, 'Results', 'EFI')
    return results_efi if os.path.isdir(results_efi) else None


def copy_efi(built_efi_dir, efi_mount_path):
    """Copies an already-built EFI folder (from find_built_efi(), or
    run()'s return value) onto a mounted EFI partition. Returns the
    destination path."""
    efi_dest = os.path.join(efi_mount_path, 'EFI')
    if os.path.exists(efi_dest):
        shutil.rmtree(efi_dest)
    shutil.copytree(built_efi_dir, efi_dest)
    return efi_dest


def run(workdir, prompt_for_motherboard=True):
    """
    Generates a hardware report for this machine and hands off to
    OpCore-Simplify's real interactive tool. Doesn't need a mounted disk -
    OpCore-Simplify builds into its own tool_dir/Results folder regardless.
    Returns the built EFI folder's path, or None if the user quit before
    building (use copy_efi() afterward once you have somewhere to put it).
    """
    os.makedirs(workdir, exist_ok=True)
    report_path = os.path.join(workdir, 'hardware_report.json')
    hardware_report.write(report_path, prompt_for_motherboard=prompt_for_motherboard)
    print(f'Wrote hardware report to {report_path}')

    acpi_tables_dir = dump_acpi_tables(os.path.join(workdir, 'acpi'))
    if acpi_tables_dir:
        print(f'Dumped this machine\'s ACPI tables to {acpi_tables_dir}')

    tool_dir = fetch_tool(workdir)
    run_interactive(tool_dir, report_path, acpi_tables_dir)
    built = find_built_efi(tool_dir)
    if not built:
        print(f'No EFI folder found under {tool_dir}/Results - did you finish '
              '"6. Build OpenCore EFI" before quitting?')
    return built


def build(efi_mount_path, workdir, prompt_for_motherboard=True):
    """Convenience wrapper for when you already have a mounted EFI partition
    to write straight to: run() + copy_efi() in one call."""
    built = run(workdir, prompt_for_motherboard=prompt_for_motherboard)
    return copy_efi(built, efi_mount_path) if built else None


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} <path where the EFI partition is mounted>')
        sys.exit(1)
    result = build(sys.argv[1], os.path.join(os.getcwd(), 'opcore_simplify_work'))
    if result:
        print(f'EFI written to {result}')
