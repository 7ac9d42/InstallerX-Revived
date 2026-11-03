import argparse
import os
from pathlib import Path
import re
import subprocess
import sys


UPSTREAM = 'https://github.com/wxxsfxyzm/InstallerX-Revived.git'
PROTECTED_PATHS = (
    '.github/workflows',
    '.github/scripts',
    '.github/aosp-module',
    'docs/AOSP_BUILD.md',
)


def git(repo, *args, check=True):
    environment = os.environ.copy()
    environment['GIT_TERMINAL_PROMPT'] = '0'
    environment['GIT_MERGE_AUTOEDIT'] = 'no'
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=environment, check=check, timeout=180,
    )


def protected(path):
    return any(path == root or path.startswith(root + '/') for root in PROTECTED_PATHS)


def conflict_aliases(repo, before, source, conflicts):
    aliases = []
    # 根目录发生文件/目录冲突时，Git 会把文件暂存为 root~HEAD 或 root~SHA。
    for root in PROTECTED_PATHS:
        for suffix, revision, stage in (('HEAD', before, '2'), (source, source, '3')):
            alias = root + '~' + suffix
            if alias not in conflicts:
                continue
            if any(git(repo, 'ls-tree', ref, '--', alias).stdout for ref in (before, source)):
                continue
            entry = git(repo, 'ls-tree', revision, '--', root).stdout
            if not entry:
                continue
            mode, kind, object_id = entry.split('\t', 1)[0].split()
            expected = f'{mode} {object_id} {stage}\t{alias}\0'
            if kind == 'blob' and git(repo, 'ls-files', '--unmerged', '-z', '--', alias).stdout == expected:
                aliases.append(alias)
    return aliases


def sync(repo, source_sha, upstream):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', source_sha):
        raise ValueError('--source-sha must be a full 40-character hexadecimal SHA')
    repo = Path(git(repo, 'rev-parse', '--show-toplevel').stdout.strip())
    if git(repo, 'status', '--porcelain=v1', '--untracked-files=all').stdout:
        raise ValueError('Checkout must be clean, including untracked files')
    if git(repo, 'rev-parse', '--verify', '-q', 'MERGE_HEAD', check=False).returncode == 0:
        raise ValueError('Checkout already has a merge in progress')
    before = git(repo, 'rev-parse', 'HEAD').stdout.strip()
    git(repo, 'fetch', upstream, source_sha)
    fetched = git(repo, 'rev-parse', 'FETCH_HEAD^{commit}').stdout.strip()
    if fetched.lower() != source_sha.lower():
        raise ValueError('Fetched commit does not match --source-sha')
    ancestor = git(repo, 'merge-base', '--is-ancestor', fetched, before, check=False)
    if ancestor.returncode == 0:
        print('Upstream commit is already included; nothing to commit.')
        return
    if ancestor.returncode != 1:
        raise RuntimeError(ancestor.stderr.strip() or 'Cannot check upstream ancestry')

    try:
        merged = git(
            repo, 'merge', '--no-ff', '--no-commit', '--no-edit',
            '--no-overwrite-ignore', fetched, check=False,
        )
        conflicts = git(repo, 'diff', '--name-only', '--diff-filter=U', '-z').stdout.split('\0')
        conflicts = [path for path in conflicts if path]
        aliases = conflict_aliases(repo, before, fetched, conflicts)
        source_conflicts = [path for path in conflicts if not protected(path) and path not in aliases]
        if source_conflicts:
            raise RuntimeError('Non-protected merge conflicts: ' + ', '.join(source_conflicts))
        if merged.returncode and not conflicts:
            raise RuntimeError(merged.stderr.strip() or merged.stdout.strip() or 'Merge failed')

        # 先移除合并条目（包括未合并的新增文件），再恢复合并前整棵树。
        if aliases:
            git(repo, 'rm', '-f', '--', *aliases)
        git(repo, 'rm', '-r', '-f', '--ignore-unmatch', '--', *PROTECTED_PATHS)
        original = git(repo, 'ls-tree', '-r', '--name-only', '-z', before).stdout.split('\0')
        roots = [
            root for root in PROTECTED_PATHS
            if any(path == root or path.startswith(root + '/') for path in original)
        ]
        if roots:
            git(repo, 'restore', '--source=' + before, '--staged', '--worktree', '--', *roots)
        if git(repo, 'diff', '--name-only', '--diff-filter=U', '-z').stdout:
            raise RuntimeError('Unresolved conflicts remain after restoring protected paths')
        git(repo, 'commit', '-m', f'fix(sync): 同步上游预发布 {fetched[:7]}')
        print(f'Synced upstream {fetched}; protected paths preserved.')
    except BaseException:
        # 只撤销本次创建的合并；执行前已拒绝脏工作区及已有合并。
        if git(repo, 'rev-parse', '--verify', '-q', 'MERGE_HEAD', check=False).returncode == 0:
            git(repo, 'merge', '--abort')
        raise


def main():
    parser = argparse.ArgumentParser(description='Merge upstream preview while preserving fork automation.')
    parser.add_argument('--repo-dir', required=True, type=Path)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--upstream', default=UPSTREAM)
    args = parser.parse_args()
    try:
        sync(args.repo_dir, args.source_sha, args.upstream)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr.strip() or str(error), file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
