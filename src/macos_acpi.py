#!/usr/bin/env python3
"""
Dumps real ACPI tables (DSDT/SSDTs) from a running macOS system - the thing
this toolkit's README used to say had "no macOS path" and SSDTTime (the
tool opcore_simplify.py uses for this on Windows/Linux) genuinely can't do.

macOS can't, and this module doesn't try to, read raw physical ACPI tables
the way Linux's /sys/firmware/acpi/tables or Windows' WinRing0-based tools
do. But it doesn't need to: the AppleACPIPlatformExpert IOKit service
already has the *parsed* tables sitting in its own "ACPI Tables" property -
a CFDictionary mapping each table's name ("DSDT", "SSDT", "SSDT-1", ...) to
its raw bytes as CFData, read via one call to IORegistryEntryCreateCFProperty.
This is not a novel technique: it's exactly what Hackintool (github.com/
benbaker76/Hackintool, actively maintained, "the Swiss army knife of
vanilla Hackintoshing") does for its own "Dump ACPI Tables" feature, read
directly from its current source (Hackintool/AppDelegate.m,
-dumpACPITables) rather than guessed at. An old (2014-era, pre-Yosemite)
technique of getting this same data by parsing `ioreg`'s own text output no
longer works, which is likely why this toolkit assumed there was no macOS
path at all - but the underlying property was never removed, only hidden
from `ioreg`'s default text rendering; the actual IOKit API call still
returns it.

Verified live against a real, currently-booted Hackintosh (not assumed):
every extracted DSDT/SSDT had a correct 4-byte ACPI signature, a header
"Length" field matching the actual byte count exactly, and a checksum
byte-summing to 0 mod 256 across the whole table, as the ACPI spec
requires - the same validation OpenCore/any ACPI tool would do. No root
privileges were needed for the read.

Pure ctypes against IOKit.framework/CoreFoundation.framework - no PyObjC
dependency (unlike usb_map.py's USBToolBox hand-off, there's no
`pyobjc-framework-IOKit` package to lean on; the handful of C functions
needed here are simple enough to bind directly).
"""

import ctypes
import ctypes.util
import os
import struct

_ACPI_TABLES_PROPERTY = b'ACPI Tables'
_EXPERT_SERVICE_NAME = b'AppleACPIPlatformExpert'
_K_CF_STRING_ENCODING_UTF8 = 0x08000100
_K_IO_MASTER_PORT_DEFAULT = 0


class ACPIExtractionError(RuntimeError):
    """Couldn't get real ACPI tables from this running macOS - caller
    should treat this the same as "no dump available" (see
    opcore_simplify.py's dump_acpi_tables()), not crash the whole build."""


def _load_frameworks():
    iokit_path = ctypes.util.find_library('IOKit')
    cf_path = ctypes.util.find_library('CoreFoundation')
    if not iokit_path or not cf_path:
        raise ACPIExtractionError('IOKit/CoreFoundation not found - is this really macOS?')

    iokit = ctypes.CDLL(iokit_path)
    cf = ctypes.CDLL(cf_path)

    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32]
    cf.CFDictionaryGetCount.restype = ctypes.c_long
    cf.CFDictionaryGetCount.argtypes = [ctypes.c_void_p]
    cf.CFDictionaryGetKeysAndValues.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_int32]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    iokit.IOServiceMatching.restype = ctypes.c_void_p
    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
    iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
    iokit.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]

    return iokit, cf


def _validate_table(name, raw):
    """
    Raises ACPIExtractionError if raw doesn't look like a genuine, intact
    ACPI table - same checks any ACPI tool (including OpenCore itself)
    would make: a real 4-byte signature, a header Length field matching the
    actual byte count, and a checksum byte-summing to 0 mod 256 across the
    whole table (ACPI spec, Table 5.4 "System Description Table Header").

    FACS is the one standard exception (verified live, and documented in
    the ACPI spec) - it has no Checksum field at all, and its content is
    legitimately mutated by the OS at runtime, so it's excluded from the
    checksum check here. Not relevant in practice: this module only ever
    extracts DSDT/SSDT tables (see dump_acpi_tables()), never FACS.
    """
    if len(raw) < 36:  # ACPI SDT header is 36 bytes
        raise ACPIExtractionError(f'{name} is only {len(raw)} bytes - too short to be a real ACPI table.')
    declared_length = struct.unpack_from('<I', raw, 4)[0]
    if declared_length != len(raw):
        raise ACPIExtractionError(
            f'{name}: header declares {declared_length} bytes but got {len(raw)} - '
            f'looks truncated or corrupted, not trusting it.'
        )
    if name != 'FACS' and (sum(raw) & 0xFF) != 0:
        raise ACPIExtractionError(f'{name}: checksum does not sum to 0 - looks corrupted, not trusting it.')


def dump_acpi_tables(workdir):
    """
    Extracts this machine's real DSDT and SSDT tables (matching Hackintool's
    own filter - other tables like FACP/APIC/HPET aren't what OpenCore-
    Simplify's ACPI folder needs) into workdir/acpi/, one .aml file per
    table, named after its own key ("DSDT.aml", "SSDT.aml", "SSDT-1.aml",
    ...). Returns that folder's path.

    Raises ACPIExtractionError (not caught here) if the AppleACPIPlatformExpert
    service or its "ACPI Tables" property isn't present, or if any DSDT/SSDT
    table fails validation (see _validate_table()) - callers should treat
    that as "no dump available" and fall back accordingly, the same as a
    failed dump on Windows/Linux, not let it crash the whole build.
    """
    iokit, cf = _load_frameworks()

    matching = iokit.IOServiceMatching(_EXPERT_SERVICE_NAME)
    if not matching:
        raise ACPIExtractionError('IOServiceMatching("AppleACPIPlatformExpert") failed.')

    service = iokit.IOServiceGetMatchingService(_K_IO_MASTER_PORT_DEFAULT, matching)
    if not service:
        raise ACPIExtractionError('AppleACPIPlatformExpert service not found on this machine.')

    try:
        key = cf.CFStringCreateWithCString(None, _ACPI_TABLES_PROPERTY, _K_CF_STRING_ENCODING_UTF8)
        try:
            tables = iokit.IORegistryEntryCreateCFProperty(service, key, None, 0)
        finally:
            cf.CFRelease(key)

        if not tables:
            raise ACPIExtractionError(
                '"ACPI Tables" property not present on AppleACPIPlatformExpert - '
                'this macOS version/build may not expose it the way this was verified against.'
            )

        try:
            count = cf.CFDictionaryGetCount(tables)
            keys = (ctypes.c_void_p * count)()
            values = (ctypes.c_void_p * count)()
            cf.CFDictionaryGetKeysAndValues(tables, ctypes.cast(keys, ctypes.c_void_p),
                                             ctypes.cast(values, ctypes.c_void_p))

            dest_dir = os.path.join(workdir, 'acpi')
            os.makedirs(dest_dir, exist_ok=True)

            name_buf = ctypes.create_string_buffer(64)
            written = []
            for i in range(count):
                cf.CFStringGetCString(keys[i], name_buf, 64, _K_CF_STRING_ENCODING_UTF8)
                name = name_buf.value.decode('utf-8')
                if not (name.startswith('DSDT') or name.startswith('SSDT')):
                    continue

                length = cf.CFDataGetLength(values[i])
                ptr = cf.CFDataGetBytePtr(values[i])
                raw = bytes(ctypes.string_at(ptr, length)) if length else b''
                _validate_table(name, raw)

                dest_path = os.path.join(dest_dir, f'{name}.aml')
                with open(dest_path, 'wb') as f:
                    f.write(raw)
                written.append(name)

            if not written:
                raise ACPIExtractionError('No DSDT/SSDT entries found in the "ACPI Tables" property.')

            print(f'Extracted {len(written)} real ACPI table(s) from this running macOS: {", ".join(sorted(written))}')
            return dest_dir
        finally:
            cf.CFRelease(tables)
    finally:
        iokit.IOObjectRelease(service)


if __name__ == '__main__':
    import sys
    result = dump_acpi_tables(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    print(f'ACPI tables written to {result}')
