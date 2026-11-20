"""Build exclusively from an allowlist; live data is never copied into staging."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    'app.py', 'app_paths.py', 'app_updates.py', 'updates_ui.py', 'launcher.py',
    'bot_likes.py', 'converter.py', 'group_search.py', 'parser_ui.py',
    'runtime_support.py', 'telegram_runtime.py', 'tgcrypto.py', 'ui_theme.py',
    'update_control.py', 'user_parser.py', 'version.json',
    'requirements.txt', 'converter_requirements.txt', 'requirements-build.txt',
    'start.bat', 'build_exe.bat', 'build_setup.bat', 'README.txt', 'README.md',
    '.gitignore', '.github/workflows/release.yml',
    'packaging/build_release.py', 'packaging/installer.iss',
    'packaging/apply_update.ps1', 'packaging/publish_release.ps1',
    'assets/sphere-3d-360.gif',
)


def source_files():
    return sorted(set(SOURCE_FILES) | {p.relative_to(ROOT).as_posix() for p in (ROOT / 'tests').glob('test_*.py')})


def stage_source(destination):
    if destination.exists():
        raise ValueError('Staging must be a new, empty directory')
    destination.mkdir(parents=True)
    for name in source_files():
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing source or unsafe symbolic link: {name}')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return destination


def find_compiler(override):
    candidates = [override, os.environ.get('ISCC'), shutil.which('ISCC')]
    for variable, suffix in [('ProgramFiles(x86)', 'Inno Setup 6/ISCC.exe'),
                             ('ProgramFiles', 'Inno Setup 7/ISCC.exe'),
                             ('LOCALAPPDATA', 'Programs/Inno Setup 6/ISCC.exe')]:
        if os.environ.get(variable):
            candidates.append(str(Path(os.environ[variable]) / suffix))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise SystemExit('Inno Setup not found. Install Inno Setup 6/7 or pass --iscc path/to/ISCC.exe.')


def create_icon(path):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGBA', (256, 256), '#08110d')
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 10, 246, 246), radius=44, outline='#75f68b', width=8)
    draw.ellipse((44, 28, 212, 196), outline='#294333', width=3)
    draw.ellipse((86, 28, 170, 196), outline='#294333', width=3)
    draw.line((44, 112, 212, 112), fill='#294333', width=3)
    font_path = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'consolab.ttf'
    font = ImageFont.truetype(str(font_path), 104)
    draw.text((128, 133), 'TM', anchor='mm', fill='#75f68b', font=font, stroke_width=1)
    image.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def run(arguments, cwd):
    subprocess.run([str(value) for value in arguments], cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'release')
    parser.add_argument('--iscc')
    parser.add_argument('--stage-only', action='store_true')
    parser.add_argument('--exe-only', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix='build-', dir=output))
    stage = stage_source(build / 'source')
    config = json.loads((stage / 'version.json').read_text('utf-8'))
    sys.path.insert(0, str(stage))
    from app_updates import validate_repository, version_tuple
    version_tuple(config['version'])
    validate_repository(config['repository'])
    manifest = {name: hashlib.sha256((stage / name).read_bytes()).hexdigest() for name in source_files()}
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2), 'utf-8')
    with zipfile.ZipFile(output / 'TelegramManager-source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in source_files():
            archive.write(stage / name, name)
    print(f'Clean source: {stage}', flush=True)
    if args.stage_only:
        return
    compiler = None if args.exe_only else find_compiler(args.iscc)
    create_icon(build / 'app.ico')
    parts = tuple(version_tuple(config['version'])) + (0,)
    version_info = ("VSVersionInfo(ffi=FixedFileInfo(filevers=" + repr(parts) + ", prodvers=" + repr(parts) +
        ", mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0,0)), kids=["
        "StringFileInfo([StringTable('040904b0', [StringStruct('FileDescription', 'Telegram Manager'), "
        "StringStruct('ProductName', 'Telegram Manager'), StringStruct('FileVersion', '" + config['version'] +
        "'), StringStruct('ProductVersion', '" + config['version'] +
        "')])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])")
    (build / 'version_info.txt').write_text(version_info, 'utf-8')
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed', '--noupx',
         '--name', 'TelegramManager', '--icon', build / 'app.ico', '--version-file', build / 'version_info.txt',
         '--distpath', build / 'dist', '--workpath', build / 'work', '--specpath', build,
         '--paths', stage, '--collect-all', 'opentele2', '--copy-metadata', 'telethon',
         '--copy-metadata', 'cryptg', '--copy-metadata', 'Pillow',
         '--add-data', f'{stage / "assets"};assets', '--add-data', f'{stage / "version.json"};.',
         '--add-data', f'{stage / "packaging" / "apply_update.ps1"};.', stage / 'launcher.py'], stage)
    app = build / 'dist' / 'TelegramManager'
    # Offline check uses isolated data and never opens the live accounts directory.
    env = dict(os.environ, TELEGRAM_MANAGER_DATA_DIR=str(build / 'test-data'))
    report = build / 'self-test.json'
    result = subprocess.run([str(app / 'TelegramManager.exe'), '--self-test', str(report)],
                            env=env, cwd=app, timeout=120)
    if result.returncode or not report.is_file() or not json.loads(report.read_text('utf-8')).get('ok'):
        raise SystemExit('Frozen application self-test failed; installer was not created.')
    if not args.exe_only:
        run([compiler, f'/DAppVersion={config["version"]}', f'/DAppSource={app}',
             f'/DOutputPath={output}', stage / 'packaging' / 'installer.iss'], stage)
        setup = output / 'setup.exe'
        digest = hashlib.sha256()
        with setup.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        checksum = digest.hexdigest()
        (output / 'setup.exe.sha256').write_text(f'{checksum}  setup.exe\n', 'ascii')
    print(f'Build complete: {output}', flush=True)


if __name__ == '__main__':
    main()
