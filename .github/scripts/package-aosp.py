import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--template', required=True, type=Path)
    parser.add_argument('--build-tools', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()

    candidates = list((args.source / 'app/build/outputs/apk').rglob('output-metadata.json'))
    matches = []
    for path in candidates:
        metadata = json.loads(path.read_text())
        if metadata['variantName'].lower() == 'previewrelease':
            matches.append((path, metadata))
    if len(matches) != 1:
        raise ValueError(f'Expected one Preview Release output, found {len(matches)}')
    metadata_path, metadata = matches[0]
    if metadata['applicationId'] != 'com.android.packageinstaller':
        raise ValueError('APK metadata has the wrong application ID')
    if len(metadata['elements']) != 1:
        raise ValueError('Expected a universal APK')
    element = metadata['elements'][0]
    apk = metadata_path.parent / element['outputFile']
    badging = command(str(args.build_tools / 'aapt2'), 'dump', 'badging', str(apk))
    package_line = badging.splitlines()[0]
    if not package_line.startswith("package: name='com.android.packageinstaller' "):
        raise ValueError(f'APK manifest has the wrong package: {package_line}')
    signing = command(str(args.build_tools / 'apksigner'), 'verify', '--verbose', '--print-certs', str(apk))
    if 'Verified using v2 scheme (APK Signature Scheme v2): true' not in signing:
        raise ValueError('APK must have a valid v2 signature')

    version_name = element['versionName']
    version_code = int(element['versionCode'])
    commit = command('git', '-C', str(args.source), 'rev-parse', 'HEAD')
    suffix = re.sub(r'[^A-Za-z0-9._-]', '_', version_name)
    args.output.mkdir(parents=True, exist_ok=True)
    apk_output = args.output / f'InstallerX-Revived-AOSP-{suffix}.apk'
    module_output = args.output / f'InstallerX-Revived-AOSP-{suffix}-module.zip'
    shutil.copyfile(apk, apk_output)

    with tempfile.TemporaryDirectory(prefix='installerx-aosp-module-') as temporary:
        module = Path(temporary) / 'module'
        shutil.copytree(args.template, module)
        app_directory = module / 'system/priv-app/PackageInstaller'
        shutil.copyfile(apk, app_directory / 'PackageInstaller.apk')
        architectures = {
            'arm64-v8a': 'arm64', 'armeabi-v7a': 'arm', 'x86': 'x86', 'x86_64': 'x86_64',
        }
        with zipfile.ZipFile(apk) as archive:
            for entry in archive.namelist():
                parts = entry.split('/')
                if len(parts) == 3 and parts[0] == 'lib' and parts[2].endswith('.so'):
                    architecture = architectures[parts[1]]
                    target = app_directory / 'lib' / architecture / parts[2]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(entry))

        (module / 'module.prop').write_text(
            'id=com.android.packageinstaller\n'
            'name=InstallerX Revived AOSP (7ac9d42)\n'
            f'version={version_name}\n'
            f'versionCode={version_code}\n'
            'author=7ac9d42; upstream wxxsfxyzm and contributors\n'
            'description=Unofficial AOSP system installer build. Requires compatible systemless mounts, signature handling and ROM routing.\n',
            encoding='utf-8',
        )
        allowlist = module / 'system/etc/permissions/privapp-permissions-installerx.xml'
        permission_root = ET.parse(allowlist).getroot()
        permission_block = permission_root.find('privapp-permissions')
        if permission_block.attrib['package'] != metadata['applicationId']:
            raise ValueError('Privilege allowlist package does not match APK')
        with zipfile.ZipFile(module_output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(module.rglob('*')):
                if path.is_file():
                    relative = path.relative_to(module).as_posix()
                    info = zipfile.ZipInfo(relative)
                    info.create_system = 3
                    executable = relative.endswith('.sh') or relative.endswith('/update-binary')
                    info.external_attr = (0o100755 if executable else 0o100644) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, path.read_bytes())
        with zipfile.ZipFile(module_output) as archive:
            required = {
                'module.prop', 'customize.sh', 'action.sh',
                'META-INF/com/google/android/update-binary',
                'META-INF/com/google/android/updater-script',
                'system/priv-app/PackageInstaller/PackageInstaller.apk',
                'system/priv-app/PackageInstaller/.replace',
                'system/etc/permissions/privapp-permissions-installerx.xml',
            }
            if not required.issubset(archive.namelist()):
                raise ValueError('Module ZIP is missing required root entries')
            if archive.read('system/priv-app/PackageInstaller/PackageInstaller.apk') != apk.read_bytes():
                raise ValueError('Module APK differs from verified standalone APK')
            if archive.testzip() is not None:
                raise ValueError('Module ZIP failed its CRC check')

    info = {
        'upstream_repository': 'wxxsfxyzm/InstallerX-Revived',
        'upstream_commit': commit,
        'application_id': metadata['applicationId'],
        'variant': metadata['variantName'],
        'version_name': version_name,
        'version_code': version_code,
        'apk': apk_output.name,
        'module': module_output.name,
        'apk_sha256': digest(apk_output),
        'module_sha256': digest(module_output),
        'signing_verification': signing.splitlines(),
        'device_installation_tested': False,
    }
    (args.output / 'build-info.json').write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
    (args.output / 'SHA256SUMS').write_text(
        f'{digest(apk_output)}  {apk_output.name}\n{digest(module_output)}  {module_output.name}\n',
        encoding='utf-8',
    )
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
