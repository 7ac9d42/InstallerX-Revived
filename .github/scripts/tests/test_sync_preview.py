import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'sync-preview.py'
PROTECTED_PATHS = (
    '.github/workflows', '.github/scripts', '.github/aosp-module', 'docs/AOSP_BUILD.md',
)


class SyncPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='sync-preview-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = self.root / 'upstream'
        self.fork = self.root / 'fork'
        self.environment = os.environ.copy()
        self.environment.update({
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_AUTHOR_NAME': 'Sync Test',
            'GIT_AUTHOR_EMAIL': 'sync@example.invalid',
            'GIT_COMMITTER_NAME': 'Sync Test',
            'GIT_COMMITTER_EMAIL': 'sync@example.invalid',
        })
        self.git(self.root, 'init', '--initial-branch=main', str(self.upstream))
        for path in (
            'src/main.txt', 'src/deleted.txt', '.github/workflows/preview.yml',
            '.github/scripts/helper.py', '.github/aosp-module/module.prop', 'docs/AOSP_BUILD.md',
        ):
            self.write(self.upstream, path, 'base\n')
        self.base = self.commit(self.upstream, 'base')
        self.git(self.root, 'clone', '--no-hardlinks', str(self.upstream), str(self.fork))

    def git(self, repo, *args, check=True):
        return subprocess.run(
            ['git', '-C', str(repo), *args], check=check, capture_output=True,
            text=True, encoding='utf-8', env=self.environment, timeout=30,
        )

    def write(self, repo, name, content):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')

    def commit(self, repo, message):
        self.git(repo, 'add', '--all')
        self.git(repo, 'commit', '-m', message)
        return self.git(repo, 'rev-parse', 'HEAD').stdout.strip()

    def run_sync(self, source, success=True):
        result = subprocess.run(
            [sys.executable, '-I', str(SCRIPT), '--repo-dir', str(self.fork),
             '--source-sha', source, '--upstream', str(self.upstream)],
            capture_output=True, text=True, encoding='utf-8',
            env=self.environment, timeout=60,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def protected_tree(self):
        return self.git(
            self.fork, 'ls-tree', '-r', 'HEAD', '--', *PROTECTED_PATHS,
        ).stdout

    def assert_clean(self):
        self.assertEqual(self.git(self.fork, 'status', '--porcelain=v1').stdout, '')
        self.assertNotEqual(
            self.git(self.fork, 'rev-parse', '--verify', '-q', 'MERGE_HEAD', check=False).returncode, 0,
        )

    def assert_merge(self, before, source):
        parents = self.git(self.fork, 'show', '-s', '--format=%P', 'HEAD').stdout.strip().split()
        self.assertEqual(parents, [before, source])
        self.assertEqual(
            self.git(self.fork, 'show', '-s', '--format=%s', 'HEAD').stdout.strip(),
            f'fix(sync): 同步上游预发布 {source[:7]}',
        )
        self.assert_clean()

    def test_diverged_merge_preserves_entire_protected_tree(self):
        self.write(self.fork, '.github/workflows/preview.yml', 'fork workflow\n')
        self.write(self.fork, '.github/scripts/fork-only.py', 'fork script\n')
        self.write(self.fork, 'docs/AOSP_BUILD.md', 'fork docs\n')
        self.write(self.fork, 'src/fork-only.txt', 'fork source\n')
        before = self.commit(self.fork, 'fork changes')
        protected_before = self.protected_tree()
        self.write(self.upstream, '.github/workflows/preview.yml', 'upstream workflow\n')
        self.write(self.upstream, '.github/workflows/new.yml', 'upstream new workflow\n')
        self.git(self.upstream, 'rm', '.github/scripts/helper.py')
        self.write(self.upstream, '.github/scripts/upstream.py', 'upstream script\n')
        self.write(self.upstream, '.github/aosp-module/module.prop', 'upstream module\n')
        self.write(self.upstream, '.github/aosp-module/new.prop', 'upstream new module\n')
        self.write(self.upstream, 'docs/AOSP_BUILD.md', 'upstream docs\n')
        self.write(self.upstream, 'src/main.txt', 'upstream source\n')
        self.write(self.upstream, 'src/new.txt', 'new source\n')
        self.git(self.upstream, 'rm', 'src/deleted.txt')
        source = self.commit(self.upstream, 'upstream changes')

        self.run_sync(source)

        self.assertEqual(self.protected_tree(), protected_before)
        self.assertFalse((self.fork / '.github/workflows/new.yml').exists())
        self.assertFalse((self.fork / '.github/scripts/upstream.py').exists())
        self.assertFalse((self.fork / '.github/aosp-module/new.prop').exists())
        self.assertEqual((self.fork / 'src/main.txt').read_text(), 'upstream source\n')
        self.assertEqual((self.fork / 'src/new.txt').read_text(), 'new source\n')
        self.assertEqual((self.fork / 'src/fork-only.txt').read_text(), 'fork source\n')
        self.assertFalse((self.fork / 'src/deleted.txt').exists())
        self.assert_merge(before, source)

    def test_fast_forward_candidate_still_restores_protection_before_commit(self):
        protected_before = self.protected_tree()
        self.write(self.upstream, '.github/workflows/new.yml', 'new workflow\n')
        self.git(self.upstream, 'rm', '.github/workflows/preview.yml')
        self.write(self.upstream, 'src/main.txt', 'upstream source\n')
        source = self.commit(self.upstream, 'upstream ahead')

        self.run_sync(source)

        self.assertEqual(self.protected_tree(), protected_before)
        self.assertFalse((self.fork / '.github/workflows/new.yml').exists())
        self.assertEqual((self.fork / 'src/main.txt').read_text(), 'upstream source\n')
        self.assert_merge(self.base, source)

    def test_protected_paths_absent_in_fork_remain_absent(self):
        self.git(self.fork, 'rm', '-r', '--', *PROTECTED_PATHS)
        before = self.commit(self.fork, 'fork removes protected paths')
        self.write(self.upstream, '.github/workflows/new.yml', 'new workflow\n')
        self.write(self.upstream, '.github/scripts/helper.py', 'changed script\n')
        self.write(self.upstream, '.github/aosp-module/module.prop', 'changed module\n')
        self.write(self.upstream, 'docs/AOSP_BUILD.md', 'changed docs\n')
        self.write(self.upstream, 'src/main.txt', 'upstream source\n')
        source = self.commit(self.upstream, 'upstream adds and modifies protection')

        self.run_sync(source)

        self.assertEqual(self.protected_tree(), '')
        for path in PROTECTED_PATHS:
            self.assertFalse((self.fork / path).exists())
        self.assert_merge(before, source)

    def test_protected_directory_file_conflict(self):
        self.write(self.fork, '.github/scripts/helper.py', 'fork script\n')
        before = self.commit(self.fork, 'fork modifies script')
        protected_before = self.protected_tree()
        self.git(self.upstream, 'rm', '.github/scripts/helper.py')
        self.write(self.upstream, '.github/scripts/helper.py/nested.py', 'nested script\n')
        self.write(self.upstream, 'src/main.txt', 'upstream source\n')
        source = self.commit(self.upstream, 'upstream replaces file with directory')

        self.run_sync(source)

        self.assertEqual(self.protected_tree(), protected_before)
        self.assertEqual((self.fork / '.github/scripts/helper.py').read_text(), 'fork script\n')
        self.assert_merge(before, source)

    def test_protected_root_directory_file_conflict(self):
        self.write(self.fork, '.github/workflows/preview.yml', 'fork workflow\n')
        before = self.commit(self.fork, 'fork modifies workflow')
        protected_before = self.protected_tree()
        self.git(self.upstream, 'rm', '-r', '.github/workflows')
        self.write(self.upstream, '.github/workflows', 'upstream replaces directory\n')
        self.write(self.upstream, 'src/main.txt', 'upstream source\n')
        source = self.commit(self.upstream, 'upstream replaces protected root')

        self.run_sync(source)

        self.assertEqual(self.protected_tree(), protected_before)
        self.assert_merge(before, source)

    def test_protected_root_file_directory_conflict(self):
        self.git(self.fork, 'rm', '-r', '.github/workflows')
        self.write(self.fork, '.github/workflows', 'fork file\n')
        before = self.commit(self.fork, 'fork replaces protected root')
        protected_before = self.protected_tree()
        self.write(self.upstream, '.github/workflows/preview.yml', 'upstream workflow\n')
        source = self.commit(self.upstream, 'upstream modifies workflow')

        self.run_sync(source)

        self.assertEqual(self.protected_tree(), protected_before)
        self.assertEqual((self.fork / '.github/workflows').read_text(), 'fork file\n')
        self.assert_merge(before, source)

    def test_real_file_with_conflict_alias_name_is_not_automatically_resolved(self):
        name = '.github/workflows~HEAD'
        self.write(self.upstream, name, 'base alias-named file\n')
        base = self.commit(self.upstream, 'add non-protected file')
        self.git(self.fork, 'fetch', str(self.upstream), base)
        self.git(self.fork, 'merge', '--ff-only', base)
        self.write(self.fork, name, 'fork content\n')
        before = self.commit(self.fork, 'fork changes alias-named file')
        self.write(self.upstream, name, 'upstream content\n')
        source = self.commit(self.upstream, 'upstream changes alias-named file')

        self.run_sync(source, success=False)

        self.assertEqual(self.git(self.fork, 'rev-parse', 'HEAD').stdout.strip(), before)
        self.assertEqual((self.fork / name).read_text(), 'fork content\n')
        self.assert_clean()

    def test_source_conflict_aborts_even_with_protected_conflicts(self):
        self.write(self.fork, 'src/main.txt', 'fork source\n')
        self.write(self.fork, '.github/workflows/preview.yml', 'fork workflow\n')
        before = self.commit(self.fork, 'fork conflicts')
        tree_before = self.git(self.fork, 'ls-tree', '-r', 'HEAD').stdout
        self.write(self.upstream, 'src/main.txt', 'upstream source\n')
        self.write(self.upstream, '.github/workflows/preview.yml', 'upstream workflow\n')
        self.write(self.upstream, '.github/workflows/new.yml', 'new workflow\n')
        source = self.commit(self.upstream, 'upstream conflicts')

        result = self.run_sync(source, success=False)

        self.assertIn('src/main.txt', result.stderr)
        self.assertEqual(self.git(self.fork, 'rev-parse', 'HEAD').stdout.strip(), before)
        self.assertEqual(self.git(self.fork, 'ls-tree', '-r', 'HEAD').stdout, tree_before)
        self.assertEqual((self.fork / 'src/main.txt').read_text(), 'fork source\n')
        self.assertFalse((self.fork / '.github/workflows/new.yml').exists())
        self.assert_clean()

    def test_protected_only_merge_records_history_without_repeated_commit(self):
        self.write(self.upstream, '.github/workflows/new.yml', 'new workflow\n')
        source = self.commit(self.upstream, 'protected only')
        tree_before = self.git(self.fork, 'rev-parse', 'HEAD^{tree}').stdout
        self.run_sync(source)
        self.assert_merge(self.base, source)
        self.assertEqual(self.git(self.fork, 'rev-parse', 'HEAD^{tree}').stdout, tree_before)
        head = self.git(self.fork, 'rev-parse', 'HEAD').stdout

        self.run_sync(source)
        self.run_sync(self.base)

        self.assertEqual(self.git(self.fork, 'rev-parse', 'HEAD').stdout, head)
        self.assert_clean()

    def test_already_up_to_date_creates_no_commit(self):
        self.run_sync(self.base)
        self.assertEqual(self.git(self.fork, 'rev-parse', 'HEAD').stdout.strip(), self.base)
        self.assert_clean()

    def check_dirty_checkout(self, name, staged=False):
        self.write(self.upstream, 'src/main.txt', 'upstream source\n')
        source = self.commit(self.upstream, 'upstream change')
        self.write(self.fork, name, 'user work\n')
        if staged:
            self.git(self.fork, 'add', '--', name)
        status = self.git(self.fork, 'status', '--porcelain=v1').stdout
        index = self.git(self.fork, 'ls-files', '--stage').stdout
        head = self.git(self.fork, 'rev-parse', 'HEAD').stdout

        result = self.run_sync(source, success=False)

        self.assertIn('clean', result.stderr)
        self.assertEqual(self.git(self.fork, 'status', '--porcelain=v1').stdout, status)
        self.assertEqual(self.git(self.fork, 'ls-files', '--stage').stdout, index)
        self.assertEqual(self.git(self.fork, 'rev-parse', 'HEAD').stdout, head)
        self.assertEqual((self.fork / name).read_text(), 'user work\n')
        self.assertFalse((self.fork / '.git/FETCH_HEAD').exists())

    def test_unstaged_work_is_untouched(self):
        self.check_dirty_checkout('src/main.txt')

    def test_staged_work_is_untouched(self):
        self.check_dirty_checkout('src/main.txt', staged=True)

    def test_untracked_work_is_untouched(self):
        self.check_dirty_checkout('.github/workflows/user.yml')

    def test_invalid_sha_is_rejected_before_fetch(self):
        self.run_sync('1234567', success=False)
        self.assertFalse((self.fork / '.git/FETCH_HEAD').exists())
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
