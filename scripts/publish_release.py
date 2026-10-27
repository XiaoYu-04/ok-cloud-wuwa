"""Publish the built plugin to GitHub Releases.

Creates the tag/release for manifest.json's version and uploads the archive twice:
once as .okscript (what ok-ww's own exporter produces) and once as .zip (a plain
archive, same bytes) so the install instructions can offer either.

Auth uses the git credential helper, so no token needs to live in this file.
"""

import json
import mimetypes
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'cloud_wuwa' / 'manifest.json'
DIST = ROOT / 'dist'
OWNER = 'XiaoYu-04'
REPO = 'ok-cloud-wuwa'
API = 'https://api.github.com'


def git_token():
    out = subprocess.run(
        ['git', 'credential', 'fill'],
        input='protocol=https\nhost=github.com\n\n',
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith('password='):
            return line[len('password='):]
    raise SystemExit('no GitHub password/token in the git credential helper')


def api(token, method, url, payload=None, raw=None, content_type=None):
    data = raw
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'ok-wuwa-cloud-release',
    }
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    elif content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
    return json.loads(body) if body else None


def main():
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8-sig'))
    version = manifest['version']
    tag = f'v{version}'

    archive = DIST / f"cloud_wuwa-{version}.okscript"
    if not archive.exists():
        raise SystemExit(f'missing {archive}; run scripts/build_okscript.py first')

    token = git_token()

    try:
        release = api(token, 'POST', f'{API}/repos/{OWNER}/{REPO}/releases', {
            'tag_name': tag,
            'target_commitish': 'main',
            'name': f'云鸣潮 {version}',
            'body': (ROOT / 'RELEASE_NOTES.md').read_text(encoding='utf-8'),
            'draft': False,
            'prerelease': False,
        })
        print(f'created release {tag} (id={release["id"]})')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')
        raise SystemExit(f'release create failed: {exc.code} {detail}')

    # .zip is the same zip bytes under a name GitHub users expect; ok-ww accepts both.
    plain_zip = DIST / f'cloud_wuwa-{version}.zip'
    shutil.copyfile(archive, plain_zip)

    upload_base = f'https://uploads.github.com/repos/{OWNER}/{REPO}/releases/{release["id"]}/assets'
    for path in (archive, plain_zip):
        ctype = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        asset = api(
            token,
            'POST',
            f'{upload_base}?name={path.name}',
            raw=path.read_bytes(),
            content_type=ctype,
        )
        print(f'uploaded {asset["name"]} ({asset["size"]} bytes)')


if __name__ == '__main__':
    sys.exit(main())
