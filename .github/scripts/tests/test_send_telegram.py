import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error


SCRIPT = Path(__file__).resolve().parents[1] / 'send-telegram.py'
SPEC = importlib.util.spec_from_file_location('send_telegram', SCRIPT)
sender = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sender)


class Response:
    def __init__(self, value, status=200):
        self.body = json.dumps(value).encode('utf-8')
        self.status = status

    def getcode(self):
        return self.status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def success(message_id):
    return Response({'ok': True, 'result': {'message_id': message_id}})


class TextTests(unittest.TestCase):
    def test_utf16_boundaries_do_not_split_emoji_or_lose_text(self):
        for text in ('a' * 4097, '😀' * 4097, 'a' * 4095 + '😀' + '尾巴', '😀' * 2048):
            with self.subTest(length=len(text)):
                chunks = sender.split_text(text)
                self.assertEqual(''.join(chunks), text)
                self.assertTrue(all(sender.utf16_length(part) <= 4096 for part in chunks))
                self.assertTrue(all(part for part in chunks))
        self.assertEqual(sender.utf16_length('a😀'), 3)

    def test_markdown_keeps_changelog_links_and_code(self):
        markdown = ('## Updates\n- **Fixed** `some_code` and *emoji* 😀\n'
                    '> See [Full Changelog](https://example.test/compare/a...b_(v2))\n'
                    '```text\nimportant detail\n```')
        text = sender.plain_text(markdown)
        self.assertIn('Updates\n• Fixed some_code and emoji 😀', text)
        self.assertIn('Full Changelog (https://example.test/compare/a...b_(v2))', text)
        self.assertIn('important detail', text)
        self.assertNotIn('```', text)
        self.assertEqual(sender.plain_text('```inline detail```'), 'inline detail')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.release_path = self.root / 'release.json'
        self.state_path = self.root / 'state.json'
        self.helper = self.root / 'parent-helper.py'
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.apk = b'test APK data'
        (self.artifacts / 'installer.apk').write_bytes(self.apk)
        (self.artifacts / 'module.zip').write_bytes(b'module must never be uploaded')
        self.release = {'source_sha': 'a' * 40, 'id': 123, 'tag_name': 'v1.2',
                        'html_url': 'https://example.test/releases/v1.2', 'body': 'body is not changelog',
                        'changelog': '## Changes\n- **Fix**\n[Full Changelog](https://example.test/compare/a...b)'}
        self.info = {'application_id': 'com.android.packageinstaller', 'upstream_commit': 'a' * 40,
                     'apk': 'installer.apk', 'apk_sha256': hashlib.sha256(self.apk).hexdigest(),
                     'version_name': '1.2', 'module': 'module.zip'}
        self.environ = {'TG_BOT_TOKEN': 'secret-bot-token', 'CHATID': 'secret-chat-id'}
        self.events = []
        self.checkpoints = []
        self.requests = []
        self.results = []
        self.sleeps = []
        self.reset_state()
        self.write_inputs()

    def reset_state(self):
        self.state = {'source_sha': self.release['source_sha'], 'release': copy.deepcopy(self.release),
                      'status': 'pending', 'delivery': {'next_index': 0, 'pending_index': None,
                                                       'message_ids': [], 'payload_sha256': None}}

    def write_inputs(self):
        self.release_path.write_text(json.dumps(self.release), encoding='utf-8')
        self.state_path.write_text(json.dumps(self.state), encoding='utf-8')
        (self.artifacts / 'build-info.json').write_text(json.dumps(self.info), encoding='utf-8')

    def checkpoint(self, state):
        self.state = copy.deepcopy(state)
        self.checkpoints.append(copy.deepcopy(state))
        self.events.append(('save', state['delivery']['pending_index'], state['delivery']['next_index']))
        self.state_path.write_text(json.dumps(state), encoding='utf-8')

    def urlopen(self, request, timeout):
        self.assertEqual(timeout, 60)
        self.assertEqual(self.events[-1][0], 'save')
        self.assertIsNotNone(self.state['delivery']['pending_index'])
        self.events.append(('api',))
        self.requests.append(request)
        result = self.results.pop(0) if self.results else success(100 + len(self.requests))
        if isinstance(result, Exception):
            raise result
        return result

    def run_delivery(self, **kwargs):
        options = {'urlopen': self.urlopen, 'checkpoint': self.checkpoint,
                   'sleep': self.sleeps.append, 'environ': self.environ}
        options.update(kwargs)
        sender.deliver(self.release_path, self.state_path, self.artifacts, self.helper, **options)

    def long_changelog(self):
        self.release['changelog'] = '## Changes\n' + '😀abc\n' * 2200 + '\n[Full Changelog](https://example.test/a...b)'
        self.reset_state()
        self.write_inputs()

    def test_short_caption_uploads_only_apk_then_completes(self):
        self.run_delivery()
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertTrue(request.full_url.endswith('/sendDocument'))
        self.assertTrue(request.get_header('Content-type').startswith('multipart/form-data; boundary='))
        self.assertIn(b'filename="installer.apk"', request.data)
        self.assertIn(self.apk, request.data)
        self.assertIn(b'InstallerX Revived AOSP 1.2', request.data)
        self.assertIn(b'Full Changelog (https://example.test/compare/a...b)', request.data)
        self.assertNotIn(b'module.zip', request.data)
        self.assertNotIn(b'module must never', request.data)
        self.assertNotIn(b'body is not changelog', request.data)
        self.assertNotIn(b'parse_mode', request.data)
        self.assertEqual(self.state['status'], 'complete')
        self.assertEqual(self.state['delivery']['message_ids'], [101])
        self.assertEqual(self.events, [('save', 0, 0), ('api',), ('save', None, 1), ('save', None, 1)])

    def test_long_changelog_sent_in_full_as_plain_messages(self):
        self.long_changelog()
        self.run_delivery()
        texts = []
        for request in self.requests[1:]:
            self.assertTrue(request.full_url.endswith('/sendMessage'))
            value = json.loads(request.data)
            self.assertNotIn('parse_mode', value)
            self.assertLessEqual(sender.utf16_length(value['text']), 4096)
            texts.append(value['text'])
        self.assertEqual(''.join(texts), sender.plain_text(self.release['changelog']))
        self.assertNotIn('😀'.encode(), self.requests[0].data)
        self.assertEqual(self.state['delivery']['next_index'], len(self.requests))

    def test_caption_1024_utf16_boundary(self):
        prefix = f'InstallerX Revived AOSP 1.2\n提交：{self.release["source_sha"][:7]}\n{self.release["html_url"]}\n\n'
        remaining = 1024 - sender.utf16_length(prefix)
        self.release['changelog'] = '😀' * (remaining // 2) + 'a' * (remaining % 2)
        self.reset_state()
        self.write_inputs()
        payload = sender.prepare_payload(self.release, self.artifacts)
        self.assertEqual(sender.utf16_length(payload[3]), 1024)
        self.assertEqual(payload[4], [])
        self.release['changelog'] += '😀'
        payload = sender.prepare_payload(self.release, self.artifacts)
        self.assertEqual(payload[4], [self.release['changelog']])
        self.assertLessEqual(sender.utf16_length(payload[3]), 1024)

    def test_rejects_invalid_apk_provenance_hash_and_names(self):
        cases = [('application_id', 'other.package'), ('upstream_commit', 'b' * 40),
                 ('apk_sha256', '0' * 64), ('apk', '../installer.apk'), ('apk', '..\\installer.apk'),
                 ('apk', 'C:installer.apk'), ('apk', '/installer.apk'), ('apk', 'module.zip'),
                 ('apk', 'bad\r\nname.apk'), ('apk', 'missing.apk')]
        original = copy.deepcopy(self.info)
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.info = dict(original, **{field: value})
                self.write_inputs()
                with self.assertRaises(sender.DeliveryError):
                    self.run_delivery()
                self.assertEqual(self.requests, [])
                self.assertEqual(self.checkpoints, [])

    def test_apk_size_limit(self):
        with mock.patch.object(sender, 'MAX_APK_BYTES', len(self.apk) - 1):
            with self.assertRaises(sender.DeliveryError):
                self.run_delivery()
        self.assertFalse(self.requests)

    def test_module_not_required(self):
        (self.artifacts / 'module.zip').unlink()
        self.info['module'] = '../irrelevant.zip'
        self.write_inputs()
        self.run_delivery()
        self.assertEqual(self.state['status'], 'complete')

    def test_api_rejection_clears_pending_and_does_not_advance(self):
        for response in (Response({'ok': False, 'description': 'secret-bot-token secret-chat-id'}),
                         urllib.error.HTTPError('https://token.invalid', 400, 'private details', {},
                                                io.BytesIO(b'invalid'))):
            with self.subTest(response=type(response).__name__):
                self.results = [response]
                self.reset_state()
                self.write_inputs()
                with self.assertRaises(sender.APIRejected):
                    self.run_delivery()
                self.assertIsNone(self.state['delivery']['pending_index'])
                self.assertEqual(self.state['delivery']['next_index'], 0)
                self.assertEqual(self.state['delivery']['message_ids'], [])

    def test_429_retries_after_saved_clear_checkpoint(self):
        rejected = {'ok': False, 'error_code': 429, 'parameters': {'retry_after': 2}}
        self.results = [urllib.error.HTTPError('https://private.invalid', 429, 'private details', {},
                                               io.BytesIO(json.dumps(rejected).encode())), success(77)]
        self.run_delivery()
        self.assertEqual(self.sleeps, [2])
        self.assertEqual(self.state['delivery']['message_ids'], [77])
        self.assertEqual(self.events[:5], [('save', 0, 0), ('api',), ('save', None, 0),
                                          ('save', 0, 0), ('api',)])

    def test_429_has_finite_retries_and_delay(self):
        self.results = [Response({'ok': False, 'error_code': 429, 'parameters': {'retry_after': 0}})] * 10
        with self.assertRaises(sender.APIRejected):
            self.run_delivery()
        self.assertEqual(len(self.requests), sender.MAX_RETRIES + 1)
        self.assertEqual(len(self.sleeps), sender.MAX_RETRIES)
        self.assertIsNone(self.state['delivery']['pending_index'])
        self.reset_state()
        self.write_inputs()
        self.results = [Response({'ok': False, 'error_code': 429, 'parameters': {'retry_after': 100000}})]
        count = len(self.requests)
        with self.assertRaises(sender.APIRejected):
            self.run_delivery()
        self.assertEqual(len(self.requests), count + 1)

    def test_ambiguous_network_and_response_preserve_pending(self):
        for response in (TimeoutError('secret-bot-token secret-chat-id'),
                         ConnectionResetError('private connection'),
                         urllib.error.URLError('private URL'), Response({'ok': True, 'result': {}}),
                         Response({'unexpected': 'response'}), Response({'error': 'server'}, status=502)):
            with self.subTest(response=type(response).__name__):
                self.reset_state()
                self.write_inputs()
                self.results = [response]
                count = len(self.requests)
                with self.assertRaises(sender.DeliveryError):
                    self.run_delivery()
                self.assertEqual(self.state['delivery']['pending_index'], 0)
                self.assertEqual(len(self.requests), count + 1)
                with self.assertRaisesRegex(sender.DeliveryError, 'pending_index'):
                    self.run_delivery()
                self.assertEqual(len(self.requests), count + 1)

    def test_resume_skips_confirmed_apk_after_explicit_failure(self):
        self.long_changelog()
        self.results = [success(77), Response({'ok': False, 'error_code': 400})]
        with self.assertRaises(sender.APIRejected):
            self.run_delivery()
        self.assertEqual(self.state['delivery']['next_index'], 1)
        self.assertEqual(self.state['delivery']['message_ids'], [77])
        self.assertIsNone(self.state['delivery']['pending_index'])
        self.requests.clear()
        self.run_delivery()
        self.assertTrue(all(request.full_url.endswith('/sendMessage') for request in self.requests))
        self.assertEqual(self.state['status'], 'complete')
        self.assertEqual(self.state['delivery']['message_ids'][0], 77)

    def test_payload_change_with_progress_stops(self):
        self.long_changelog()
        self.results = [success(77), Response({'ok': False})]
        with self.assertRaises(sender.APIRejected):
            self.run_delivery()
        count = len(self.requests)
        for change in ('apk', 'destination', 'text'):
            with self.subTest(change=change):
                info = copy.deepcopy(self.info)
                release = copy.deepcopy(self.release)
                environment = dict(self.environ)
                if change == 'apk':
                    data = b'different valid APK'
                    (self.artifacts / 'installer.apk').write_bytes(data)
                    self.info['apk_sha256'] = hashlib.sha256(data).hexdigest()
                elif change == 'destination':
                    self.environ['CHATID'] = 'another-chat'
                else:
                    self.release['changelog'] += '\nchanged'
                    self.state['release'] = copy.deepcopy(self.release)
                self.write_inputs()
                with self.assertRaisesRegex(sender.DeliveryError, '指纹'):
                    self.run_delivery()
                self.assertEqual(len(self.requests), count)
                self.info, self.release, self.environ = info, release, environment
                self.state['release'] = copy.deepcopy(release)
                (self.artifacts / 'installer.apk').write_bytes(self.apk)

    def test_no_progress_allows_fingerprint_change_and_complete_is_noop(self):
        self.state['delivery']['payload_sha256'] = '0' * 64
        self.write_inputs()
        self.run_delivery()
        fingerprint = self.state['delivery']['payload_sha256']
        self.assertNotEqual(fingerprint, '0' * 64)
        count = len(self.checkpoints)
        self.run_delivery()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.checkpoints), count)
        self.assertEqual(self.state['delivery']['payload_sha256'], fingerprint)

    def test_checkpoint_failure_prevents_api_call(self):
        def fail(state):
            raise sender.DeliveryError('checkpoint failed')
        with self.assertRaises(sender.DeliveryError):
            self.run_delivery(checkpoint=fail)
        self.assertFalse(self.requests)

    def test_failure_after_api_success_never_retries_request(self):
        def fail_after_success(state):
            if state['delivery']['next_index']:
                raise sender.DeliveryError('checkpoint failed')
            self.checkpoint(state)
        with self.assertRaises(sender.DeliveryError):
            self.run_delivery(checkpoint=fail_after_success)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.state['delivery']['pending_index'], 0)
        with self.assertRaisesRegex(sender.DeliveryError, 'pending_index'):
            self.run_delivery()
        self.assertEqual(len(self.requests), 1)

    def test_default_checkpoint_command_and_saved_file(self):
        checkpoint = sender.make_checkpoint(self.release_path, self.state_path, self.helper)
        with mock.patch.object(sender.subprocess, 'run') as run:
            checkpoint(self.state)
        self.assertEqual(json.loads(self.state_path.read_text(encoding='utf-8')), self.state)
        self.assertEqual(run.call_args.args[0], [sys.executable, '-I', str(self.helper), 'save-state',
                                               '--release', str(self.release_path), '--state', str(self.state_path)])
        self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_default_checkpoint_failure_is_sanitized_and_prevents_api(self):
        with mock.patch.object(sender.subprocess, 'run', side_effect=RuntimeError('secret-bot-token secret-chat-id')):
            with self.assertRaises(sender.DeliveryError) as caught:
                self.run_delivery(checkpoint=sender.make_checkpoint(self.release_path, self.state_path, self.helper))
        self.assertNotIn('secret', str(caught.exception))
        self.assertFalse(self.requests)
        self.assertEqual(json.loads(self.state_path.read_text(encoding='utf-8'))['delivery']['pending_index'], 0)

    def test_cli_never_outputs_secrets_or_raw_http_errors(self):
        argv = ['--release', str(self.release_path), '--state', str(self.state_path),
                '--artifacts', str(self.artifacts), '--state-helper', str(self.helper)]
        for error in (TimeoutError('https://api.telegram.org/botsecret-bot-token/sendDocument secret-chat-id'),
                      sender.APIRejected(), RuntimeError('secret-bot-token secret-chat-id')):
            output = io.StringIO()
            with mock.patch.object(sender, 'deliver', side_effect=error), contextlib.redirect_stderr(output), contextlib.redirect_stdout(output):
                self.assertEqual(sender.main(argv), 1)
            for secret in ('secret-bot-token', 'secret-chat-id', 'api.telegram.org', 'https://'):
                self.assertNotIn(secret, output.getvalue())

    def test_malformed_release_and_inconsistent_state_fail_before_send(self):
        for mutation in ('short_sha', 'missing_changelog', 'snapshot', 'ids', 'complete', 'index', 'missing_pending'):
            with self.subTest(mutation=mutation):
                release = copy.deepcopy(self.release)
                self.reset_state()
                if mutation == 'short_sha':
                    self.release['source_sha'] = 'abcdef0'
                elif mutation == 'missing_changelog':
                    del self.release['changelog']
                elif mutation == 'snapshot':
                    self.state['release']['body'] = 'different'
                elif mutation == 'ids':
                    self.state['delivery']['message_ids'] = [1]
                elif mutation == 'complete':
                    self.state['status'] = 'complete'
                elif mutation == 'missing_pending':
                    del self.state['delivery']['pending_index']
                else:
                    self.state['delivery']['next_index'] = True
                self.write_inputs()
                with self.assertRaises(sender.DeliveryError):
                    self.run_delivery()
                self.assertFalse(self.requests)
                self.release = release

    def test_missing_environment_fails_before_send(self):
        with self.assertRaises(sender.DeliveryError):
            self.run_delivery(environ={})
        self.assertFalse(self.requests)


if __name__ == '__main__':
    unittest.main()
