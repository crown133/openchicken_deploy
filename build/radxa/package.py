"""Package compiled ARM64 binaries for manual deployment, without install hooks."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile

SOURCE = Path('/src')
WORK = Path('/work')
RELEASE = WORK / 'target/aarch64-unknown-linux-gnu/release'
NAMES = ['robotd', 'robotctl', 'updaterd', 'configd', 'btd', 'padd', 'netpadd',
         'mediad', 'sounds', 'pet-detect', 'pet-features', 'tofd', 'nfcd']
version = re.search(r'\[workspace.package\]\s*version\s*=\s*"([^"]+)"',
                    (SOURCE / 'Cargo.toml').read_text()).group(1)
name = f'openchicken-{version}-radxa-arm64'
bundle = WORK / 'output' / name
bundle.mkdir(exist_ok=False)
(bundle / 'bin').mkdir()
reports = []
for name in NAMES:
    source = RELEASE / name
    header = source.read_bytes()[:20]
    assert header[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', header, 18)[0] == 183, source
    target = bundle / 'bin' / name
    shutil.copy2(source, target)
    subprocess.run(['aarch64-linux-gnu-strip', '--strip-debug', str(target)], check=True)
    dynamic = subprocess.check_output(['aarch64-linux-gnu-readelf', '-d', str(target)], text=True)
    versions = subprocess.check_output(['aarch64-linux-gnu-readelf', '--version-info', str(target)], text=True)
    glibc = sorted(set(re.findall(r'GLIBC_(\d+\.\d+(?:\.\d+)?)', versions)),
                   key=lambda v: tuple(map(int, v.split('.'))))
    assert not glibc or tuple(map(int, glibc[-1].split('.'))) <= (2, 41), (name, glibc)
    reports.append({'binary': name, 'machine': 'AArch64',
                    'needed': re.findall(r'\(NEEDED\).*?\[(.*?)\]', dynamic),
                    'required_glibc': glibc[-1] if glibc else None})

for source, dest in [
    ('pet-detect/models/pet_detect.onnx', 'models/pet_detect.onnx'),
    ('openchicken-control/models/pickup_detector.onnx', 'models/pickup_detector.onnx'),
    ('deploy/robotd.toml', 'reference/robotd.toml'),
    ('LICENSE', 'LICENSE'),
]:
    target = bundle / dest
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / source, target)
for folder in ['robotd', 'updater', 'configd', 'btd', 'padd', 'netpadd', 'mediad', 'tof', 'nfc']:
    for source in (SOURCE / folder / 'systemd').glob('*.service'):
        target = bundle / 'reference/systemd' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

(bundle / 'runtime-dependencies.json').write_text(json.dumps(reports, indent=2) + '\n')
metadata = {'version': version, 'target': 'aarch64-unknown-linux-gnu',
            'robot_os_record': 'Armbian 26.8.1 / Debian 13 trixie, Radxa ZERO 3 RK3566',
            'rust': subprocess.check_output(['rustc', '--version'], text=True).strip(),
            'revision': os.environ.get('OPENCHICKEN_REVISION', 'unknown'),
            'build_time': os.environ.get('OPENCHICKEN_BUILD_TIME', 'unknown'),
            'signed_updater_release': False, 'robot_tested': False,
            'install_hooks': False, 'binaries': NAMES}
(bundle / 'build-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
shutil.copy2(SOURCE / 'README.md', bundle / 'README.md')
for filename in ['packages.tsv', 'rust-targets.txt', 'source-manifest.json',
                 'target-debs.sha256', 'target-package-metadata.txt', 'target-download-uris.txt']:
    shutil.copy2(WORK / filename, bundle / filename)
files = sorted(path for path in bundle.rglob('*') if path.is_file())
(bundle / 'SHA256SUMS').write_text(''.join(
    f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(bundle)}\n' for p in files))
archive = bundle.parent / (bundle.name + '.tar.gz')
with tarfile.open(archive, 'w:gz') as tar:
    tar.add(bundle, arcname=bundle.name)
archive.with_name(archive.name + '.sha256').write_text(
    f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
print(archive)
