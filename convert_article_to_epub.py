#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按指定的 calibre recipe 把一篇网络文章抓成 Kindle 友好的 epub，默认直接推送到 Kindle。

书名默认取文章标题，输出到脚本目录下的 output/。Windows / macOS / Linux 通用。

  python convert_article_to_epub.py https://mp.weixin.qq.com/s/<id> --recipe wechat
  python convert_article_to_epub.py <url> --recipe generic --title '自己起的书名'
  python convert_article_to_epub.py <url> --recipe generic --no-send
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

# shutil.which 已经覆盖 PATH，这里只兜它真的会漏的那一种：
# macOS 官方 .dmg 装法不把 CLI 放进 PATH（Homebrew 的 cask 会 symlink，用不到这里）；
# Windows 安装器的"加入 PATH"是个可取消的选项，所以也留一条
if sys.platform == 'darwin':
    CALIBRE_DIRS = ['/Applications/calibre.app/Contents/MacOS']
elif sys.platform == 'win32':
    CALIBRE_DIRS = [os.path.expandvars(r'%ProgramFiles%\Calibre2')]
else:
    CALIBRE_DIRS = []

# Windows 的非法文件名字符是最严的一套，macOS 上也照这套清，
# 两台机器同步同一个 output/ 时文件名才不会打架
INVALID_FILENAME_CHARS = set('<>:"/\\|?*') | {chr(c) for c in range(32)}


def load_config():
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            sys.exit('Failed to read %s: %s' % (CONFIG_FILE.name, exc))
    return {}


def setting(config, key, cli_value=None, default=None):
    """取值优先级：命令行 > 环境变量 KINDLE_<KEY> > config.local.json > 默认值"""
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
    # encoding 写死 utf-8：calibre 的输出是 utf-8，不指定的话 Windows 会按 GBK 解码中文标题
    return subprocess.run(cmd, env=env, capture_output=True,
                          text=True, encoding='utf-8', errors='replace')


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

    # Amazon 不看主题和正文，但 calibre-smtp 的 compose 模式要求正文非空，
    # 否则它会转去读 STDIN 并挂住，所以这里把标题同时当主题和正文。
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
    parser.add_argument('url')
    parser.add_argument('--recipe', required=True,
                        help='recipe name (<name>.recipe next to this script) or a path')
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

    config = load_config()
    ebook_convert = resolve_calibre_tool('ebook-convert')
    ebook_meta = resolve_calibre_tool('ebook-meta')
    recipe = resolve_recipe(args.recipe)

    out_dir = Path(setting(config, 'out_dir', args.out_dir, HERE / 'output')).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.gettempdir()) / ('article-%s.epub' % uuid.uuid4().hex)

    try:
        # recipe 从这个环境变量读 URL
        env = dict(os.environ, ARTICLE_URL=args.url)

        print('Fetching: %s' % args.url)
        result = run([ebook_convert, str(recipe), str(tmp)], env=env)
        if result.returncode != 0 or not tmp.is_file():
            tail = [l for l in result.stdout.splitlines() if l.strip()][-5:]
            sys.exit('ebook-convert failed (exit %d).\n%s'
                     % (result.returncode, '\n'.join(tail)))

        if args.title:
            # 改写元数据里的书名，Kindle 书库显示的就是这个
            result = run([ebook_meta, str(tmp), '--title', args.title])
            if result.returncode != 0:
                sys.exit('ebook-meta failed to set the title (exit %d).' % result.returncode)

        # 标题和 author 已经写进元数据，直接读回来
        title, author = read_metadata(ebook_meta, tmp)
        title = title or 'untitled-article'

        dest = out_dir / (safe_filename(title) + '.epub')
        if not args.force:
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
