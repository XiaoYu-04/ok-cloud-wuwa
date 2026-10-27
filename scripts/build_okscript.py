"""Build the installable .okscript archive for the 云鸣潮 plugin.

Run with the ok-ww bundled python:
    python scripts/build_okscript.py
"""

import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "cloud_wuwa")
DIST = os.path.join(ROOT, "dist")

FILES = ["manifest.json", "cloud_wuwa_tab.py"]


def build():
    manifest_path = os.path.join(SRC, "manifest.json")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    os.makedirs(DIST, exist_ok=True)
    name = f"{manifest['file_name']}-{manifest['version']}.okscript"
    target = os.path.join(DIST, name)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in FILES:
            archive.write(os.path.join(SRC, filename), filename)

    print(f"built {target} ({os.path.getsize(target)} bytes)")
    return target


def verify(path):
    import sys

    working = r"C:\Apps\Local\ok-ww\data\apps\ok-ww\working"
    sys.path.insert(0, working)

    from ok.core.script_packager import validate_plugin_manifest, validate_archive

    with zipfile.ZipFile(path) as archive:
        ok, message = validate_archive(archive)
        if not ok:
            raise SystemExit(f"archive rejected: {message}")
        with archive.open("manifest.json") as handle:
            manifest = json.load(handle)

    validate_plugin_manifest(manifest)
    print(f"manifest ok: {manifest['script_name']} {manifest['version']} tabs={manifest['custom_tabs']}")


if __name__ == "__main__":
    verify(build())
