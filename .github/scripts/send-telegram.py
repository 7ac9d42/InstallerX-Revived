import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


MAX_APK_BYTES = 50 * 1024 * 1024
MAX_RETRIES = 3
MAX_RETRY_AFTER = 60


class DeliveryError(Exception):
    pass


class APIRejected(DeliveryError):
    def __init__(self, retry_after=None):
        super().__init__('Telegram 明确拒绝请求。')
        self.retry_after = retry_after


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def split_text(text, limit=4096):
    chunks = []
    start = 0
    length = 0
    for index, character in enumerate(text):
        units = 2 if ord(character) > 0xffff else 1
        if length + units > limit:
            if index == start:
                raise DeliveryError('消息长度限制无效。')
            chunks.append(text[start:index])
            start = index
            length = 0
        length += units
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def plain_text(markdown):
    # 保留链接目标和代码内容，只移除常见 GitHub Markdown 排版。
    text = re.sub(r'^ {0,3}(`{3,}|~{3,})[^\n`~]*$', '', markdown, flags=re.MULTILINE)
    text = re.sub(r'^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?$', r'\1', text, flags=re.MULTILINE)
    text = re.sub(r'^ {0,3}>[ \t]?', '', text, flags=re.MULTILINE)
    text = re.sub(r'^([ \t]*)[-*+][ \t]+', r'\1• ', text, flags=re.MULTILINE)
    # 链接地址允许括号；简单的 [label](url) 正则会丢失这类链接的一部分。
    result = []
    position = 0
    pattern = re.compile(r'!?\[([^\]\n]*)\]\(')
    while match := pattern.search(text, position):
        end = match.end()
        depth = 1
        while end < len(text) and depth:
            if text[end] == '\\':
                end += 2
                continue
            if text[end] == '(':
                depth += 1
            elif text[end] == ')':
                depth -= 1
            end += 1
        if depth:
            break
        label = match.group(1)
        target = text[match.end():end - 1]
        result.append(text[position:match.start()])
        result.append(target if label == target else f'{label} ({target})')
        position = end
    result.append(text[position:])
    text = ''.join(result)
    text = re.sub(r'<(https?://[^>]+)>', r'\1', text)
    text = re.sub(r'(`+)(.+?)\1', r'\2', text)
    text = re.sub(r'(\*\*|__|~~)(.+?)\1', r'\2', text)
    text = re.sub(r'(?<!\w)(\*|_)([^\n]+?)\1(?!\w)', r'\2', text)
    text = re.sub(r'\\([\\`*_{}\[\]()#+.!>~-])', r'\1', text)
    return html.unescape(text).strip()


def read_json(path):
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise DeliveryError('JSON 必须是对象。')
    return value


def validate_release(release):
    if not isinstance(release.get('source_sha'), str) or not re.fullmatch(r'[0-9a-fA-F]{40}', release['source_sha']):
        raise DeliveryError('发布 source_sha 必须是完整提交哈希。')
    if type(release.get('id')) is not int:
        raise DeliveryError('发布 id 必须是整数。')
    for key in ('tag_name', 'html_url', 'body', 'changelog'):
        if not isinstance(release.get(key), str):
            raise DeliveryError('发布快照缺少文本字段。')


def prepare_payload(release, artifacts):
    info = read_json(artifacts / 'build-info.json')
    if info.get('application_id') != 'com.android.packageinstaller':
        raise DeliveryError('APK application_id 不匹配。')
    if info.get('upstream_commit') != release['source_sha']:
        raise DeliveryError('APK upstream_commit 不匹配。')
    name = info.get('apk')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*\.apk', name):
        raise DeliveryError('APK 文件名不安全。')
    root = artifacts.resolve()
    apk = artifacts / name
    if apk.is_symlink() or apk.resolve().parent != root or not apk.is_file():
        raise DeliveryError('APK 必须是产物目录内的普通文件。')
    if apk.stat().st_size > MAX_APK_BYTES:
        raise DeliveryError('APK 超过 Telegram 50 MB 限制。')
    data = apk.read_bytes()
    if len(data) > MAX_APK_BYTES:
        raise DeliveryError('APK 超过 Telegram 50 MB 限制。')
    digest = hashlib.sha256(data).hexdigest()
    if info.get('apk_sha256') != digest:
        raise DeliveryError('APK SHA256 不匹配。')
    version = info.get('version_name', release['tag_name'])
    if not isinstance(version, str):
        raise DeliveryError('APK 版本字段无效。')
    short_caption = f'InstallerX Revived AOSP {version}\n提交：{release["source_sha"][:7]}\n{release["html_url"]}'
    if utf16_length(short_caption) > 1024:
        raise DeliveryError('版本和来源链接超过 caption 长度限制。')
    changelog = plain_text(release['changelog'])
    full_caption = short_caption + ('\n\n' + changelog if changelog else '')
    if utf16_length(full_caption) <= 1024:
        caption, messages = full_caption, []
    else:
        caption, messages = short_caption, split_text(changelog)
    return name, data, digest, caption, messages


def validate_state(state, release, count):
    if state.get('source_sha') != release['source_sha'] or state.get('release') != release:
        raise DeliveryError('状态与发布快照不匹配。')
    if state.get('status') not in ('pending', 'complete'):
        raise DeliveryError('状态 status 无效。')
    delivery = state.get('delivery')
    if not isinstance(delivery, dict) or not {'next_index', 'pending_index', 'message_ids', 'payload_sha256'} <= delivery.keys():
        raise DeliveryError('状态缺少 delivery 字段。')
    if delivery.get('pending_index') is not None:
        raise DeliveryError('上次发送结果不明，pending_index 尚未解除；请人工核实。')
    index = delivery.get('next_index')
    ids = delivery.get('message_ids')
    if type(index) is not int or not 0 <= index <= count:
        raise DeliveryError('状态 next_index 无效。')
    if not isinstance(ids, list) or len(ids) != index or any(type(item) is not int for item in ids):
        raise DeliveryError('状态 message_ids 无效。')
    if state['status'] == 'complete' and index != count:
        raise DeliveryError('完成状态与发送进度不匹配。')
    fingerprint = delivery.get('payload_sha256')
    if fingerprint is not None and (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
        raise DeliveryError('状态 payload_sha256 无效。')
    return delivery


def make_checkpoint(release_path, state_path, state_helper):
    def checkpoint(state):
        temporary = None
        try:
            # 父 helper 从 --state 读取已更新的快照，再持久化其检查点。
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=state_path.parent,
                                             prefix=state_path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, state_path)
            subprocess.run(
                [sys.executable, '-I', str(state_helper), 'save-state', '--release', str(release_path),
                 '--state', str(state_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
        except Exception:
            raise DeliveryError('状态检查点保存失败，发送已停止。') from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return checkpoint


def multipart(chat_id, name, data, caption):
    boundary = 'installerx-' + uuid.uuid4().hex
    pieces = []
    for field, value in (('chat_id', chat_id), ('caption', caption)):
        pieces.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n'.encode())
        pieces.append(value.encode('utf-8') + b'\r\n')
    pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\n'
                   'Content-Type: application/vnd.android.package-archive\r\n\r\n').encode())
    pieces.extend((data, f'\r\n--{boundary}--\r\n'.encode()))
    return b''.join(pieces), f'multipart/form-data; boundary={boundary}'


def api_request(token, method, body, content_type, urlopen):
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=body,
                                     headers={'Content-Type': content_type}, method='POST')
    http_status = None
    try:
        with urlopen(request, timeout=60) as response:
            http_status = response.getcode()
            response_body = response.read()
    except urllib.error.HTTPError as error:
        http_status = error.code
        try:
            response_body = error.read()
        except Exception:
            response_body = b''
    except Exception:
        raise DeliveryError('Telegram 网络请求结果不明，已保留 pending_index。') from None
    try:
        result = json.loads(response_body)
    except (ValueError, UnicodeError):
        result = None
    # HTTP 明确拒绝，或 Bot API 显式返回 ok:false，才允许解除不明状态。
    rejected = http_status is not None and 400 <= http_status < 500
    rejected = rejected or (isinstance(result, dict) and result.get('ok') is False)
    if rejected:
        retry_after = None
        if http_status == 429 or (isinstance(result, dict) and result.get('error_code') == 429):
            parameters = result.get('parameters', {}) if isinstance(result, dict) else {}
            delay = parameters.get('retry_after') if isinstance(parameters, dict) else None
            if type(delay) is int and 0 <= delay <= MAX_RETRY_AFTER:
                retry_after = delay
        raise APIRejected(retry_after)
    if (http_status is not None and not 200 <= http_status < 300) or not isinstance(result, dict) or result.get('ok') is not True:
        raise DeliveryError('Telegram 响应结果不明，已保留 pending_index。')
    message = result.get('result')
    if not isinstance(message, dict) or type(message.get('message_id')) is not int:
        raise DeliveryError('Telegram 响应缺少 message_id，已保留 pending_index。')
    return message['message_id']


def deliver(release_path, state_path, artifacts, state_helper, *, urlopen=None, checkpoint=None,
            sleep=time.sleep, environ=None):
    environ = os.environ if environ is None else environ
    token, chat_id = environ.get('TG_BOT_TOKEN'), environ.get('CHATID')
    if not token or not chat_id:
        raise DeliveryError('缺少 TG_BOT_TOKEN 或 CHATID。')
    release = read_json(release_path)
    validate_release(release)
    state = read_json(state_path)
    # 不论新产物是否有效，都优先拦截结果不明的旧请求。
    if isinstance(state.get('delivery'), dict) and state['delivery'].get('pending_index') is not None:
        raise DeliveryError('上次发送结果不明，pending_index 尚未解除；请人工核实。')
    name, data, digest, caption, messages = prepare_payload(release, artifacts)
    delivery = validate_state(state, release, 1 + len(messages))
    payload = {'release': release, 'apk': name, 'apk_sha256': digest, 'caption': caption,
               'messages': messages, 'chat_id': chat_id}
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode('utf-8')).hexdigest()
    if delivery['next_index'] and delivery.get('payload_sha256') != fingerprint:
        raise DeliveryError('发送载荷指纹已变化，不能继续已有发送进度。')
    delivery['payload_sha256'] = fingerprint
    if state['status'] == 'complete':
        return
    checkpoint = checkpoint or make_checkpoint(release_path, state_path, state_helper)
    urlopen = urlopen or urllib.request.urlopen
    for index in range(delivery['next_index'], 1 + len(messages)):
        if index == 0:
            body, content_type = multipart(chat_id, name, data, caption)
            method = 'sendDocument'
        else:
            body = json.dumps({'chat_id': chat_id, 'text': messages[index - 1]}, ensure_ascii=False).encode('utf-8')
            content_type, method = 'application/json', 'sendMessage'
        retries = 0
        while True:
            delivery['pending_index'] = index
            checkpoint(state)
            try:
                message_id = api_request(token, method, body, content_type, urlopen)
            except APIRejected as error:
                delivery['pending_index'] = None
                checkpoint(state)
                if error.retry_after is None or retries >= MAX_RETRIES:
                    raise
                retries += 1
                sleep(error.retry_after)
                continue
            delivery['message_ids'].append(message_id)
            delivery['next_index'] = index + 1
            delivery['pending_index'] = None
            checkpoint(state)
            break
    state['status'] = 'complete'
    checkpoint(state)


def main(argv=None):
    parser = argparse.ArgumentParser()
    for argument in ('release', 'state', 'artifacts', 'state-helper'):
        parser.add_argument('--' + argument, required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        deliver(args.release, args.state, args.artifacts, args.state_helper)
    except Exception:
        # 不输出 API 描述、异常、路径或 URL，避免 token、chat id 和服务端回显泄露。
        print('Telegram 投递失败；请检查本地输入、检查点及 pending_index 后重试。', file=sys.stderr)
        return 1
    print('Telegram 投递完成。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
