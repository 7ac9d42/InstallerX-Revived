import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'preview-automation.py'
spec = importlib.util.spec_from_file_location('preview_automation', SCRIPT)
automation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(automation)
SOURCE = 'a' * 40
BODY = '''Preview announcement\n\n**更新日志（Changelog）:**\n- fix: keep the release source (author)\n- feat: new installer behavior (#123)\n\n**Full Changelog:** [compare](https://github.com/example/repo/compare/a...b)\n'''


def release_data(**overrides):
    release = {
        'id': 42, 'tag_name': '26.10.aaaaaaa', 'prerelease': True, 'draft': False,
        'published_at': '2026-11-03T00:00:00Z', 'target_commitish': 'main',
        'html_url': 'https://github.com/example/repo/releases/tag/26.10.aaaaaaa',
        'body': BODY,
    }
    release.update(overrides)
    return release


class PreviewTests(unittest.TestCase):
    def test_log_comes_from_published_body(self):
        changelog = automation.changelog_from_body(BODY)
        self.assertIn('fix: keep the release source', changelog)
        self.assertIn('Full Changelog', changelog)
        self.assertNotIn('Preview announcement', changelog)
        with self.assertRaises(ValueError):
            automation.changelog_from_body('No changelog was published')

    def test_annotated_tag_is_resolved_instead_of_main(self):
        calls = []
        def api(method, path, **kwargs):
            calls.append(path)
            if '/git/ref/tags/' in path:
                return {'object': {'type': 'tag', 'sha': 'b' * 40}}
            return {'object': {'type': 'commit', 'sha': SOURCE}}
        result = automation.resolve_release([
            release_data(), release_data(id=99, prerelease=False, published_at='2026-12-01T00:00:00Z'),
            release_data(id=100, draft=True, published_at='2026-12-02T00:00:00Z'),
        ], api)
        self.assertEqual(SOURCE, result['source_sha'])
        self.assertEqual(42, result['id'])
        self.assertEqual(2, len(calls))

    def test_no_preview_performs_no_tag_lookup(self):
        def unexpected(*args, **kwargs):
            self.fail('No API lookup should be needed')
        self.assertIsNone(automation.resolve_release([release_data(prerelease=False)], unexpected))

    def detect_api(self, state=None):
        def api(method, path, **kwargs):
            if '/releases?' in path:
                return [release_data()]
            if '/git/ref/tags/' in path:
                return {'object': {'type': 'commit', 'sha': SOURCE}}
            if '/contents/deliveries/' in path:
                if state is None:
                    return None
                return {'sha': 'blob', 'content': base64.b64encode(json.dumps(state).encode()).decode()}
            self.fail(f'Unexpected API call: {method} {path}')
        return api

    def test_detection_creates_snapshot_and_then_skips_completed_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = automation.detect('example/fork', output, api=self.detect_api())
            self.assertEqual('true', result['should_build'])
            saved = json.loads((output / 'release.json').read_text(encoding='utf-8'))
            self.assertEqual(SOURCE, saved['source_sha'])
            state = json.loads((output / 'delivery-state.json').read_text(encoding='utf-8'))
            state['status'] = 'complete'
            result = automation.detect('example/fork', output, api=self.detect_api(state))
            self.assertEqual('false', result['should_build'])

    def test_unknown_delivery_is_not_automatically_repeated(self):
        state = {
            'source_sha': SOURCE, 'status': 'pending',
            'delivery': {'pending_index': 0},
        }
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'unknown'):
            automation.detect('example/fork', Path(directory), api=self.detect_api(state))

    def test_resume_uses_original_release_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            automation.detect('example/fork', output, api=self.detect_api())
            state = json.loads((output / 'delivery-state.json').read_text(encoding='utf-8'))
            state['release']['changelog'] = 'Original published changelog'
            automation.detect('example/fork', output, api=self.detect_api(state))
            saved = json.loads((output / 'release.json').read_text(encoding='utf-8'))
            self.assertEqual('Original published changelog', saved['changelog'])

    def test_checkpoint_creates_only_state_branch_and_file(self):
        calls = []
        def api(method, path, data=None, **kwargs):
            calls.append((method, path, data))
            if method == 'GET':
                return None
            return {'sha': 'f' * 40}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            automation.detect('example/fork', output, api=self.detect_api())
            automation.save_state('example/fork', output / 'release.json', output / 'delivery-state.json', api)
        reference = [data for method, path, data in calls if path.endswith('/git/refs')][0]
        self.assertEqual('refs/heads/automation-state', reference['ref'])
        saved = [data for method, path, data in calls if method == 'PUT'][0]
        self.assertEqual('automation-state', saved['branch'])
        decoded = json.loads(base64.b64decode(saved['content']))
        self.assertEqual('pending', decoded['status'])

    def test_build_metadata_rejects_other_source(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'build-info.json').write_text(json.dumps({'upstream_commit': 'b' * 40}))
            (output / 'release.json').write_text(json.dumps({'source_sha': SOURCE}))
            with self.assertRaisesRegex(ValueError, 'differs'):
                automation.add_metadata(output, output / 'release.json', 'c' * 40)


if __name__ == '__main__':
    unittest.main()
