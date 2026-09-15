# Hackintosh EFI / Installer Builder

A cross-platform (Windows / Linux / macOS) toolkit that automates the parts
of building a Hackintosh USB installer that *can* be safely automated, and
is explicit and honest about the parts that can't.

## What it does

1. **Detects your CPU/GPU** and works out which macOS versions have native,
   accelerated graphics on your hardware (`gpu_compat.py`).
2. **Lets you pick a macOS version** - either one your GPU natively
   supports, or you can explicitly force *any* version with no
   acceleration (basic display only).
3. **Downloads the real macOS Recovery/BaseSystem image straight from
   Apple's own Internet Recovery servers** (`macrecovery.py`) - this is the
   same protocol a real Mac uses; nothing is mirrored or redistributed.
4. **Partitions a disk you choose** into an EFI System Partition + a target
   partition (`partition.py`) - gated behind a strict typed confirmation.
5. **Writes the BaseSystem image** onto the target partition
   (`write_basesystem.py`).
6. **Builds a real OpenCore EFI** - downloads OpenCorePkg + Lilu/VirtualSMC/
   WhateverGreen/AppleALC/RestrictEvents (and NVMeFix, if this machine has
   NVMe storage) straight from Acidanthera's GitHub releases and assembles a
   working `config.plist` (`opencore_build.py`), then runs a structural
   sanity check on the result (bootloader present, every referenced kext
   actually in `OC/Kexts`, Lilu loads first, an SMBIOS identity is set) so a
   broken build is caught here, not at boot.
7. **Detects your Bluetooth controller and patches it automatically** -
   classifies it as a genuine Apple internal module (native, nothing to do -
   verified live on a real Hackintosh with an Apple-sourced BCM4350 card), a
   Broadcom RAMUSB USB device (stages the exact BrcmPatchRAM kext
   combination for your target macOS version - the version boundaries and
   kext names are exactly as documented in BrcmPatchRAM's own README, not
   inferred), or something else (Intel combo Bluetooth gets a printed
   advisory instead of a guess, since its Hackintosh support is
   inconsistent) (`hw_detect.get_bluetooth_controllers()`,
   `bluetooth_compat.py`).
8. **Detects your WiFi controller and patches it automatically** - a
   Broadcom card gets AirportBrcmFixup staged (safe even if it's already
   natively supported, since it only patches what needs patching); an Intel
   card - which has zero native macOS driver, ever, since real Macs never
   shipped one - gets OpenIntelWireless's itlwm.kext staged, with a printed
   note that you connect via the HeliPort menu-bar app rather than the
   normal WiFi menu (the alternative, AirportItlwm.kext, integrates with
   System Preferences properly but needs an OpenCore Kernel->Force config
   sourced from a live macOS install after first boot - not something this
   can safely template pre-boot, so it's mentioned as a manual upgrade path
   rather than attempted). Anything else (Atheros, Realtek, MediaTek) is
   flagged rather than guessed at (`hw_detect.get_wifi_controllers()`,
   `wifi_compat.py`).
9. **Generates a real SMBIOS identity** - serial, MLB, system UUID and ROM
   for whatever Mac model you choose, using Acidanthera's own `macserial`
   generator bundled in every OpenCorePkg release (`smbios.py`). Fully
   automatic - no hardware-specific input needed for this one.
10. **Generates ACPI SSDT patches automatically, on Windows/Linux** - dumps
    *this machine's* real ACPI tables and non-interactively generates the
    five standard, deterministic patches (SSDT-PLUG CPU power management,
    SSDT-EC fake embedded controller, SSDT-AWAC/RTC0 clock fix, SSDT-HPET IRQ
    fix, SSDT-USBX power properties) by driving corpnewt's SSDTTime as a
    library, then copies the results into `EFI/OC/ACPI/Add` and registers
    them in `config.plist` (`acpi_patches.py`).
11. **Generates a USB port map automatically, on Linux** - reads this
    machine's real USB controller/port topology straight from `/sys/bus/usb`
    and writes a data-only `USBMap.kext` using Apple's own stock
    `AppleUSBHostMergeProperties` driver (no third-party kext binary needed),
    registers it in `config.plist`, and turns off the blunt `XhciPortLimit`
    fallback (`usb_map.py`).
12. **Detects laptop vs. desktop automatically** via battery presence
    (`hw_detect.is_laptop()`) to pre-fill the ACPI step's fake-EC choice -
    you're still asked, but with the right default instead of a blind guess.
13. **Warns if the target disk looks too small** (under ~14 GiB) before you
    confirm the wipe, instead of failing partway through the BaseSystem
    write.
14. **Logs the whole session** to `hackintosh_build/session_<timestamp>.log`
    so a failed run can be diagnosed after the fact.
15. **Supports CPUFriend for laptops** - fixes CPU power-management data
    mismatches from a spoofed SMBIOS (mainly a battery-life/thermal issue).
    On a detected laptop, `hackintosh_setup.py` offers to stage
    `CPUFriend.kext`; the actual data half, `CPUFriendDataProvider.kext`,
    can only be generated *after* you've successfully booted this drive into
    macOS (its source data lives inside the running OS itself), so run
    `cpufriend.py` then. Off by default even on laptops - CPUFriend's own
    docs say most builds don't need it (`cpufriend.py`).
16. **Sets up the real, documented iMessage/iCloud activation checklist** -
    following [Dortania's iServices guide](https://github.com/dortania/OpenCore-Post-Install/blob/master/universal/iservices.md)
    exactly: `smbios.py`'s ROM is now a real detected network adapter's
    actual MAC address (verified live - it was previously random bytes,
    which the guide is explicit does not work), that same adapter is marked
    `built-in` via a `DeviceProperties` patch when its PCI path can be
    cheaply resolved (bus-0 devices on Linux/Windows; prints Hackintool
    fallback guidance otherwise), and `smbios.generate_candidates()` can
    produce several serials at once for manually checking against
    [Check Coverage](https://checkcoverage.apple.com/) - deliberately not
    scripted, since that's a normal Apple web form and scripting it would
    cross into automated querying the guide itself warns can get
    rate-limited. `imessage.py` also ships the guide's documented
    cache/preference cleanup as a standalone, non-destructive-by-default
    script for retrying after a failed attempt (`imessage.py`, tested live -
    correctly found this machine's real iMessage state without deleting
    anything in dry-run mode).
17. **Auto-detects your USB/SD installer target** - `install.sh` (macOS/
    Linux) handles finding python3 and re-running with sudo; disk selection
    itself snapshots connected removable media, asks you to plug in the
    drive, and diffs the list to find it, rather than asking you to know its
    device identifier up front (`partition.list_removable_disks()`,
    `install.sh`).

## Why these two aren't 100% automatic everywhere

Both `acpi_patches.py` and `usb_map.py` do real, non-interactive generation
where the platform allows it - see above - rather than just handing you a
menu. Two genuine limits remain, inherited from the underlying mechanisms
rather than introduced by this toolkit:

- **ACPI dumping has no macOS path.** SSDTTime itself only knows how to
  read raw ACPI tables on Windows/Linux; Apple doesn't expose them the same
  way. On macOS, `acpi_patches.py` falls back to SSDTTime's real interactive
  menu (bring an existing dump if you have one from a Windows/Linux boot of
  the *same* machine - ACPI tables are hardware, not OS-specific).
- **A USB port map generated from a single snapshot only knows about ports
  with something plugged in right now** (or, for Linux, guesses a generic
  type for the rest from the controller's real port count). A verified,
  per-port walk needs a live human plugging a device into each physical
  port one at a time - there's no way around that step for full precision.
  `usb_map.py` still automates it as far as it can: on Linux it generates a
  real map from live data with no interaction; on Windows/macOS - where
  USBToolBox (the tool that does the per-port walk) is actually better
  supported than on Linux - it fetches and launches that tool for you and
  wires its output into the EFI afterward.

Either way, `opencore_build.py` leaves you with a working baseline if you
skip both: `XhciPortLimit` is enabled until `usb_map.py` replaces it, and no
ACPI patches are assumed - most reasonably modern desktop boards boot fine
without any SSDTs at all, they just won't have things like native CPU power
management tuned until you run `acpi_patches.py`.

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

You will be walked through hardware detection, macOS version selection,
disk selection, download, partitioning, imaging, EFI generation, an SMBIOS
model choice, and then two yes/no prompts offering to generate the ACPI
patches and USB port map right then and there (recommended if you're
running this directly on the target machine - both need this machine's
real hardware).

Disk selection auto-detects your target: it snapshots currently-connected
USB/SD media, asks you to plug in the drive, and diffs the list to find it -
no need to know its device identifier up front. This only ever offers
removable media (verified live against a real Hackintosh: internal NVMe
drives there report as "external" to macOS - a known quirk with no real
ACPI/PCI "internal" marker on non-Mac boards - so detection is bus/
transport-based, not the OS's own internal/external flag). If nothing
removable is detected, it falls back to the full disk list with a clear
warning that internal drives are now included. Either way, the destructive
step is still gated behind the same typed disk-identifier + `ERASE`
confirmation - auto-detection only speeds up *finding* the right disk.

To run either step later instead - e.g. from the macOS Base System once
this drive can boot to Recovery - point them at the EFI partition directly:

```bash
python3 acpi_patches.py /Volumes/EFI/EFI            # add --laptop on laptops
python3 usb_map.py /Volumes/EFI/EFI
```

`cpufriend.py` (laptops, opt-in) is different: it can *only* run once macOS
is fully installed and booted (not just Recovery), since its source data
lives inside the running OS. Once you're there:

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
4. If you skipped the ACPI/USB steps earlier, run `acpi_patches.py` /
   `usb_map.py` now, as shown above, from within that Recovery environment.
5. Once macOS is actually installed and booted, run `cpufriend.py` if you
   staged CPUFriend earlier.
6. Sign into iMessage/FaceTime. If it doesn't activate, work through the
   checklist `opencore_build.py` printed during the build (also in
   `imessage.py`) before assuming something is broken - Apple ID history
   matters more than any config detail.

## Why the GPU compatibility numbers might be off

Intel integrated-GPU rules are ported from device-ID logic used by the
[OpCore-Simplify](https://github.com/lzhoang2801/OpCore-Simplify) project
and are reliable. Discrete AMD/NVIDIA support is matched by *GPU name
substring* rather than exact PCI device ID, because a byte-accurate table
is a large, constantly-updated moving target maintained by Acidanthera/
WhateverGreen and OpenCore Legacy Patcher. Treat it as a strong hint, not
gospel - the "force no acceleration" override is always available as a
fallback regardless of what the checker concludes.

## Legal/safety notes

- Running macOS on non-Apple hardware is only permitted under Apple's EULA
  on Apple-branded hardware; running it on other hardware is a licensing
  gray area many enthusiasts accept the risk on, but it is **not
  Apple-sanctioned**. This toolkit only automates mechanics you could do by
  hand; it doesn't change that.
- Every download here comes from Apple's own servers or from the named
  open-source projects' official GitHub releases/repos - nothing is bundled
  or mirrored by this toolkit itself.
- The SMBIOS serial/MLB `smbios.py` generates is *valid-format*, not one
  Apple actually issued to a real Mac - fine for local use, but don't
  expect Apple's own lookup tools to recognise it, and note Apple can flag
  iCloud/iMessage activity from a generated identity.
- Disk partitioning is the one truly destructive step. Read the disk
  identifier and size the tool prints back to you carefully before typing
  the confirmation phrase.
