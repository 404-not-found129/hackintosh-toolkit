# Hackintosh EFI / Installer Builder

One file, [`install.py`](install.py), builds a Hackintosh USB installer end
to end on Windows or Linux (see below for why not macOS) - one-click by
default, no separate setup step: downloads macOS straight from Apple,
partitions a USB/SD card, and builds the OpenCore EFI using
[OpCore-Simplify](https://github.com/lzhoang2801/OpCore-Simplify) - a real,
actively-maintained community tool - rather than a from-scratch config
generator. This toolkit's own job is everything *around* that: generating
the hardware description and ACPI dumps OpCore-Simplify needs as input
(normally a Windows-only companion binary's job), driving OpCore-Simplify's
own menu with its own recommended defaults so nobody has to type them in by
hand, fetching macOS, partitioning, and the handful of things that still
need to happen afterward (USB port mapping, CPUFriend, iMessage's known
activation fixes).

Two kinds of question still stop and ask a real person, because there's no
safe default to fall back on: which GPU/WiFi/Bluetooth device to use when a
machine has more than one (picking wrong can mean no video output or no
WiFi), and whether to accept OpenCore Legacy Patcher's SIP/AMFI tradeoff on
hardware that needs it. Everything else - including the disk-wipe
confirmation, when exactly one USB/SD device is connected, and elevating to
root/Administrator - proceeds without a keypress (you'll still see the
normal sudo password prompt or Windows UAC dialog; this doesn't bypass
that). See "Automation and safety" below before you run this.

## One file, not one tool bolted onto many

`install.py` is a real Python source file, not an archive or a compiled
binary - open it in an editor and you'll find the individual modules listed
below embedded as plain, readable `r'''...'''` string blocks, byte-for-byte
identical to their standalone files (`build_install.py` generates it and
refuses to produce a file where that isn't true - see that script's own
docstring). Nothing is compressed, encoded, or hidden; you can audit
exactly what will run on a disk-partitioning tool before you run it, the
same as reading any of the individual `.py` files below.

On first run it extracts every module to a real `.py` file next to itself,
then imports and runs them exactly as this repository's own module layout
always has. That means the documented standalone follow-up commands
(`python3 install_efi.py`, `python3 usb_map.py ...`, `python3 cpufriend.py
...`, `python3 imessage.py --apply`) keep working afterward too, using
those same extracted files.

If you're working on this toolkit's source rather than just running it, use
the individual module files listed below as normal and run
`python3 build_install.py` afterward to regenerate `install.py` - don't
hand-edit the embedded copies inside it.

## What it does

1. **Builds a hardware report** describing this machine's CPU/GPU/
   motherboard/network/storage/etc. in the exact JSON schema
   OpCore-Simplify requires - schema reverse-engineered from its own
   `Scripts/report_validator.py`, and the generated report is verified
   against that real validator (zero errors/warnings) as well as against
   the live tool itself. OpCore-Simplify's own way of producing this file
   is a Windows-only binary ("Hardware Sniffer"); this is a cross-platform
   substitute, honest about the handful of fields (mainly motherboard name/
   chipset, and PCI IDs for USB controllers/input devices/sound codecs) that
   plain macOS genuinely doesn't expose on a Hackintosh, verified live
   rather than assumed (`hardware_report.py`).
2. **Dumps this machine's real ACPI tables** (Windows/Linux) - required,
   not optional: reading OpCore-Simplify's own source shows it calls
   `ensure_dsdt()` unconditionally right after you select a hardware
   report, and hard-crashes without one (verified live). Uses corpnewt's
   SSDTTime (MIT) for just this dump step (`opcore_simplify.py`).
3. **Drives OpCore-Simplify's own real menu end to end**, pre-loaded with
   that hardware report and ACPI dump - compatibility checking, macOS
   version selection, ACPI patch generation, kext selection, and SMBIOS
   model are all its own real, tested logic, not re-implemented here, just
   no longer typed in by hand. Answers are matched against the literal
   prompt text OpCore-Simplify's own source can produce (catalogued from
   all 54 of its `request_input()` call sites, not guessed), using its own
   suggested/recommended default wherever one exists. The two prompts with
   no safe default (multi-GPU/WiFi/BT device choice, the OpenCore Legacy
   Patcher SIP/AMFI tradeoff) still stop and wait for you; anything this
   driver's prompt table doesn't recognize at all also falls back to a
   real prompt instead of guessing (`opcore_simplify.py`).
4. **Fetches the Recovery/BaseSystem image for whichever macOS version
   OpCore-Simplify picked** - captured directly from its own menu, not
   asked again - straight from Apple's own Internet Recovery servers, same
   protocol a real Mac uses, nothing mirrored or redistributed
   (`macrecovery.py`).
5. **Auto-detects your USB/SD card**: if exactly one is already connected
   when you run this, it's used immediately with no prompt; otherwise it
   waits for you to plug one in (physically unavoidable) or asks you to
   pick, if several are connected at once. Only ever offers removable
   media, verified live against a real Hackintosh where internal NVMe
   drives report as "external" (a genuine quirk, no real ACPI/PCI
   "internal" marker exists on non-Mac boards) - detection is bus/
   transport-based, not the OS's own internal/external flag
   (`partition.py`).
6. **Partitions it and writes the BaseSystem image on** - EFI System
   Partition + a partition for the image. If exactly one USB/SD device is
   connected, wiping it is auto-confirmed after a 5-second countdown
   (Ctrl+C to abort) instead of typing a confirmation phrase back; with
   zero or multiple candidates it still asks, since there's no safe
   default for "which disk" (`partition.py`, `write_basesystem.py`).
7. **Copies OpCore-Simplify's EFI onto the EFI partition**, then patches it
   for iMessage: ROM becomes a real detected network adapter's MAC address
   (OpCore-Simplify's own SMBIOS generator uses a random one by default -
   confirmed by reading its current source, `"ROM": random_mac_address` -
   the same problem this toolkit's SMBIOS code used to have), and that
   adapter is marked `built-in` via a `DeviceProperties` patch where its
   PCI path is cheaply resolvable (`imessage.py`).
8. **Runs USB port mapping automatically on Linux** - a real one-shot map
   generated from `/sys/bus/usb`, no interaction needed. On Windows/macOS
   this step is skipped by default instead of asked, since USBToolBox (the
   tool it hands off to there) needs you to physically plug something into
   each port and click Build - not something a one-click run can do for
   you; re-run `usb_map.py` yourself afterward on those platforms
   (`usb_map.py`).
9. **Notes CPUFriend as a follow-up on detected laptops** - fixes CPU
   power-management data mismatches from a spoofed SMBIOS, mainly a
   battery-life/thermal issue. CPUFriend's own docs say most builds don't
   need it; this toolkit just prints the reminder at the end, since the
   data half (`CPUFriendDataProvider.kext`) can only be generated after
   you've booted this drive into macOS, its source data living inside the
   running OS (`cpufriend.py`).
10. **Logs the whole session** to `hackintosh_build/session_<timestamp>.log`
    so a failed run can be diagnosed after the fact.

Everything above runs before macOS itself is even installed - it only
builds the USB installer. One more step happens after, from the installed
macOS itself: **`install_efi.py`** copies the same built EFI onto the
*internal* target disk's own EFI System Partition (every GPT disk macOS's
own Disk Utility creates has one, whether or not Finder shows it) and
best-effort registers it as the default boot entry via `bless`, so the
machine can boot without the USB installer left plugged in forever. See
"After it finishes" below for exactly when to run it.

## The one real constraint this architecture has: run it from Windows or Linux

OpCore-Simplify's ACPI step is mandatory, and dumping real ACPI tables has
no macOS path (same platform limitation SSDTTime has everywhere else it's
used in this space - Apple doesn't expose raw ACPI tables the way Windows/
Linux do). Concretely: if you run `hackintosh_setup.py` from macOS, step 2
above prints a clear warning and returns nothing, and OpCore-Simplify's own
menu will loop once ("No valid .aml files were found!") and then
hard-crash the moment you select a hardware report - not a soft degradation,
a dead end. **Run this from Windows or Linux for the EFI-build stage.**
(USB mapping, CPUFriend, and the iMessage patch afterward all still work
fine cross-platform, including from macOS once you're at that point.
`install_efi.py` is the one follow-up step that's macOS-only in the other
direction - it needs the installed macOS itself to know which disk to
target, so there's nothing for it to do until you're there anyway.)

If you already have a real ACPI dump from this same physical machine's
Windows/Linux side, you can supply that by hand when OpCore-Simplify's menu
asks for it, even if you're running the rest of this from macOS.

## Prerequisites

- Python 3.8+ on the machine you run this on. That's it to get started -
  `install.py` elevates itself (see "Usage" below), so you don't need to
  remember `sudo`/"Run as Administrator" yourself.
- **Windows/Linux only**: [`dmg2img`](https://sourceforge.net/projects/dmg2img/)
  and a `dd` binary on `PATH` (macOS's `.dmg` format isn't natively
  writable outside macOS; this is the standard community workaround).
  - Linux: `sudo apt install dmg2img` (dd is already present)
  - Windows: `choco install dmg2img dd`
- Internet access (downloads from `osrecovery.apple.com`,
  `api.github.com`, and `github.com` release assets).
- **macOS only, for the USB mapping step**: `pip install pyobjc` (usb_map.py
  offers to do this for you when it launches USBToolBox).
- **Linux, for ACPI dumping**: `dmidecode` and `mokutil` if available (used
  for the hardware report's Motherboard/BIOS sections); ACPI dumping itself
  just reads `/sys/firmware/acpi/tables`, already accessible as root.

## Usage

Download just [`install.py`](install.py) (it's the whole toolkit - see
above) and run it the same way on any platform:

```bash
python3 install.py      # macOS/Linux
```

```powershell
python install.py       # Windows, from an ordinary (non-elevated) PowerShell
```

No `sudo`/"Run as Administrator" prefix needed - if it isn't already
elevated, it relaunches itself and you'll see the normal sudo password
prompt (macOS/Linux) or UAC dialog (Windows). On Windows that dialog opens
in a new console window; watch that one from here on.

It walks through hardware report + ACPI dump generation, driving OpCore-
Simplify's own menu automatically (fetching the matching Recovery image for
whichever macOS version it picked, with no need to ask which one), USB/SD
card auto-detection, partitioning, download, imaging, copying the EFI on,
the iMessage patch, and USB mapping (automatic on Linux). See "Automation
and safety" below for exactly which two situations still stop and ask you
something, and why.

## Automation and safety

This toolkit answers OpCore-Simplify's own menu prompts itself (see
`opcore_simplify.py`'s module docstring for the full list, each verified
against its actual source) rather than asking you to type them in. Two
kinds of question are deliberately left for a real person, because there's
no safe default:

- **Multiple GPU/WiFi/Bluetooth devices detected.** OpCore-Simplify's own
  menu has no "recommended" choice here - picking the wrong one can mean no
  video output or no WiFi/Bluetooth. Only comes up on hardware with more
  than one such device (most machines never see this).
- **Whether to proceed with OpenCore Legacy Patcher.** Needed on hardware
  OpCore-Simplify can't otherwise support on the macOS version you're
  targeting; it disables SIP/AMFI and means future macOS updates need a
  full installer rather than the usual incremental update. A real
  tradeoff, not a build-mechanics default.

Anything OpCore-Simplify's menu asks that isn't recognized at all also
falls back to asking you, rather than guessing - and if the exact same
unrecognized prompt repeats three times in a row, the build stops rather
than looping forever silently.

**The disk-wipe step is auto-confirmed only when there is exactly one
USB/SD device connected** - it prints the disk identifier, size, and label,
then waits 5 seconds (Ctrl+C to abort) before erasing it. With zero or more
than one candidate connected, it always falls back to picking/typing a
confirmation by hand, since there's no safe default for "which disk". If
you're running this somewhere multiple removable drives might be plugged
in, unplug everything except your actual target first.

### Re-running the follow-up steps later

```bash
python3 usb_map.py /Volumes/EFI/EFI
```

`install_efi.py` and `cpufriend.py` can *only* run once macOS is fully
installed and booted (not just Recovery) - `install_efi.py` because it's
putting OpenCore onto the disk that installed macOS now lives on,
`cpufriend.py` because its source data lives inside the running OS. Run
`install_efi.py` first, with the USB installer still plugged in (it's the
source it copies from):

```bash
sudo python3 install_efi.py                    # auto-detects the USB's EFI
sudo python3 install_efi.py /Volumes/EFI/EFI    # or point it at one explicitly
```

```bash
python3 cpufriend.py /Volumes/EFI/EFI
```

If iMessage/FaceTime don't activate after signing in, or you want to retry
after a failed attempt, run the documented cache cleanup from a Terminal in
the installed macOS (prints what it would remove by default; pass `--apply`
to actually remove it):

```bash
python3 imessage.py            # dry run
python3 imessage.py --apply    # actually clean up
```

## After it finishes

1. In the target machine's BIOS/UEFI: disable Secure Boot, disable CSM
   (UEFI-only boot), disable VT-d / Intel VMD if present.
2. Boot from the drive you just built, pick the OpenCore boot entry, then
   "macOS Base System" to reach Recovery.
3. In Recovery, use Disk Utility to erase your *actual* target disk as
   APFS, then reinstall macOS onto it from the Recovery menu.
4. Follow OpCore-Simplify's own "Before Using EFI" checklist it printed at
   the end of the build (BIOS requirements, USB mapping reminder).
5. If you skipped USB mapping earlier, run `usb_map.py` now, as shown
   above, from within Recovery or the installed system.
6. Once macOS is actually installed and booted (with the USB installer
   still plugged in), run `sudo python3 install_efi.py` to put OpenCore
   onto the internal disk itself - without this, the USB stays required
   for every future boot, since it's the only place OpenCore exists so
   far. It also tries to make this the default boot entry via `bless`; if
   that doesn't take on your board, set it as the default in the BIOS/UEFI
   setup menu instead (same screen from step 1).
7. Once that's done, you can boot without the USB. Run `cpufriend.py` if
   you want CPUFriend and confirmed `CPUFriend.kext` is present.
8. Sign into iMessage/FaceTime. If it doesn't activate, work through the
   checklist `imessage.py` printed after copying the EFI on - Apple ID
   history matters more than any config detail.

## Legal/safety notes

- Running macOS on non-Apple hardware is only permitted under Apple's EULA
  on Apple-branded hardware; running it on other hardware is a licensing
  gray area many enthusiasts accept the risk on, but it is **not
  Apple-sanctioned**. This toolkit only automates mechanics you could do by
  hand; it doesn't change that.
- Every download here comes from Apple's own servers or from the named
  open-source projects' official GitHub releases/repos - nothing is bundled
  or mirrored by this toolkit itself.
- The SMBIOS serial/MLB OpCore-Simplify generates is *valid-format*, not
  one Apple actually issued to a real Mac - fine for local use, but don't
  expect Apple's own lookup tools to recognise it, and note Apple can flag
  iCloud/iMessage activity from a generated identity.
- Disk partitioning is the one truly destructive step. With more than one
  USB/SD device connected it still asks you to type a confirmation phrase
  back - read the disk identifier and size carefully before doing so. With
  only one connected, that step is auto-confirmed after a 5-second
  countdown (see "Automation and safety" above) - make sure the only
  removable drive plugged in is actually the one you want erased.
