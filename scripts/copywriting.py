#!/usr/bin/env python3
"""Read pinned local copywriting sources; stdlib only, no writes or network.

Section parsing and pinned-blob checks adapted from promt-gkkd's
scripts/context.py and scripts/check_dependencies.py.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import unicodedata


REPOSITORY = Path(__file__).resolve().parents[1]
MANIFEST = REPOSITORY / 'dependencies.json'
DEFAULT_ROOT = REPOSITORY.parent / 'promt-copywriting'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def valid_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or
            any(ord(c) < 32 or unicodedata.category(c) == 'Cf' for c in value) or
            PurePosixPath(value).is_absolute() or
            any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('invalid relative file path')
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate manifest key')
        result[key] = value
    return result


def read_manifest(path):
    data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != {'dependencies'}:
        raise ValueError('invalid manifest schema')
    deps = data['dependencies']
    if not isinstance(deps, list) or len(deps) != 1:
        raise ValueError('manifest requires one dependency')
    item = deps[0]
    if (not isinstance(item, dict) or set(item) != {'name', 'url', 'revision', 'files'} or
            item['name'] != 'promt-copywriting' or
            item['url'] != 'https://github.com/BermanRoman/promt-copywriting.git'):
        raise ValueError('invalid dependency schema/source')
    if not isinstance(item['revision'], str) or not re.fullmatch('[0-9a-f]{40}', item['revision']):
        raise ValueError('revision must be an exact 40-character SHA')
    if not isinstance(item['files'], dict) or not item['files']:
        raise ValueError('files must be a nonempty SHA-256 mapping')
    for name, digest in item['files'].items():
        valid_path(name)
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid file SHA-256')
    return item


def git(root, *args):
    return subprocess.run(
        ['git', '--no-replace-objects', '--no-optional-locks', '-C', str(root), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=True, timeout=10,
    ).stdout


def check_dependency(root, item):
    root = Path(root).absolute()
    if root != root.resolve() or not root.is_dir():
        raise ValueError('root must be a real checkout directory without symlinks')
    top = Path(git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    if top != root:
        raise ValueError('root must equal the Git top level')
    if git(root, 'rev-parse', 'HEAD').decode().strip() != item['revision']:
        raise ValueError('checkout HEAD differs from pinned revision')
    sources = {}
    for name, digest in item['files'].items():
        valid_path(name)
        parts = PurePosixPath(name).parts
        target = root / name
        if (any(root.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1)) or
                not target.is_file() or root not in target.resolve().parents):
            raise ValueError(name + ': missing or invalid required file')
        entry = git(root, 'ls-tree', '-z', item['revision'], '--', name)
        if not entry.startswith((b'100644 blob ', b'100755 blob ')):
            raise ValueError(name + ': pinned source is not a regular file')
        actual = target.read_bytes()
        pinned = git(root, 'show', item['revision'] + ':' + name)
        # Read bytes directly: assume-unchanged/skip-worktree can hide edits.
        if actual != pinned or sha(actual) != digest:
            raise ValueError(name + ': bytes differ from pinned source/SHA-256')
        sources[name] = actual
    return sources


def headings(text):
    """Return (exact label, depth, character offset), ignoring fenced blocks."""
    if any(unicodedata.category(c) == 'Cf' for c in text):
        raise ValueError('source contains hidden format characters')
    result, fence, offset = [], None, 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip('\r\n')
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', raw)
        if marker:
            run, tail = marker.groups()
            if fence is None:
                # A backtick fence's info string cannot contain backticks.
                if run[0] != '`' or '`' not in tail:
                    fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
        elif fence is None:
            match = re.match(r'^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$', raw)
            if match:
                label = re.sub(r'(?:^|[ \t]+)#+[ \t]*$', '', match[2] or '').strip()
                result.append((label, len(match[1]), offset))
        offset += len(line)
    return result


def sections(text, selectors):
    if len(set(selectors)) != len(selectors):
        raise ValueError('duplicate heading selector')
    table = headings(text)
    selected = []
    for selector in selectors:
        matches = [i for i, row in enumerate(table) if row[0] == selector]
        if len(matches) != 1:
            raise ValueError('heading must occur exactly once: ' + selector)
        index = matches[0]
        _, level, start = table[index]
        end = next((pos for _, depth, pos in table[index + 1:] if depth <= level), len(text))
        selected.append((start, end))
    merged = []
    for start, end in sorted(selected):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return '\n'.join(text[start:end] for start, end in merged)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT, help='local copywriting checkout')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('check', 'index', 'extract'):
        sub = commands.add_parser(command)
        sub.add_argument('--root', type=Path, default=argparse.SUPPRESS)
        if command != 'check':
            sub.add_argument('file', metavar='FILE')
        if command == 'extract':
            sub.add_argument('--heading', action='append', default=[], help='exact ATX label; repeatable')
    args = parser.parse_args(argv)
    try:
        item = read_manifest(MANIFEST)
        if args.command != 'check' and valid_path(args.file) not in item['files']:
            raise ValueError('file is not declared in dependencies.json')
        sources = check_dependency(args.root, item)
        if args.command == 'check':
            result = 'promt-copywriting: ok\n'
        else:
            data = sources[args.file]
            text = data.decode('utf-8')
            if args.command == 'index':
                result = ''.join('#' * level + ' ' + label + '\n' for label, level, _ in headings(text))
            else:
                result = sections(text, args.heading) if args.heading else text
            print('read SHA-256: ' + sha(data) + '; selected SHA-256: ' +
                  sha(result.encode('utf-8')) + '; characters: ' + str(len(result)), file=sys.stderr)
    except (OSError, UnicodeError, ValueError, TypeError, subprocess.SubprocessError) as error:
        # Do not print subprocess arguments, file contents or tracebacks.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print('copywriting: ' + detail, file=sys.stderr)
        return 2
    sys.stdout.write(result)
    return 0


if __name__ == '__main__':
    sys.exit(main())
