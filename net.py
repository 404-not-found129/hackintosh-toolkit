#!/usr/bin/env python3
"""Small shared HTTP helpers used by opencore_build.py, smbios.py, acpi_patches.py, usb_map.py, cpufriend.py."""

import json
import os
import shutil
import sys
import zipfile
from urllib.request import Request, urlopen

GITHUB_API_LATEST_RELEASE = 'https://api.github.com/repos/{repo}/releases/latest'


def isolate_module_cache(*names):
    """
    Several corpnewt tools (SSDTTime, CPUFriendFriend, ...) each ship their
    own same-named local package called "Scripts". Python's import system
    caches modules by name in sys.modules regardless of which sys.path
    directory they were found under, so if this process already imported one
    tool's "Scripts" package and then adds a second tool's directory to
    sys.path, `import Scripts` silently returns the FIRST tool's cached
    package instead of the second tool's - wrong code runs with no error.

    Call this to evict a tool's top-level module name and any dotted
    submodules of it (e.g. "Scripts", "Scripts.utils") from sys.modules
    right before importing that tool fresh, so it always loads its own copy.
    """
    for name in names:
        for key in [k for k in sys.modules if k == name or k.startswith(name + '.')]:
            del sys.modules[key]


def api_get(url):
    req = Request(url, headers={'User-Agent': 'hackintosh-toolkit', 'Accept': 'application/vnd.github+json'})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def download(url, dest):
    # This exact bug (missing parent dir) has bitten three separate call
    # sites so far - fixing it here once rather than at every caller.
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    req = Request(url, headers={'User-Agent': 'hackintosh-toolkit'})
    with urlopen(req, timeout=60) as resp, open(dest, 'wb') as f:
        shutil.copyfileobj(resp, f)
    return dest


def fetch_latest_release_zip(repo, workdir, name_must_contain=('RELEASE',), name_must_not_contain=('DEBUG', 'SOURCE')):
    os.makedirs(workdir, exist_ok=True)
    release = api_get(GITHUB_API_LATEST_RELEASE.format(repo=repo))
    for asset in release.get('assets', []):
        name = asset['name'].upper()
        if not name.endswith('.ZIP'):
            continue
        if name_must_contain and not any(tok in name for tok in name_must_contain):
            continue
        if any(tok in name for tok in name_must_not_contain):
            continue
        dest = os.path.join(workdir, asset['name'])
        print(f'Downloading {asset["name"]} ({asset["size"] / 1024:.0f} KB)...')
        download(asset['browser_download_url'], dest)
        return dest
    raise RuntimeError(f'Could not find a RELEASE zip asset for {repo} (tag {release.get("tag_name")})')


def fetch_repo_source_zip(owner_repo, workdir, branch='master'):
    """Downloads a GitHub repo's source as a zip (no git required) and extracts it.
    Returns the path to the extracted top-level folder."""
    os.makedirs(workdir, exist_ok=True)
    url = f'https://github.com/{owner_repo}/archive/refs/heads/{branch}.zip'
    name = owner_repo.split('/')[-1]
    zip_path = os.path.join(workdir, f'{name}-{branch}.zip')
    print(f'Downloading {owner_repo}@{branch} source...')
    download(url, zip_path)
    extract_dir = os.path.join(workdir, f'{name}-src')
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    entries = os.listdir(extract_dir)
    if len(entries) == 1:
        return os.path.join(extract_dir, entries[0])
    return extract_dir


def extract_zip(zip_path, into):
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(into)
    return into


def find_first(root, predicate):
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            if predicate(path):
                return path
    for base, dirs, _files in os.walk(root):
        for name in dirs:
            path = os.path.join(base, name)
            if predicate(path):
                return path
    return None
