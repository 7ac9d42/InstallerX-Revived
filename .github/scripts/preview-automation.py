import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


UPSTREAM = 'wxxsfxyzm/InstallerX-Revived'
STATE_BRANCH = 'automation-state'
SHA = re.compile(r'[0-9a-f]{40}')


def github_api(method, endpoint, data=None, missing_ok=False):
    command = ['gh', 'api', '--method', method, endpoint]
    if data is not None:
        command.extend(['--input', '-'])
    result = subprocess.run(
        command, input=json.dumps(data) if data is not None else None,
        text=True, capture_output=True, timeout=60,
    )
    if result.returncode:
        match = re.search(r'\(HTTP (\d{3})\)', result.stderr)
        status = match.group(1) if match else 'unknown'
        if missing_ok and status == '404':
            return None
        raise RuntimeError(f'GitHub API {method} failed (HTTP {status})')
    return json.loads(result.stdout) if result.stdout.strip() else None


def require_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('Expected a full commit SHA')
    return value


def changelog_from_body(body):
    marker = re.search(
        r'^\s*(?:#{1,6}\s*)?(?:\*\*)?'
        r'(?:更新日志\s*[（(]\s*Changelog\s*[）)]|Changelog|更新日志)'
        r'\s*[:：]?\s*(?:\*\*)?\s*$',
        body, re.IGNORECASE | re.MULTILINE,
    )
    if marker is None:
        raise ValueError('Upstream prerelease has no recognized changelog section')
    section = body[marker.end():].strip()
    section = re.split(r'^#{1,6}\s+', section, maxsplit=1, flags=re.MULTILINE)[0].strip()
    if not re.search(r'^\s*[-*]\s+\S', section, re.MULTILINE):
        raise ValueError('Upstream prerelease changelog has no entries')
    return '更新日志（Changelog）：\n' + section


def resolve_release(releases, api=github_api):
    previews = [release for release in releases if release.get('prerelease') and not release.get('draft')]
    if not previews:
        return None
    release = max(previews, key=lambda item: (item.get('published_at') or '', item['id']))
    tag = release['tag_name']
    reference = api('GET', f'repos/{UPSTREAM}/git/ref/tags/{quote(tag, safe="")}')
    target = reference['object']
    for _ in range(10):
        if target['type'] == 'commit':
            source_sha = require_sha(target['sha'])
            break
        if target['type'] != 'tag':
            raise ValueError('Prerelease tag does not reference a commit')
        target = api('GET', f'repos/{UPSTREAM}/git/tags/{require_sha(target["sha"])}')['object']
    else:
        raise ValueError('Prerelease tag nesting limit exceeded')
    body = release.get('body') or ''
    return {
        'id': release['id'], 'tag_name': tag, 'html_url': release['html_url'],
        'body': body, 'source_sha': source_sha, 'changelog': changelog_from_body(body),
    }


def read_state(repository, source_sha, api=github_api):
    source_sha = require_sha(source_sha)
    response = api(
        'GET', f'repos/{repository}/contents/deliveries/{source_sha}.json?ref={STATE_BRANCH}',
        missing_ok=True,
    )
    if response is None:
        return None, None
    return json.loads(base64.b64decode(response['content'])), response['sha']


def ensure_state_branch(repository, api=github_api):
    reference = api('GET', f'repos/{repository}/git/ref/heads/{STATE_BRANCH}', missing_ok=True)
    if reference is not None:
        return
    tree = api('POST', f'repos/{repository}/git/trees', {'tree': [{
        'path': 'README.md', 'mode': '100644', 'type': 'blob',
        'content': 'Delivery checkpoints for the AOSP preview automation.\n',
    }]})
    commit = api('POST', f'repos/{repository}/git/commits', {
        'message': 'fix(state): 初始化预发布交付状态', 'tree': tree['sha'], 'parents': [],
    })
    api('POST', f'repos/{repository}/git/refs', {
        'ref': f'refs/heads/{STATE_BRANCH}', 'sha': commit['sha'],
    })


def detect(repository, output, output_file=None, api=github_api):
    releases = api('GET', f'repos/{UPSTREAM}/releases?per_page=100')
    release = resolve_release(releases, api)
    outputs = {'should_build': 'false', 'source_sha': '', 'reason': 'no-prerelease'}
    if release is not None:
        source_sha = release['source_sha']
        state, _ = read_state(repository, source_sha, api)
        outputs.update(source_sha=source_sha, reason='already-delivered')
        if state is None or state['status'] != 'complete':
            if state is None:
                state = {
                    'source_sha': source_sha, 'release': release, 'status': 'pending',
                    'delivery': {
                        'next_index': 0, 'pending_index': None,
                        'message_ids': [], 'payload_sha256': None,
                    },
                }
            else:
                if state['source_sha'] != source_sha:
                    raise ValueError('Stored delivery state has a different source commit')
                if state['delivery']['pending_index'] is not None:
                    raise ValueError('Previous Telegram delivery outcome is unknown; inspect before retrying')
                release = state['release']
            output.mkdir(parents=True, exist_ok=True)
            (output / 'release.json').write_text(json.dumps(release, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            (output / 'delivery-state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            outputs.update(should_build='true', reason='new-or-incomplete-prerelease')
    if output_file:
        with output_file.open('a', encoding='utf-8') as stream:
            for key, value in outputs.items():
                stream.write(f'{key}={value}\n')
    print(json.dumps(outputs))
    return outputs


def save_state(repository, release_path, state_path, api=github_api):
    release = json.loads(release_path.read_text(encoding='utf-8'))
    state = json.loads(state_path.read_text(encoding='utf-8'))
    source_sha = require_sha(release['source_sha'])
    if state['source_sha'] != source_sha or state['release'] != release:
        raise ValueError('Delivery checkpoint does not match the release snapshot')
    if state['status'] not in ('pending', 'complete'):
        raise ValueError('Invalid delivery status')
    delivery = state['delivery']
    if delivery['next_index'] != len(delivery['message_ids']):
        raise ValueError('Invalid Telegram delivery checkpoint')
    if state['status'] == 'complete' and (delivery['pending_index'] is not None or not delivery['message_ids']):
        raise ValueError('Cannot complete an unconfirmed Telegram delivery')
    existing, blob_sha = read_state(repository, source_sha, api)
    if existing == state:
        return
    if existing is not None:
        if existing['status'] == 'complete':
            raise ValueError('Cannot overwrite a completed delivery')
        if existing['delivery']['next_index'] > delivery['next_index']:
            raise ValueError('Cannot move a delivery checkpoint backwards')
    ensure_state_branch(repository, api)
    content = json.dumps(state, ensure_ascii=False, indent=2) + '\n'
    data = {
        'branch': STATE_BRANCH,
        'message': f'fix(state): 记录 {source_sha[:7]} 预发布交付状态',
        'content': base64.b64encode(content.encode('utf-8')).decode('ascii'),
    }
    if blob_sha:
        data['sha'] = blob_sha
    api('PUT', f'repos/{repository}/contents/deliveries/{source_sha}.json', data)
    print(f'Saved delivery checkpoint for {source_sha[:7]}: {state["status"]}')


def add_metadata(artifacts, release_path, config_sha):
    info_path = artifacts / 'build-info.json'
    info = json.loads(info_path.read_text(encoding='utf-8'))
    info['ci_configuration_commit'] = require_sha(config_sha)
    if release_path:
        release = json.loads(release_path.read_text(encoding='utf-8'))
        if info['upstream_commit'] != require_sha(release['source_sha']):
            raise ValueError('Built APK source differs from prerelease source')
        info['upstream_release'] = {
            'id': release['id'], 'tag': release['tag_name'], 'url': release['html_url'],
            'changelog_sha256': hashlib.sha256(release['changelog'].encode('utf-8')).hexdigest(),
        }
    info_path.write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    detection = commands.add_parser('detect')
    detection.add_argument('--output', type=Path, required=True)
    detection.add_argument('--github-output', type=Path)
    checkpoint = commands.add_parser('save-state')
    checkpoint.add_argument('--release', type=Path, required=True)
    checkpoint.add_argument('--state', type=Path, required=True)
    metadata = commands.add_parser('add-metadata')
    metadata.add_argument('--artifacts', type=Path, required=True)
    metadata.add_argument('--release', type=Path)
    metadata.add_argument('--configuration-sha', required=True)
    args = parser.parse_args()
    if args.command == 'add-metadata':
        add_metadata(args.artifacts, args.release, args.configuration_sha)
        return
    repository = os.environ.get('GITHUB_REPOSITORY', '7ac9d42/InstallerX-Revived')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GitHub repository')
    if args.command == 'detect':
        detect(repository, args.output, args.github_output)
    else:
        save_state(repository, args.release, args.state)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f'Preview automation failed: {error}') from None
