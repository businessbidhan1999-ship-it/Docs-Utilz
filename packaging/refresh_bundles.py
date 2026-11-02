"""Refresh app code in existing unsigned PyInstaller bundles, retaining their runtimes.

This offline path needs the exact Python minor version used by the bundle and
PyInstaller 6.x. Full clean builds remain available via the platform scripts.
Only project modules and tkinter.font change; bundled drivers/libraries do not.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.archive.writers import CArchiveWriter, ZlibArchiveWriter
from PyInstaller.building.utils import replace_filename_in_code_object

ROOT = Path(__file__).resolve().parent.parent
MODULES = ('app', 'engine', 'tools', 'cli', 'devices', 'ui_theme')


def refresh(template: Path, output: Path) -> dict:
    archive = CArchiveReader(str(template))
    with template.open('rb') as source:
        source.seek(archive._end_offset - CArchiveWriter._COOKIE_LENGTH)
        cookie = struct.unpack(CArchiveWriter._COOKIE_FORMAT, source.read(CArchiveWriter._COOKIE_LENGTH))
    pyvers = sys.version_info.major * 100 + sys.version_info.minor
    if cookie[4] != pyvers:
        raise RuntimeError(f'{template.name} requires Python {cookie[4] // 100}.{cookie[4] % 100}; current runtime differs.')
    pyz_name = next(name for name, entry in archive.toc.items() if entry[-1] == 'z')
    pyz = archive.open_embedded_archive(pyz_name)
    # Marshal is version-sensitive. Refuse before decoding any code objects.
    pyz_header = archive.extract(pyz_name)[:8]
    if pyz_header[4:8] != importlib.util.MAGIC_NUMBER:
        raise RuntimeError('Python bytecode magic differs from the template runtime.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        scratch = Path(temporary)
        codes, entries = {}, {}
        for name, (kind, _, _) in pyz.toc.items():
            if kind == 3:  # namespace package
                entries[name] = (name, None, 'PYMODULE')
            else:
                codes[name] = pyz.extract(name)
                entries[name] = (name, '__init__.py' if kind == 1 else name + '.py', 'PYMODULE')
        sources = {f'docs_utilz.{name}': ROOT / 'docs_utilz' / (name + '.py') for name in MODULES}
        sources['docs_utilz'] = ROOT / 'docs_utilz/__init__.py'
        sources['tkinter.font'] = Path(sys.base_prefix) / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/tkinter/font.py'
        if not sources['tkinter.font'].is_file():
            import tkinter.font
            sources['tkinter.font'] = Path(tkinter.font.__file__)
        for name, path in sources.items():
            codes[name] = compile(path.read_bytes(), name.replace('.', '/') + '.py', 'exec')
            entries[name] = (name, str(path), 'PYMODULE')
        new_pyz = scratch / 'PYZ.pyz'
        ZlibArchiveWriter(str(new_pyz), list(entries.values()), codes)
        app_code = compile((ROOT / 'docs_utilz/__main__.py').read_bytes(), 'app.py', 'exec')
        app_code = replace_filename_in_code_object(app_code, 'app.py')
        entrypoint = '__main__' if '__main__' in archive.toc else 'app'
        replacements = {entrypoint: marshal.dumps(app_code), pyz_name: new_pyz.read_bytes()}
        package = scratch / 'updated.pkg'
        table = []
        with template.open('rb') as source, package.open('wb') as dest:
            for name, (old_offset, old_length, data_length, compressed, kind) in archive.toc.items():
                offset = dest.tell()
                if name in replacements:
                    data = replacements[name]
                    data_length = len(data)
                    encoded = zlib.compress(data, 9) if compressed else data
                    dest.write(encoded)
                    length = len(encoded)
                else:
                    source.seek(archive._start_offset + old_offset)
                    remaining = old_length
                    while remaining:
                        block = source.read(min(remaining, 1024 * 1024))
                        if not block:
                            raise RuntimeError('Template archive is truncated.')
                        dest.write(block)
                        remaining -= len(block)
                    length = old_length
                table.append((offset, length, data_length, compressed, kind, name))
            for option in archive.options:
                table.append((dest.tell(), 0, 0, 0, 'o', option))
            toc_offset = dest.tell()
            toc = CArchiveWriter._serialize_toc(table)
            dest.write(toc)
            dest.write(struct.pack(CArchiveWriter._COOKIE_FORMAT, cookie[0],
                                   dest.tell() + CArchiveWriter._COOKIE_LENGTH,
                                   toc_offset, len(toc), cookie[4], cookie[5]))
        built = scratch / output.name
        if template.suffix.lower() == '.exe':
            with template.open('rb') as source, built.open('wb') as dest:
                dest.write(source.read(archive._start_offset))
                with package.open('rb') as data:
                    shutil.copyfileobj(data, dest)
        else:
            subprocess.run(['objcopy', '--update-section', f'pydata={package}',
                            str(template), str(built)], check=True)
            built.chmod(0o755)
        verify = CArchiveReader(str(built))
        embedded = verify.open_embedded_archive(pyz_name)
        if any(name not in embedded.toc for name in sources) or verify.extract(entrypoint) != replacements[entrypoint]:
            raise RuntimeError('Updated archive verification failed.')
        # Every untouched runtime entry must match, including Tk, Pillow and PDF libraries.
        for name in archive.toc:
            if name not in replacements and archive.extract(name) != verify.extract(name):
                raise RuntimeError(f'Unexpected runtime change: {name}')
        os.replace(built, output)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(template=str(template.relative_to(ROOT)), template_sha256=digest(template),
                output=str(output.relative_to(ROOT)), output_sha256=digest(output),
                python_version=f'{sys.version_info.major}.{sys.version_info.minor}',
                sources={str(path.relative_to(ROOT)): digest(path) for path in sources.values()},
                method='Offline code refresh; original native runtime entries verified byte-for-byte')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outdir', type=Path, default=ROOT / 'dist/release')
    args = parser.parse_args()
    manifests = []
    for platform, filename in (('linux', 'Docs-Utilz'), ('windows', 'Docs-Utilz.exe')):
        template = ROOT / 'dist/build-templates' / platform / filename
        if not template.is_file():
            template = ROOT / 'dist' / platform / filename
        manifests.append(refresh(template, args.outdir / platform / filename))
        print(f'Updated {platform} bundle and verified unchanged runtime entries.')
    (args.outdir / 'build-manifest.json').write_text(json.dumps(manifests, indent=2) + '\n')


if __name__ == '__main__':
    main()
