#!/usr/bin/env python3
"""Build a registered local runtime; installation never overwrites an app."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import json
import os
import plistlib
import platform
import shutil
import subprocess
import sys
import uuid

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from audio_transcribe.product import APP_VERSION, APP_BUILD
from audio_transcribe.config import load_settings

parser = argparse.ArgumentParser()
parser.add_argument('--install', action='store_true')
parser.add_argument('--name', default='AudioTranscribe')
parser.add_argument('--bundle-id', default='local.audio-transcribe.desktop')
parser.add_argument('--icon', type=Path, help='Optional local PNG icon; never copied into the source tree.')
parser.add_argument('--settings-path', type=Path, help='Isolated validation settings; omitted for normal managed data.')
parser.add_argument('--install-dir', type=Path, default=Path.home()/'Applications')
args = parser.parse_args()
if not args.name or Path(args.name).name != args.name or args.name in {'.','..'}:
    parser.error('Use a single application filename.')
vendor = root/'native/vendor'
binaries = list((vendor/'imageio_ffmpeg/binaries').glob('ffmpeg-macos-*'))
if len(binaries) != 1:
    raise SystemExit('The existing pinned media runtime is required; no runtime installation was attempted.')
ffmpeg = binaries[0]
version = subprocess.run([str(ffmpeg),'-version'],capture_output=True,text=True,check=True).stdout.splitlines()[0]
(root/'native/media-runtime.json').write_text(json.dumps(dict(package='imageio-ffmpeg',package_version='0.6.0',
    relative_path=str(ffmpeg.relative_to(root)),sha256=hashlib.sha256(ffmpeg.read_bytes()).hexdigest(),version=version),indent=2)+'\n')
app = root/'native/build'/(args.name+'.app')
contents = app/'Contents'
(contents/'MacOS').mkdir(parents=True,exist_ok=True)
(contents/'Resources').mkdir(exist_ok=True)
icon_master = args.icon.expanduser().resolve() if args.icon else root/'native/build/AppIcon-master.png'
iconset = root/'native/build/AppIcon.iconset'
if args.icon is None:
    subprocess.run(['/usr/bin/xcrun', 'swift', str(root/'scripts/make-icon.swift'), str(icon_master)], check=True)
if not icon_master.is_file():
    raise SystemExit('The requested icon file is missing.')
iconset.mkdir(parents=True,exist_ok=True)
for point_size, scale in [(16,1),(16,2),(32,1),(32,2),(128,1),(128,2),(256,1),(256,2),(512,1),(512,2)]:
    pixels = point_size * scale
    suffix = '@2x' if scale == 2 else ''
    target = iconset/f'icon_{point_size}x{point_size}{suffix}.png'
    subprocess.run(['/usr/bin/sips','-s','format','png','-z',str(pixels),str(pixels),
                    str(icon_master),'--out',str(target)],check=True,capture_output=True)
subprocess.run(['/usr/bin/iconutil','-c','icns',str(iconset),'-o',
                str(contents/'Resources/AppIcon.icns')],check=True,capture_output=True)
extensions=['wav','wave','m4a','mp3','flac','aac','aiff','aif','aifc','ogg','oga','opus','mp4','mov']
metadata=dict(CFBundleName=args.name,CFBundleDisplayName=args.name,CFBundleIdentifier=args.bundle_id,
    CFBundleVersion=APP_BUILD,CFBundleShortVersionString=APP_VERSION,CFBundleExecutable='AudioTranscribe',
    CFBundlePackageType='APPL',CFBundleIconFile='AppIcon.icns',LSMinimumSystemVersion='13.0',NSHighResolutionCapable=True,
    NSPrincipalClass='NSApplication',AudioTranscribeCodeRoot=str(root),
    CFBundleDocumentTypes=[dict(CFBundleTypeName='Audio and audio-bearing media',CFBundleTypeRole='Viewer',
        LSHandlerRank='Alternate',CFBundleTypeExtensions=extensions)],
    NSHumanReadableCopyright='Local AudioTranscribe utility. Audio stays on this Mac.')
if args.settings_path:
    settings=args.settings_path.expanduser().resolve()
    if not settings.is_file(): raise SystemExit('Validation settings do not exist.')
    metadata['AudioTranscribeSettingsPath']=str(settings)
metadata['AudioTranscribeLogRoot']=load_settings(args.settings_path)['roots']['log']
(contents/'Info.plist').write_bytes(plistlib.dumps(metadata))
swift=sorted(p for p in (root/'native').glob('*.swift') if not p.stem.endswith('Tests') and p.name!='main.swift')
swift.append(root/'native/main.swift')
subprocess.run(['/usr/bin/xcrun','swiftc','-swift-version','5','-O','-target',platform.machine()+'-apple-macos13.0',
    '-module-cache-path',str(root/'native/build/module-cache'),'-framework','Cocoa','-framework','UniformTypeIdentifiers',
    '-framework','AVFoundation','-framework','AVKit',*[str(p) for p in swift],'-o',str(contents/'MacOS/AudioTranscribe')],check=True)
subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(app)],check=True)
subprocess.run(['/usr/bin/codesign','--verify','--strict',str(app)],check=True)
sources=sorted([*(root/'audio_transcribe').glob('*.py'),*(root/'native').glob('*.swift'),
                root/'scripts/build-app.py',root/'scripts/prepare-icon.swift',root/'scripts/make-icon.swift',
                root/'native/media-runtime.json'])
registration={'schema_version':1,'app_version':APP_VERSION,'app_build':APP_BUILD,
    'code_root':str(root),'python':str(root/'.venv/bin/python'),
    'bundle_icon_sha256':hashlib.sha256((contents/'Resources/AppIcon.icns').read_bytes()).hexdigest(),
    'shared_python_environment':str((root/'.venv').resolve()),
    'shared_media_vendor':str(vendor.resolve()),
    'source_hashes':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
    'retention':'Required deployed runtime. Do not classify this directory as disposable QA data.'}
(root/'runtime-manifest.json').write_text(json.dumps(registration,indent=2)+'\n')
if args.install:
    destination=args.install_dir.expanduser()/(args.name+'.app')
    if destination.exists(): raise SystemExit('Existing app preserved. Choose a new --name or install directory.')
    destination.parent.mkdir(parents=True,exist_ok=True)
    pending=destination.with_name('.'+args.name+'-'+uuid.uuid4().hex+'.pending.app')
    shutil.copytree(app,pending)
    # Darwin RENAME_EXCL prevents replacement in a concurrent install race.
    libc=ctypes.CDLL(None,use_errno=True)
    rename=libc.renamex_np
    rename.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint]
    if rename(os.fsencode(pending),os.fsencode(destination),0x4)!=0:
        raise OSError(ctypes.get_errno(),'Install was not published; staging copy preserved.',str(pending))
    subprocess.run(['/usr/bin/codesign','--verify','--strict',str(destination)],check=True)
    print(destination)
print(app)
