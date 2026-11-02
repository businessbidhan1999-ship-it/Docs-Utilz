"""Package standalone binaries and installers into release archives."""
from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / 'dist/release'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    packages = []
    for platform, binary in (('windows', 'Docs-Utilz.exe'), ('linux', 'Docs-Utilz')):
        folder = RELEASE / platform
        if not (folder / binary).is_file():
            raise FileNotFoundError(f'Build {platform} before packaging the release.')
        manifest = RELEASE / 'build-manifest.json'
        common = {'build-manifest.json': manifest} if manifest.is_file() else {}
        installers = ({name: ROOT / 'packaging' / name for name in
                       ('Install.cmd', 'install_windows.ps1', 'uninstall_windows.ps1')}
                      if platform == 'windows' else {'install.sh': ROOT / 'packaging/install_linux.sh'})
        for name, source in (common | installers).items():
            shutil.copy2(source, folder / name)
        files = [binary, *common, *installers]
        if platform == 'linux':
            shutil.copy2(ROOT / 'packaging/icon.png', folder / 'icon.png')
            files.append('icon.png')
        (folder / 'SHA256SUMS').write_text('\n'.join(f'{digest(folder / name)}  {name}' for name in files) + '\n')
        files.append('SHA256SUMS')
        package = RELEASE / f'Docs-Utilz-2.1.1-{platform.title()}.zip'
        # Only these release files belong in the ZIP, never nearby office documents/settings.
        with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in files:
                archive.write(folder / name, f'{package.stem}/{name}')
        with zipfile.ZipFile(package) as archive:
            if archive.testzip() is not None:
                raise RuntimeError(f'ZIP verification failed: {package.name}')
        packages.append(package)
        print(f'{package.name}: ZIP entries verified')
    (RELEASE / 'SHA256SUMS').write_text('\n'.join(f'{digest(path)}  {path.name}' for path in packages) + '\n')


if __name__ == '__main__':
    main()
