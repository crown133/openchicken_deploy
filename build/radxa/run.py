"""Run the isolated build and export an unsigned manual-deployment bundle."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parents[2]
WORKSPACE = PROJECT.parent
IMAGE = 'openchicken-radxa-build:rust1.94.1'
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True, help='New build directory under workspace artifacts/')
parser.add_argument('--image-ready', action='store_true', help='Use the already built local image')
parser.add_argument('--cache-from', type=Path, help='Reuse cargo and target directories from a previous artifacts build')
args = parser.parse_args()
output = args.output.resolve()
assert output.is_relative_to(WORKSPACE / 'artifacts'), output
output.mkdir(parents=True, exist_ok=True)
assert not (output / 'source').exists(), 'Use a new output directory; never overwrite a build snapshot'
logs = output / 'logs'
logs.mkdir(exist_ok=True)
if not args.image_ready:
    with (logs / 'image-build.log').open('w') as log:
        subprocess.run(['docker', 'build', '--platform', 'linux/amd64', '-t', IMAGE,
                        '-f', str(PROJECT / 'build/radxa/Dockerfile'),
                        str(PROJECT / 'build/radxa')], stdout=log, stderr=subprocess.STDOUT, check=True)
image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', IMAGE]))[0]
(output / 'build-image.json').write_text(json.dumps(image, indent=2) + '\n')
source = output / 'source'
shutil.copytree(PROJECT, source, symlinks=True,
                ignore=shutil.ignore_patterns('.git', '.codex', '.agents', '.aws', 'target', '__pycache__', '.pytest_cache'))
rows = []
for path in sorted(source.rglob('*')):
    if path.is_symlink():
        assert path.resolve().is_relative_to(source), path
        rows.append({'path': str(path.relative_to(source)), 'symlink': str(path.readlink())})
    elif path.is_file():
        rows.append({'path': str(path.relative_to(source)), 'size': path.stat().st_size,
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
encoded = json.dumps(rows, indent=2).encode() + b'\n'
(output / 'source-manifest.json').write_bytes(encoded)
revision = 'local-' + hashlib.sha256(encoded).hexdigest()[:16]
command = ['docker', 'run', '--rm', '--name', 'openchicken-radxa-compile',
           '--platform', 'linux/amd64', '--user', f'{os.getuid()}:{os.getgid()}',
           '--cpus', '6', '--memory', '12g', '--pids-limit', '512',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--read-only',
           '--tmpfs', '/tmp:rw,nosuid,size=2g',
           '--mount', f'type=bind,src={source},dst=/src,readonly',
           '--mount', f'type=bind,src={output},dst=/work',
           '-e', f'OPENCHICKEN_REVISION={revision}',
           '-e', f'OPENCHICKEN_BUILD_TIME={datetime.now(timezone.utc).isoformat()}',
           image['Id'], 'sh', '/src/build/radxa/build.sh']
if args.cache_from:
    previous = args.cache_from.resolve()
    assert previous.is_relative_to(WORKSPACE / 'artifacts'), previous
    for folder in ['cargo', 'target']:
        assert (previous / folder).is_dir(), previous / folder
        (output / folder).mkdir()
        command[2:2] = ['--mount', f'type=bind,src={previous / folder},dst=/work/{folder}']
(output / 'build-command.json').write_text(json.dumps(command, indent=2) + '\n')
print(f'Building source {revision}; logs: {logs}', flush=True)
with (logs / 'build.log').open('w') as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
print(f'Build exit code: {result.returncode}; output: {output / "output"}', flush=True)
raise SystemExit(result.returncode)
