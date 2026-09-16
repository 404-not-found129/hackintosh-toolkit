# Hackintosh EFI / Installer Builder

A cross-platform (Windows / Linux / macOS) toolkit that builds a Hackintosh
USB installer end to end: downloads macOS straight from Apple, partitions a
USB/SD card you plug in, and builds the OpenCore EFI using
[OpCore-Simplify](https://github.com/lzhoang2801/OpCore-Simplify) - a real,
actively-maintained community tool - rather than a from-scratch config
generator. This toolkit's own job is everything *around* that: generating
the hardware description and ACPI dumps OpCore-Simplify needs as input
(normally a Windows-only companion binary's job), fetching macOS,
partitioning, and the handful of things that still need to happen
afterward (USB port mapping, CPUFriend, iMessage's known activation fixes).

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
3. **Hands off to OpCore-Simplify's own interactive tool**, pre-loaded with
   that hardware report and ACPI dump - compatibility checking, macOS
   version selection, ACPI patch generation, kext selection, and SMBIOS
   model are all its own real, tested logic, not re-implemented here
   (`opcore_simplify.py`).
4. **Fetches the Recovery/BaseSystem image for whichever macOS version you
   picked**, straight from Apple's own Internet Recovery servers - same
   protocol a real Mac uses, nothing mirrored or redistributed
   (`macrecovery.py`).
5. **Auto-detects your USB/SD card**: snapshots connected removable media,
   asks you to plug in the target, and diffs the list to find it - no
   need to know its device identifier up front. Only ever offers removable
   media, verified live against a real Hackintosh where internal NVMe
   drives report as "external" (a genuine quirk, no real ACPI/PCI
   "internal" marker exists on non-Mac boards) - detection is bus/
   transport-based, not the OS's own internal/external flag
   (`partition.py`, `install.sh`).
6. **Partitions it and writes the BaseSystem image on** - EFI System
   Partition + a partition for the image, gated behind a strict typed
   confirmation before anything is erased (`partition.py`,
   `write_basesystem.py`).
7. **Copies OpCore-Simplify's EFI onto the EFI partition**, then patches it
   for iMessage: ROM becomes a real detected network adapter's MAC address
   (OpCore-Simplify's own SMBIOS generator uses a random one by default -
   confirmed by reading its current source, `"ROM": random_mac_address` -
   the same problem this toolkit's SMBIOS code used to have), and that
   adapter is marked `built-in` via a `DeviceProperties` patch where its
   PCI path is cheaply resolvable (`imessage.py`).
8. **Offers USB port mapping** - a real one-shot map generated from
   `/sys/bus/usb` with no interaction on Linux, or a hand-off to USBToolBox
   on Windows/macOS (the platforms it's actually better supported on)
   (`usb_map.py`).
9. **Offers to remind you about CPUFriend** on detected laptops - fixes
   CPU power-management data mismatches from a spoofed SMBIOS, mainly a
   battery-life/thermal issue. Off by default even there - CPUFriend's own
   docs say most builds don't need it. The data half
   (`CPUFriendDataProvider.kext`) can only be generated after you've
   booted this drive into macOS, since its source data lives inside the
   running OS (`cpufriend.py`).
10. **Logs the whole session** to `hackintosh_build/session_<timestamp>.log`
    so a failed run can be diagnosed after the fact.

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
fine cross-platform, including from macOS once you're at that point.)

If you already have a real ACPI dump from this same physical machine's
Windows/Linux side, you can supply that by hand when OpCore-Simplify's menu
asks for it, even if you're running the rest of this from macOS.

## Prerequisites

- Python 3.8+ on the machine you run this on.
- **Windows/Linux only**: [`dmg2img`](https://sourceforge.net/projects/dmg2img/)
  and a `dd` binary on `PATH` (macOS's `.dmg` format isn't natively
  writable outside macOS; this is the standard community workaround).
  - Linux: `sudo apt install dmg2img` (dd is already present)
  - Windows: `choco install dmg2img dd`
- Run as **root/Administrator** - disk partitioning requires it.
- Internet access (downloads from `osrecovery.apple.com`,
  `api.github.com`, and `github.com` release assets).
- **macOS only, for the USB mapping step**: `pip install pyobjc` (usb_map.py
  offers to do this for you when it launches USBToolBox).
- **Linux, for ACPI dumping**: `dmidecode` and `mokutil` if available (used
  for the hardware report's Motherboard/BIOS sections); ACPI dumping itself
  just reads `/sys/firmware/acpi/tables`, already accessible as root.

## Usage

macOS/Linux - `install.sh` handles finding python3 and re-running with sudo:

```bash
./install.sh
```

Or run it directly the same way `install.sh` does:

```bash
sudo python3 hackintosh_setup.py
```

On Windows, run from an elevated PowerShell (no `install.sh` equivalent):

```powershell
python hackintosh_setup.py
```

You'll be walked through: hardware report + ACPI dump generation, a
hand-off to OpCore-Simplify's own menu (see below for exactly what to click
through), which macOS version you picked (so the matching Recovery image
can be fetched), USB/SD card auto-detection, partitioning, download,
imaging, copying the EFI on, the iMessage patch, and then optional USB
mapping / CPUFriend reminder.

### What to do inside OpCore-Simplify's menu

This toolkit prints the exact paths to paste at each prompt, but the shape
of it:

1. It asks "Do you want to skip the update process? (yes/No)" first - a
   zip download has no git history for it to diff against, so its own
   update check can never succeed here. Type `yes`.
2. Choose **"1. Select Hardware Report"** and paste the path printed above
   your terminal.
3. It will then ask for an ACPI Tables folder - paste the second path
   printed (or see the constraint above if none was generated).
4. From here it's OpCore-Simplify's own real, tested logic: confirm/adjust
   the macOS version, ACPI patches, kext selection, and SMBIOS model as you
   prefer - nothing here is second-guessed by this toolkit.
5. Choose **"6. Build OpenCore EFI"**, then quit (`Q`) once it finishes.

### Re-running the follow-up steps later

```bash
python3 usb_map.py /Volumes/EFI/EFI
```

`cpufriend.py` can *only* run once macOS is fully installed and booted (not
just Recovery), since its source data lives inside the running OS:

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
6. Once macOS is actually installed and booted, run `cpufriend.py` if you
   want CPUFriend and confirmed `CPUFriend.kext` is present.
7. Sign into iMessage/FaceTime. If it doesn't activate, work through the
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
- Disk partitioning is the one truly destructive step. Read the disk
  identifier and size the tool prints back to you carefully before typing
  the confirmation phrase.
