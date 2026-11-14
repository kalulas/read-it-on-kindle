#!/usr/bin/env python3
"""把网络文章做成 Kindle 友好的 epub 并推送到 Kindle。

  python read_it_on_kindle.py https://mp.weixin.qq.com/s/<id> --recipe wechat
  python read_it_on_kindle.py <url> --recipe generic --title '自己起的书名'
  python read_it_on_kindle.py <xxx.epub> --clean zhihu
  python read_it_on_kindle.py <url> --recipe generic --no-send
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / 'config.local.json'

if sys.platform == 'darwin':
    CALIBRE_DIRS = ['/Applications/calibre.app/Contents/MacOS']
elif sys.platform == 'win32':
    CALIBRE_DIRS = [os.path.expandvars(r'%ProgramFiles%\Calibre2')]
else:
    CALIBRE_DIRS = []

INVALID_FILENAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def load_config():
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            sys.exit('Failed to read %s: %s' % (CONFIG_FILE.name, exc))
    return {}


def setting(config, key, cli_value=None, default=None):
    if cli_value not in (None, ''):
        return cli_value
    env = os.environ.get('KINDLE_' + key.upper())
    if env:
        return env
    if config.get(key) not in (None, ''):
        return config[key]
    return default


def resolve_calibre_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for directory in CALIBRE_DIRS:
        for candidate in (Path(directory) / name, Path(directory) / (name + '.exe')):
            if candidate.is_file():
                return str(candidate)
    sys.exit('%s not found. Install calibre (macOS: brew install --cask calibre) '
             'or add its install directory to PATH.' % name)


def run(cmd, env=None):
    return subprocess.run(cmd, env=env, capture_output=True,
                          text=True, encoding='utf-8', errors='replace')


def describe_failure(result, n=5):
    parts = []
    for stream in (result.stdout, result.stderr):
        lines = [l for l in (stream or '').splitlines() if l.strip()][-n:]
        if lines:
            parts.append('\n'.join(lines))
    return '\n'.join(parts)


def safe_filename(title, limit=120):
    name = ''.join('_' if c in INVALID_FILENAME_CHARS else c for c in title)
    name = name.strip().rstrip('.')
    return name[:limit] or 'untitled-article'


def resolve_recipe(value):
    path = Path(value)
    if path.is_file():
        return path
    if path.suffix != '.recipe':
        path = path.with_suffix('.recipe')
    path = HERE / path.name
    if path.is_file():
        return path
    sys.exit('Recipe not found: %s' % value)


def resolve_clean_preset(value):
    path = Path(value)
    if path.is_file():
        return path
    if not path.name.endswith('.clean.json'):
        path = Path(path.name + '.clean.json')
    path = HERE / path.name
    if path.is_file():
        return path
    sys.exit('Clean preset not found: %s' % value)


def load_clean_preset(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        sys.exit('Failed to read %s: %s' % (path.name, exc))


def clean_local_file(ebook_convert, src, tmp, preset):
    cmd = [ebook_convert, str(src), str(tmp)]
    if preset.get('filter_css'):
        cmd += ['--filter-css', preset['filter_css']]
    if preset.get('language'):
        cmd += ['--language', preset['language']]
    if preset.get('authors'):
        cmd += ['--authors', preset['authors']]

    rules = preset.get('search_replace') or []
    sr_file = None
    if rules:
        sr_file = tmp.with_suffix('.sr.txt')
        sr_file.write_text(''.join('%s\n%s\n' % (pattern, repl) for pattern, repl in rules),
                           encoding='utf-8')
        cmd += ['--search-replace', str(sr_file)]
    try:
        return run(cmd)
    finally:
        if sr_file is not None and sr_file.exists():
            sr_file.unlink()


def unique_path(directory, stem, suffix='.epub'):
    dest = directory / (stem + suffix)
    index = 2
    while dest.exists():
        dest = directory / ('%s (%d)%s' % (stem, index, suffix))
        index += 1
    return dest


def read_metadata(ebook_meta, epub):
    result = run([ebook_meta, str(epub)])
    title = author = ''
    for line in result.stdout.splitlines():
        m = re.match(r'^Title\s*:\s*(.+)$', line)
        if m and not title:
            title = m.group(1).strip()
        m = re.match(r'^Author\(s\)\s*:\s*(.+)$', line)
        if m and not author:
            author = re.sub(r'\s*\[.*\]\s*$', '', m.group(1).strip())
    return title, author


def send_to_kindle(dest, title, config, args):
    mail_from = setting(config, 'mail_from', args.mail_from)
    mail_to = setting(config, 'mail_to', args.mail_to)
    relay = setting(config, 'smtp_relay', args.smtp_relay, 'smtp.gmail.com')
    port = str(setting(config, 'smtp_port', args.smtp_port, 587))
    password = setting(config, 'smtp_password')

    missing = [k for k, v in (('mail_from', mail_from), ('mail_to', mail_to),
                              ('smtp_password', password)) if not v]
    if missing:
        sys.exit('Missing settings: %s. Set them in %s or as KINDLE_* environment '
                 'variables (see config.example.json).' % (', '.join(missing), CONFIG_FILE.name))

    calibre_smtp = resolve_calibre_tool('calibre-smtp')
    print('Sending to: %s' % mail_to)

    result = run([calibre_smtp, '--attachment', str(dest), '--subject', title,
                  '--relay', relay, '--port', port, '--encryption-method', 'TLS',
                  '--username', mail_from, '--password', password,
                  mail_from, mail_to, title])
    if result.returncode != 0:
        print('WARNING: calibre-smtp failed (exit %d). The epub is kept at %s'
              % (result.returncode, dest), file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip().splitlines()[-1], file=sys.stderr)
        return False
    print('Successfully Sent!')
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Convert a web article into a Kindle-friendly epub and send it to Kindle.')
    parser.add_argument('source', help='article URL, or a local .epub / .html file')
    parser.add_argument('--recipe',
                        help='for URLs: recipe name (<name>.recipe next to this script) or a path')
    parser.add_argument('--clean',
                        help='for local files: preset name (<name>.clean.json next to this script) or a path')
    parser.add_argument('--title', help='override the book title (no extension)')
    parser.add_argument('--out-dir', help='output directory (default: ./output)')
    parser.add_argument('--force', action='store_true', help='overwrite instead of adding (2)(3)')
    parser.add_argument('--no-send', dest='send', action='store_false', default=True,
                        help='only produce the file, do not send to Kindle')
    parser.add_argument('--mail-from')
    parser.add_argument('--mail-to')
    parser.add_argument('--smtp-relay')
    parser.add_argument('--smtp-port')
    args = parser.parse_args()

    src = Path(args.source).expanduser()
    local = src.is_file()
    if not local and not re.match(r'https?://', args.source):
        parser.error('file not found: %s' % args.source)
    if local and not args.clean:
        parser.error('local files need --clean (e.g. --clean zhihu)')
    if not local and not args.recipe:
        parser.error('URLs need --recipe (e.g. --recipe generic)')
    if local and args.recipe:
        parser.error('--recipe only applies to URLs; use --clean for local files')
    if not local and args.clean:
        parser.error('--clean only applies to local files; use --recipe for URLs')

    config = load_config()
    ebook_convert = resolve_calibre_tool('ebook-convert')
    ebook_meta = resolve_calibre_tool('ebook-meta')
    recipe = None if local else resolve_recipe(args.recipe)
    preset = load_clean_preset(resolve_clean_preset(args.clean)) if local else None

    out_dir = Path(setting(config, 'out_dir', args.out_dir, HERE / 'output')).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.gettempdir()) / ('article-%s.epub' % uuid.uuid4().hex)

    try:
        if local:
            print('Cleaning: %s' % src)
            result = clean_local_file(ebook_convert, src, tmp, preset)
        else:
            env = dict(os.environ, ARTICLE_URL=args.source)
            print('Fetching: %s' % args.source)
            result = run([ebook_convert, str(recipe), str(tmp)], env=env)
        if result.returncode != 0 or not tmp.is_file():
            sys.exit('ebook-convert failed (exit %d).\n%s'
                     % (result.returncode, describe_failure(result)))

        if args.title:
            result = run([ebook_meta, str(tmp), '--title', args.title])
            if result.returncode != 0:
                sys.exit('ebook-meta failed to set the title (exit %d).\n%s'
                         % (result.returncode, describe_failure(result)))

        title, author = read_metadata(ebook_meta, tmp)
        title = title or 'untitled-article'

        dest = out_dir / (safe_filename(title) + '.epub')
        same_file = local and dest.exists() and dest.resolve() == src.resolve()
        if not (args.force or same_file):
            dest = unique_path(out_dir, safe_filename(title))

        shutil.move(str(tmp), str(dest))
        print('Done. Output: %s' % dest)
        print('  Title  : %s' % title)
        print('  Author : %s' % author)

        if args.send:
            send_to_kindle(dest, title, config, args)
    finally:
        if tmp.exists():
            tmp.unlink()


if __name__ == '__main__':
    main()
