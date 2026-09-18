#!/usr/bin/env python3
"""Small shared HTTP helpers used by opcore_simplify.py, usb_map.py, cpufriend.py."""

import json
import os
import shutil
import sys
import zipfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GITHUB_API_LATEST_RELEASE = 'https://api.github.com/repos/{repo}/releases/latest'


def _friendly_url_error(e, url):
    """A raw urllib.error.HTTPError/URLError left uncaught surfaces as a
    Python traceback with text like "[Errno 8] nodename nor servname
    provided" - confirmed live, not obviously "no internet" to anyone who
    isn't already a Python programmer. Turns the common, actionable cases
    (GitHub's low anonymous rate limit, no internet/DNS failure) into a
    message that says what's actually wrong and what to do about it."""
    if isinstance(e, HTTPError):
        if e.code == 403:
            return (f'GitHub returned 403 (rate limited) fetching {url} - unauthenticated GitHub '
                     f'requests are capped at a low hourly limit. Wait a while and try again.')
        if e.code == 404:
            return f'{url} returned 404 (not found) - it may have moved or been renamed upstream.'
        return f'{url} returned HTTP {e.code}.'
    return f'Could not reach {url} ({e.reason}) - check your internet connection and try again.'


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
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        raise RuntimeError(_friendly_url_error(e, url)) from e


def download(url, dest):
    # This exact bug (missing parent dir) has bitten three separate call
    # sites so far - fixing it here once rather than at every caller.
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    req = Request(url, headers={'User-Agent': 'hackintosh-toolkit'})
    try:
        with urlopen(req, timeout=60) as resp, open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f)
    except URLError as e:
        raise RuntimeError(_friendly_url_error(e, url)) from e
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
    Returns the path to the extracted top-level folder.

    workdir is a fixed path this toolkit reuses across separate runs (see
    hackintosh_setup.py's WORKDIR) rather than a fresh one per run, so a
    stale extract_dir from an earlier run can already be sitting there -
    confirmed live, still present in this exact repo from earlier runs this
    same session. zf.extractall() only adds/overwrites paths that exist in
    the zip being extracted now; it never deletes a file that existed in an
    older run's zip but was since renamed/removed upstream (this tool
    fetches "main"/"master" fresh every time - a moving target, not a
    pinned release). Wiping extract_dir first guarantees what ends up on
    disk always matches exactly what's actually in the zip just downloaded,
    not a merge of that with whatever an older run happened to leave there."""
    os.makedirs(workdir, exist_ok=True)
    url = f'https://github.com/{owner_repo}/archive/refs/heads/{branch}.zip'
    name = owner_repo.split('/')[-1]
    zip_path = os.path.join(workdir, f'{name}-{branch}.zip')
    print(f'Downloading {owner_repo}@{branch} source...')
    download(url, zip_path)
    extract_dir = os.path.join(workdir, f'{name}-src')
    shutil.rmtree(extract_dir, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    entries = os.listdir(extract_dir)
    if len(entries) == 1:
        return os.path.join(extract_dir, entries[0])
    return extract_dir


def extract_zip(zip_path, into):
    """See fetch_repo_source_zip()'s docstring for why `into` is wiped
    first - same reused-workdir staleness risk applies here."""
    shutil.rmtree(into, ignore_errors=True)
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
