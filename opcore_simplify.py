#!/usr/bin/env python3
"""
Drives lzhoang2801/OpCore-Simplify (github.com/lzhoang2801/OpCore-Simplify,
BSD-3-Clause) as this toolkit's EFI builder, replacing the previous
custom-built opencore_build.py/gpu_compat.py/bluetooth_compat.py/
wifi_compat.py/acpi_patches.py/smbios.py pipeline.

OpCore-Simplify only does one thing (build the EFI folder from a hardware
description) and does it as one big, deeply stateful, menu-driven tool. This
module drives that real menu end to end for a one-click build: it imports
OpCore-Simplify in-process and answers its own request_input() prompts by
matching their literal text against every prompt its source can actually
produce (catalogued by reading OpCore-Simplify.py and every Scripts/*.py
file that calls request_input() - 54 call sites total), using the same
recommended/suggested default its own menu already offers wherever one
exists (blank input already means "use the suggested value" for most of
them - see _build_answer_table()'s comments for exactly which, and why).

Two kinds of prompt are deliberately answered by a real human instead of a
canned default, because there's no "recommended" choice to fall back on:
  - Which GPU/WiFi/Bluetooth device to use, on hardware with more than one -
    picking wrong can mean no video output or no WiFi/BT at all.
  - Whether to proceed with OpenCore Legacy Patcher - disables SIP/AMFI and
    requires full-installer updates; a real security/stability tradeoff,
    not a build-mechanics default.
Anything this driver's prompt table doesn't recognize at all also falls
back to a real prompt rather than guessing blind, and gives up after the
same exact prompt repeats 3 times in a row (would otherwise spin forever).

Two things OpCore-Simplify needs that it doesn't collect interactively,
both normally supplied by "Hardware Sniffer" (Windows-only) alongside each
other:

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
"""

import importlib.util
import os
import re
import shutil
import sys

import hardware_report
import hw_detect
import net

REPO = 'lzhoang2801/OpCore-Simplify'

_PRESS_ENTER_RE = re.compile(r'^Press Enter', re.IGNORECASE)
_MAIN_MENU_PROMPT = 'Select an option: '


class AutomationStuck(RuntimeError):
    """The same exact OpCore-Simplify prompt repeated 3 times in a row
    without this driver's table resolving it to a real answer - almost
    certainly a genuinely unrecognized prompt (upstream changed something)
    rather than a normal hardware-specific question."""


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


def _build_answer_table(hardware_report_path, acpi_tables_dir):
    """
    Ordered (regex, answer) pairs. `answer` of None means "a real human
    needs to answer this one" - the driver prints why, then falls back to
    a real input() call just for that prompt before resuming automation.
    Every pattern here is copied verbatim from OpCore-Simplify's own
    source (see module docstring) - not guessed from what the menu
    "probably" says.
    """
    return [
        (re.compile(r'^Do you want to skip the update process\? \(yes/No\): $'),
         ('yes', None)),
        (re.compile(r'^Drag and drop your hardware report here'),
         (hardware_report_path, None)),
        (re.compile(r'^Please drag and drop ACPI Tables folder here: $'),
         (acpi_tables_dir, None) if acpi_tables_dir else
         (None, 'no ACPI dump is available on this host (see the warning printed earlier)')),
        (re.compile(r'^Please enter the macOS version you want to use \(default: .*\): $'),
         ('', None)),  # blank = OpCore-Simplify's own suggested/compatible version
        (re.compile(r'^Build EFI for UEFI\? \(Yes/no\): $'),
         ('yes', None)),  # matches this toolkit's own README guidance to boot UEFI-only
        (re.compile(r'^Select a .+ (combination|device) \(1-\d+\): $'),
         (None, 'multiple GPU/WiFi/Bluetooth devices detected - no safe default, picking wrong can mean no video/no WiFi')),
        (re.compile(r'^Select audio kext for your system: $'),
         ('1', None)),  # AppleALC - matches every pre-Tahoe macOS version's own un-prompted default
        (re.compile(r'^Select kext for your AMD .+ GPU \(default: .*\): $'),
         ('', None)),
        (re.compile(r'^Select kext for your Intel WiFi device \(default: .*\): $'),
         ('', None)),
        (re.compile(r'^Do you want to force load (these kexts|this kext) on the unsupported macOS version\? \(yes/No\): $'),
         ('no', None)),  # matches the printed capital-N default; forcing can cause real instability
        (re.compile(r'^Enter the ID of the codec layout you want to use \(default: .*\): $'),
         ('', None)),
        (re.compile(r'^Continue with OpenCore Legacy Patcher\? \(yes/No\): $'),
         (None, 'OCLP disables SIP/AMFI and needs full-installer updates going forward - a real tradeoff, not a build default')),
    ]


def _install_auto_answers(tool_dir, hardware_report_path, acpi_tables_dir, picked):
    """
    Monkeypatches OpCore-Simplify's Utils.request_input so driving its real
    menu (via OCPE.main(), called exactly like its own __main__ block does)
    needs no human at the keyboard for the ordinary case. Returns nothing;
    mutates `picked` in place with picked['darwin_version'] once a macOS
    version has actually been selected, so the caller can fetch the
    matching Recovery image without asking a second time.
    """
    from Scripts import utils as oc_utils

    answer_table = _build_answer_table(hardware_report_path, acpi_tables_dir)
    real_input = input
    state = {'menu_visits': 0, 'last_prompt': None, 'repeat_count': 0}

    def auto_request_input(self, prompt='Press Enter to continue...'):
        if prompt == _MAIN_MENU_PROMPT:
            state['menu_visits'] += 1
            # 1st visit selects the hardware report, which (per OpCore-
            # Simplify's own main(), read from its source) runs the whole
            # compatibility/version/hardware-customization/SMBIOS/ACPI/kext
            # chain inline before returning here. 2nd visit builds. Anything
            # after that, there's nothing left this driver wants to do.
            answer = {1: '1', 2: '6'}.get(state['menu_visits'], 'q')
            print(f'[one-click] {prompt}{answer}')
            return answer

        if _PRESS_ENTER_RE.match(prompt):
            return ''

        for pattern, (answer, reason) in answer_table:
            if pattern.match(prompt):
                if answer is not None:
                    print(f'[one-click] {prompt}{answer!r}')
                    return answer
                print()
                print(f'== Needs your input here: {reason} ==')
                return real_input(prompt)

        if prompt == state['last_prompt']:
            state['repeat_count'] += 1
            if state['repeat_count'] >= 3:
                raise AutomationStuck(
                    f'OpCore-Simplify asked the same prompt 3 times in a row '
                    f'with no recognized answer: {prompt!r}'
                )
        else:
            state['last_prompt'] = prompt
            state['repeat_count'] = 0

        print()
        print('== Unrecognized OpCore-Simplify prompt - answer this one yourself ==')
        return real_input(prompt)

    oc_utils.Utils.request_input = auto_request_input

    entry = os.path.join(tool_dir, 'OpCore-Simplify.py')
    spec = importlib.util.spec_from_file_location('opcore_simplify_main', entry)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    o = mod.OCPE()

    # select_macos_version()'s return value is a local inside OpCore-
    # Simplify's own main(), not stored on self anywhere - wrap the bound
    # method on this one instance to capture it instead of duplicating
    # OpCore-Simplify's own compatibility logic to re-derive it.
    original_select_macos_version = o.select_macos_version

    def _capture_macos_version(*args, **kwargs):
        result = original_select_macos_version(*args, **kwargs)
        picked['darwin_version'] = result
        return result

    o.select_macos_version = _capture_macos_version
    return o


def run_automated(tool_dir, hardware_report_path, acpi_tables_dir):
    """
    Drives OpCore-Simplify's real menu (OCPE.main()) end to end, answering
    its own prompts as described in this module's docstring. Returns the
    darwin macOS version string OpCore-Simplify actually built for (e.g.
    "23.99.99"), or None if nothing was built (quit before "6").
    """
    if tool_dir not in sys.path:
        sys.path.insert(0, tool_dir)

    picked = {}
    o = _install_auto_answers(tool_dir, hardware_report_path, acpi_tables_dir, picked)

    try:
        o.main()
    except SystemExit:
        pass
    except AutomationStuck as e:
        # main() aborted mid-flow, so its local state (hardware report,
        # version, kext selections) is gone - the only sane recovery is to
        # start the menu over. The same auto_request_input closure keeps
        # running, so the ordinary prompts before/after the stuck one are
        # still answered automatically; only that one now goes to a real
        # human the first time it's asked again, since repeat_count carries
        # forward. If it's still stuck, this is a real upstream change this
        # driver's prompt table needs updating for, not a transient glitch.
        print(f'\n{e}')
        print('Restarting OpenCore-Simplify\'s menu - the prompt above needs a real answer now.')
        try:
            o.main()
        except SystemExit:
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            print('\nOpCore-Simplify crashed (see traceback above) - no EFI was built.')
    except Exception:
        # A real crash inside OpCore-Simplify's own code (verified live: this
        # is exactly how it fails with no usable ACPI tables - see
        # dump_acpi_tables()'s docstring). Print it for diagnosis instead of
        # letting it kill this whole process with a raw traceback; the
        # caller already treats "nothing under Results/EFI" as "not built".
        import traceback
        traceback.print_exc()
        print('\nOpCore-Simplify crashed (see traceback above) - no EFI was built.')

    return picked.get('darwin_version')


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
    Generates a hardware report for this machine and drives OpCore-
    Simplify's real menu end to end with no human interaction needed for
    the ordinary case (see module docstring). Doesn't need a mounted disk -
    OpCore-Simplify builds into its own tool_dir/Results folder regardless.

    Returns (built_efi_dir, darwin_macos_version) - built_efi_dir is None
    if nothing was built (quit before finishing); darwin_macos_version is
    the version string OpCore-Simplify actually selected (None if it never
    got that far), so the caller can fetch the matching Recovery image
    without asking which version was picked a second time.
    """
    os.makedirs(workdir, exist_ok=True)
    report_path = os.path.join(workdir, 'hardware_report.json')
    hardware_report.write(report_path, prompt_for_motherboard=prompt_for_motherboard)
    print(f'Wrote hardware report to {report_path}')

    acpi_tables_dir = dump_acpi_tables(os.path.join(workdir, 'acpi'))
    if acpi_tables_dir:
        print(f'Dumped this machine\'s ACPI tables to {acpi_tables_dir}')
    else:
        raise SystemExit(
            'No ACPI dump available on this host - OpCore-Simplify hard-requires one '
            '(see this module\'s docstring). Run this from Windows or Linux instead.'
        )

    tool_dir = fetch_tool(workdir)
    darwin_version = run_automated(tool_dir, report_path, acpi_tables_dir)
    built = find_built_efi(tool_dir)
    if not built:
        print(f'No EFI folder found under {tool_dir}/Results - the build did not finish.')
    return built, darwin_version


def build(efi_mount_path, workdir, prompt_for_motherboard=True):
    """Convenience wrapper for when you already have a mounted EFI partition
    to write straight to: run() + copy_efi() in one call."""
    built, darwin_version = run(workdir, prompt_for_motherboard=prompt_for_motherboard)
    dest = copy_efi(built, efi_mount_path) if built else None
    return dest, darwin_version


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} <path where the EFI partition is mounted>')
        sys.exit(1)
    result, _darwin = build(sys.argv[1], os.path.join(os.getcwd(), 'opcore_simplify_work'))
    if result:
        print(f'EFI written to {result}')
