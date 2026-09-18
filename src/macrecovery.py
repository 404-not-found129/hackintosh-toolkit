#!/usr/bin/env python3
"""
Fetch a genuine macOS Recovery/BaseSystem image straight from Apple's own
Internet Recovery servers (osrecovery.apple.com) - the same protocol a real
Mac uses when it does Internet Recovery. No installer is redistributed or
mirrored anywhere here; every byte comes from Apple's CDN at download time.

Protocol reimplemented from the public, BSD-3-Clause "macrecovery" utility
that ships in Acidanthera's OpenCorePkg:
    https://github.com/acidanthera/OpenCorePkg/tree/master/Utilities/macrecovery
Board-id / MLB pairs below are the well-known public identifiers used by
that same community tooling to select a specific macOS release.

This is pure stdlib (urllib) and works identically on Windows, Linux and
macOS - only the *use* of the downloaded image differs per OS (see
write_basesystem.py).
"""

import hashlib
import os
import random
import shutil
import string
import struct
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

MLB_ZERO = '00000000000000000'

TYPE_SID = 16
TYPE_K = 64
TYPE_FG = 64

INFO_PRODUCT = 'AP'
INFO_IMAGE_LINK = 'AU'
INFO_IMAGE_HASH = 'AH'
INFO_IMAGE_SESS = 'AT'
INFO_SIGN_LINK = 'CU'
INFO_SIGN_HASH = 'CH'
INFO_SIGN_SESS = 'CT'
INFO_REQUIRED = [INFO_PRODUCT, INFO_IMAGE_LINK, INFO_IMAGE_HASH, INFO_IMAGE_SESS, INFO_SIGN_LINK, INFO_SIGN_HASH, INFO_SIGN_SESS]

# zhangyoufu https://gist.github.com/MCJack123/943eaca762730ca4b7ae460b731b68e7#gistcomment-3061078
APPLE_EFI_ROM_PUBKEY = 0xC3E748CAD9CD384329E10E25A91E43E1A762FF529ADE578C935BDDF9B13F2179D4855E6FC89E9E29CA12517D17DFA1EDCE0BEBF0EA7B461FFE61D94E2BDF72C196F89ACD3536B644064014DAE25A15DB6BB0852ECBD120916318D1CCDEA3C84C92ED743FC176D0BACA920D3FCF3158AFF731F88CE0623182A8ED67E650515F75745909F07D415F55FC15A35654D118C55A462D37A3ACDA08612F3F3F6571761EFCCBCC299AEE99B3A4FD6212CCFFF5EF37A2C334E871191F7E1C31960E010A54E86FA3F62E6D6905E1CD57732410A3EB0C6B4DEFDABE9F59BF1618758C751CD56CEF851D1C0EAA1C558E37AC108DA9089863D20E2E7E4BF475EC66FE6B3EFDCF

ChunkListHeader = struct.Struct('<4sIBBBxQQQ')
Chunk = struct.Struct('<I32s')

# Curated board-id / MLB pairs known to select a specific macOS release via
# Internet Recovery. "latest" os_type asks Apple for the newest build ever
# offered to that board; "default" asks for the build it originally shipped
# with. Apple occasionally retires very old boards - if one entry stops
# working, try RECENT_MAC ("Mac-27AD2F918AE68F61") with os_type=latest.
MACOS_VERSIONS = [
    {"name": "macOS High Sierra", "version": "10.13", "darwin": 17, "board_id": "Mac-7BA5B2D9E42DDD94", "mlb": "00000000000J80300", "os_type": "default"},
    {"name": "macOS Mojave",      "version": "10.14", "darwin": 18, "board_id": "Mac-7BA5B2DFE22DDD8C", "mlb": "00000000000KXPG00", "os_type": "default"},
    {"name": "macOS Catalina",    "version": "10.15", "darwin": 19, "board_id": "Mac-00BE6ED71E35EB86", "mlb": MLB_ZERO,          "os_type": "default"},
    {"name": "macOS Big Sur",     "version": "11",    "darwin": 20, "board_id": "Mac-2BD1B31983FE1663", "mlb": MLB_ZERO,          "os_type": "default"},
    {"name": "macOS Monterey",    "version": "12",    "darwin": 21, "board_id": "Mac-B809C3757DA9BB8D", "mlb": MLB_ZERO,          "os_type": "latest"},
    {"name": "macOS Ventura",     "version": "13",    "darwin": 22, "board_id": "Mac-4B682C642B45593E", "mlb": MLB_ZERO,          "os_type": "latest"},
    {"name": "macOS Sonoma",      "version": "14",    "darwin": 23, "board_id": "Mac-827FAC58A8FDFA22", "mlb": MLB_ZERO,          "os_type": "latest"},
    {"name": "macOS Sequoia",     "version": "15",    "darwin": 24, "board_id": "Mac-937A206F2EE63C01", "mlb": MLB_ZERO,          "os_type": "latest"},
    {"name": "macOS Tahoe",       "version": "26",    "darwin": 25, "board_id": "Mac-937CB26E2E02BB01", "mlb": MLB_ZERO,          "os_type": "latest"},
]


def version_by_darwin(darwin_major):
    return next((v for v in MACOS_VERSIONS if v["darwin"] == darwin_major), None)


def _run_query(url, headers, post=None, raw=False):
    data = None
    if post is not None:
        data = '\n'.join(f'{k}={v}' for k, v in post.items()).encode()
    req = Request(url=url, headers=headers, data=data)
    try:
        response = urlopen(req, timeout=30)
    except HTTPError as e:
        raise RuntimeError(
            f'Apple\'s server returned HTTP {e.code} for {url} - it may be temporarily down, or '
            f'this board-id may no longer be recognized. Try again, or pick a different macOS version.'
        ) from e
    except URLError as e:
        # HTTPError (above) is itself a URLError subclass, so this only ever
        # catches the no-connection case (DNS failure, no route, timeout) -
        # confirmed live, this used to surface as a raw traceback reading
        # "[Errno 8] nodename nor servname provided, or not known", which
        # means nothing to anyone who isn't already a Python programmer.
        raise RuntimeError(f'Could not reach {url} ({e.reason}) - check your internet connection and try again.') from e
    if raw:
        return response
    return dict(response.info()), response.read()


def _generate_id(length):
    return ''.join(random.choices(string.hexdigits[:16].upper(), k=length))


def _get_session():
    headers = {
        'Host': 'osrecovery.apple.com',
        'Connection': 'close',
        'User-Agent': 'InternetRecovery/1.0',
    }
    headers, _ = _run_query('http://osrecovery.apple.com/', headers)
    for header, value in headers.items():
        if header.lower() == 'set-cookie':
            for cookie in value.split('; '):
                if cookie.startswith('session='):
                    return cookie
    raise RuntimeError('Apple did not return a session cookie')


def _get_image_info(session, board_id, mlb, os_type):
    headers = {
        'Host': 'osrecovery.apple.com',
        'Connection': 'close',
        'User-Agent': 'InternetRecovery/1.0',
        'Cookie': session,
        'Content-Type': 'text/plain',
    }
    post = {
        'cid': _generate_id(TYPE_SID),
        'sn': mlb,
        'bid': board_id,
        'k': _generate_id(TYPE_K),
        'fg': _generate_id(TYPE_FG),
        'os': os_type,
    }
    _, output = _run_query('http://osrecovery.apple.com/InstallationPayload/RecoveryImage', headers, post)
    info = {}
    for line in output.decode('utf-8').split('\n'):
        if ': ' in line:
            key, value = line.split(': ', 1)
            info[key] = value
    missing = [k for k in INFO_REQUIRED if k not in info]
    if missing:
        raise RuntimeError(f'Apple response missing fields {missing} - board-id may be retired, try another version')
    return info


def _save_image(url, sess, filename, directory):
    purl = urlparse(url)
    headers = {
        'Host': purl.hostname,
        'Connection': 'close',
        'User-Agent': 'InternetRecovery/1.0',
        'Cookie': f'AssetToken={sess}',
    }
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    print(f'Downloading {os.path.basename(path)} ...')
    response = _run_query(url, headers, raw=True)
    total = int(dict(response.headers).get('Content-Length', -1))
    if total > 0:
        free = shutil.disk_usage(directory).free
        if free < total * 1.05:
            raise SystemExit(
                f'Not enough free space to download {os.path.basename(path)}: it is '
                f'{total / 2**30:.1f} GB but only {free / 2**30:.1f} GB is free at {directory}. '
                f'Free up space and try again - Apple already confirmed the exact size before any '
                f'of it was written, so this fails now instead of partway through the download.'
            )
    size = 0
    with open(path, 'wb') as fh:
        while True:
            chunk = response.read(2 ** 20)
            if not chunk:
                break
            fh.write(chunk)
            size += len(chunk)
            if total > 0:
                print(f'\r  {size / 2**20:.1f}/{total / 2**20:.1f} MB ({size * 100 // total}%)', end='', flush=True)
            else:
                print(f'\r  {size / 2**20:.1f} MB', end='', flush=True)
    print()
    return path


def _verify_chunklist(cnkpath):
    with open(cnkpath, 'rb') as f:
        hash_ctx = hashlib.sha256()
        data = f.read(ChunkListHeader.size)
        hash_ctx.update(data)
        magic, header_size, file_version, chunk_method, signature_method, chunk_count, chunk_offset, signature_offset = ChunkListHeader.unpack(data)
        if magic != b'CNKL' or header_size != ChunkListHeader.size or file_version != 1 or chunk_method != 1:
            raise RuntimeError('Unrecognised chunklist format')
        for _ in range(chunk_count):
            data = f.read(Chunk.size)
            hash_ctx.update(data)
            chunk_size, chunk_sha256 = Chunk.unpack(data)
            yield chunk_size, chunk_sha256
        digest = hash_ctx.digest()
        if signature_method == 1:
            data = f.read(256)
            signature = int.from_bytes(data, 'little')
            plaintext = int(f'0x1{"f" * 404}003031300d060960864801650304020105000420{"0" * 64}', 16) | int.from_bytes(digest, 'big')
            if pow(signature, 0x10001, APPLE_EFI_ROM_PUBKEY) != plaintext:
                raise RuntimeError('Chunklist signature verification failed - image may be corrupt/tampered')
        else:
            raise RuntimeError('Unexpected chunklist signature method')


def _verify_image(dmgpath, cnkpath):
    print('Verifying downloaded image against Apple chunklist...')
    with open(dmgpath, 'rb') as dmgf:
        for size, expected_hash in _verify_chunklist(cnkpath):
            chunk = dmgf.read(size)
            if len(chunk) != size or hashlib.sha256(chunk).digest() != expected_hash:
                raise RuntimeError('Chunk hash mismatch - re-download, the file is corrupt')
        if dmgf.read(1) != b'':
            raise RuntimeError('Downloaded image is larger than expected')
    print('Image verified OK.')


def download_recovery(version, outdir):
    """version: one entry from MACOS_VERSIONS. Returns path to the verified .dmg.

    Names the saved files after the requested Darwin version (not the fixed
    "BaseSystem.dmg"/"BaseSystem.chunklist" this used before) and reuses them
    across runs if they're already there and still verify - outdir is a
    fixed, across-run-reused path (hackintosh_setup.py's WORKDIR), and this
    exact user's own workflow already involves re-running the installer
    multiple times over several days while iterating on a build. A generic
    releases image is immutable once Apple publishes it for a given version,
    unlike OpCore-Simplify's own source (see net.py's fetch_repo_source_zip
    docstring for why *that* one is wiped fresh every run instead) - so
    caching this one is safe, and re-downloading a multi-hundred-MB-to-2GB+
    image on every retry when nothing about the macOS version choice changed
    is pure wasted time/bandwidth. Falls back to a fresh download whenever
    a cached copy is missing or fails verification - never trusts a cached
    file blindly.
    """
    cnkpath = os.path.join(outdir, f'BaseSystem-{version["darwin"]}.chunklist')
    dmgpath = os.path.join(outdir, f'BaseSystem-{version["darwin"]}.dmg')
    if os.path.isfile(cnkpath) and os.path.isfile(dmgpath):
        print(f'Found a previously-downloaded {version["name"]} image in {outdir} - verifying before reusing it...')
        try:
            _verify_image(dmgpath, cnkpath)
            print('Still verifies OK - skipping re-download.')
            return dmgpath
        except Exception as e:
            print(f'Cached copy failed verification ({e}) - downloading fresh instead.')

    print(f'Requesting {version["name"]} Internet Recovery image from Apple (board-id {version["board_id"]})...')
    session = _get_session()
    info = _get_image_info(session, version['board_id'], version['mlb'], version['os_type'])
    print(f'Apple offered product {info[INFO_PRODUCT]}')
    cnkpath = _save_image(info[INFO_SIGN_LINK], info[INFO_SIGN_SESS], f'BaseSystem-{version["darwin"]}.chunklist', outdir)
    dmgpath = _save_image(info[INFO_IMAGE_LINK], info[INFO_IMAGE_SESS], f'BaseSystem-{version["darwin"]}.dmg', outdir)
    _verify_image(dmgpath, cnkpath)
    return dmgpath


if __name__ == '__main__':
    print('Available macOS versions:')
    for i, v in enumerate(MACOS_VERSIONS, 1):
        print(f'  {i}. {v["name"]} ({v["version"]})')
    choice = int(input('Pick a version to test-download: ')) - 1
    download_recovery(MACOS_VERSIONS[choice], os.path.join(os.getcwd(), 'recovery_download'))
