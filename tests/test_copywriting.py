"""Offline contract tests for the pinned copywriting reader."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('copywriting', REPOSITORY / 'scripts/copywriting.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
TEXT = '# One\nBody\n## Child\nDetails\n```md\n# Fake\n```\n# Two\nEnd\n'


class CopywritingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'source'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'source.md').write_text(TEXT, encoding='utf-8')
        (self.root / 'extra.md').write_text('# Extra\n', encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.item = {
            'name': 'promt-copywriting',
            'url': 'https://github.com/BermanRoman/promt-copywriting.git',
            'revision': self.git('rev-parse', 'HEAD').decode().strip(),
            'files': {name: cli.sha((self.root / name).read_bytes())
                      for name in ('source.md', 'extra.md')},
        }
        self.manifest = self.base / 'dependencies.json'
        self.save_manifest()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def save_manifest(self):
        self.manifest.write_text(json.dumps({'dependencies': [self.item]}), encoding='utf-8')

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, 'MANIFEST', self.manifest), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--root', str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_full_file_index_sections_and_diagnostics(self):
        code, out, err = self.run_cli('extract', 'source.md')
        self.assertEqual((code, out), (0, TEXT))
        self.assertIn('read SHA-256: ' + cli.sha(TEXT.encode()), err)
        self.assertIn('selected SHA-256:', err)
        self.assertIn('characters: ' + str(len(TEXT)), err)
        self.assertNotIn('tokens', err)
        self.assertEqual(self.run_cli('index', 'source.md')[1], '# One\n## Child\n# Two\n')
        self.assertEqual(self.run_cli('extract', 'source.md', '--heading', 'One',
                                      '--heading', 'Child')[1], TEXT[:TEXT.index('# Two')])
        self.assertEqual(self.run_cli('check')[:2], (0, 'promt-copywriting: ok\n'))
        self.assertEqual(self.run_cli('check', '--root', str(self.root))[0], 0)

    def test_unknown_duplicate_ambiguous_and_fenced_headings_fail_atomically(self):
        for selectors in [('One', 'Missing'), ('Fake',), ('One', 'One')]:
            args = [part for name in selectors for part in ('--heading', name)]
            code, out, err = self.run_cli('extract', 'source.md', *args)
            self.assertEqual((code, out), (2, ''))
            self.assertNotIn('Traceback', err)
        with self.assertRaises(ValueError):
            cli.sections('# Same\na\n# Same\nb\n', ['Same'])

    def test_fences_closing_markers_and_source_order(self):
        text = '# A ###\nx\n~~~\n# Hidden\n~~~~\n## B\ny\n# C\nz\n'
        self.assertEqual([row[0] for row in cli.headings(text)], ['A', 'B', 'C'])
        self.assertEqual(cli.sections(text, ['C', 'A']), text)
        self.assertEqual([row[0] for row in cli.headings('``` bad`info\n# Visible\n')], ['Visible'])
        self.assertEqual(cli.headings('#\n')[0][0], '')

    def test_unrelated_dirty_files_allowed(self):
        (self.root / 'unrelated.txt').write_text('untracked')
        self.git('add', 'unrelated.txt')
        (self.root / 'unrelated.txt').write_text('dirty')
        self.assertEqual(self.run_cli('check')[0], 0)

    def test_hidden_edit_in_another_required_file_blocks_extract(self):
        self.git('update-index', '--assume-unchanged', 'extra.md')
        (self.root / 'extra.md').write_text('hidden edit')
        self.assertEqual(self.git('status', '--porcelain'), b'')
        self.assertEqual(self.run_cli('extract', 'source.md')[:2], (2, ''))

    def test_sha_mismatch_wrong_revision_and_nested_root(self):
        self.item['files']['source.md'] = '0' * 64
        self.save_manifest()
        self.assertEqual(self.run_cli('check')[0], 2)
        self.item['files']['source.md'] = cli.sha(TEXT.encode())
        self.item['revision'] = '0' * 40
        self.save_manifest()
        self.assertEqual(self.run_cli('check')[0], 2)
        with self.assertRaises(ValueError):
            cli.check_dependency(self.root / 'nested', self.item)
        (self.root / 'nested').mkdir()
        with self.assertRaisesRegex(ValueError, 'top level'):
            cli.check_dependency(self.root / 'nested', self.item)

    def test_replace_objects_cannot_redefine_pinned_source(self):
        original = self.git('rev-parse', 'HEAD:source.md').decode().strip()
        (self.root / 'tampered').write_text('replacement bytes')
        replacement = self.git('hash-object', '-w', 'tampered').decode().strip()
        self.git('replace', original, replacement)
        self.assertEqual(self.run_cli('check')[0], 0)
        self.git('update-index', '--assume-unchanged', 'source.md')
        (self.root / 'source.md').write_text('replacement bytes')
        self.item['files']['source.md'] = cli.sha(b'replacement bytes')
        self.save_manifest()
        self.assertEqual(self.run_cli('extract', 'source.md')[:2], (2, ''))

    def test_unknown_invalid_and_symlink_paths(self):
        for name in ('unknown.md', '../source.md', '/source.md', 'a//b', 'a/./b', 'a\\b'):
            self.assertEqual(self.run_cli('extract', name)[:2], (2, ''))
        (self.root / 'source.md').unlink()
        (self.root / 'source.md').symlink_to(self.root / 'extra.md')
        self.assertEqual(self.run_cli('check')[0], 2)
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            cli.check_dependency(alias, self.item)

    def test_symlink_directory_and_pinned_symlink_rejected(self):
        (self.root / 'links').symlink_to(self.base, target_is_directory=True)
        self.item['files'] = {'links/dependencies.json': cli.sha(self.manifest.read_bytes())}
        self.save_manifest()
        self.assertEqual(self.run_cli('check')[0], 2)
        (self.root / 'link.md').symlink_to('extra.md')
        self.git('add', 'link.md')
        self.git('commit', '-qm', 'symlink')
        (self.root / 'link.md').unlink()
        (self.root / 'link.md').write_text('extra.md')
        self.item['revision'] = self.git('rev-parse', 'HEAD').decode().strip()
        self.item['files'] = {'link.md': cli.sha(b'extra.md')}
        self.save_manifest()
        self.assertEqual(self.run_cli('check')[0], 2)

    def test_strict_manifest(self):
        for edit in (lambda d: d.update(extra=True),
                     lambda d: d.update(revision='HEAD'),
                     lambda d: d.update(files={'../escape': '0' * 64}),
                     lambda d: d.update(files={'source.md': 42}),
                     lambda d: d.update(files=[])):
            original = self.item.copy()
            edit(self.item)
            self.save_manifest()
            self.assertEqual(self.run_cli('check')[:2], (2, ''))
            self.item = original
        self.manifest.write_text('{"dependencies": [], "dependencies": []}')
        self.assertEqual(self.run_cli('check')[:2], (2, ''))

    def test_cwd_independence_with_temporary_checkouts(self):
        consumer = self.base / 'consumer'
        scripts = consumer / 'scripts'
        scripts.mkdir(parents=True)
        reader = scripts / 'copywriting.py'
        reader.write_bytes((REPOSITORY / 'scripts/copywriting.py').read_bytes())
        (consumer / 'dependencies.json').write_bytes(self.manifest.read_bytes())
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        # An explicit root works even when the default sibling does not exist.
        result = subprocess.run(['python3', str(reader), '--root', str(self.root), 'check'],
                                cwd=elsewhere, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'promt-copywriting: ok\n')
        # The default is relative to the copied reader, not the process cwd.
        self.root.rename(self.base / 'promt-copywriting')
        result = subprocess.run(['python3', str(reader), 'check'],
                                cwd=elsewhere, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'promt-copywriting: ok\n')


if __name__ == '__main__':
    unittest.main()
