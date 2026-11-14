#!/usr/bin/env python3
"""把 Kindle 导出到 Gmail 的笔记本（标注 + 笔记）同步到 Notion 阅读笔记库。

  python sync_kindle_notes_to_notion.py
  python sync_kindle_notes_to_notion.py --dry-run
  python sync_kindle_notes_to_notion.py --file '<书名> - Notebook.html' --dry-run
"""
import argparse
import email
import html
import imaplib
import json
import os
import posixpath
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / 'config.local.json'
NOTION_API = os.environ.get('NOTION_API_BASE', 'https://api.notion.com/v1')
NOTION_VERSION = '2025-09-03'
SYNCED_LABEL = 'notion synced'
GMAIL_QUERY = 'filename:Notebook.html -label:%s' % SYNCED_LABEL.replace(' ', '-')
BLOCK_TAGS = r'</?(?:p|div|h[1-6]|li|blockquote|pre|br|tr|section|figure|figcaption|hr)\b[^>]*>'
CJK = '\u2e80-\u2fdf\u3040-\u30ff\u3100-\u312f\u3200-\u32ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff'
CJK_PUNCT = '，。！？；：、“”‘’（）《》【】「」『』'


def load_config():
    if not CONFIG_FILE.is_file():
        sys.exit('%s not found (see config.example.json).' % CONFIG_FILE.name)
    return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))


def setting(config, key, required=True):
    value = os.environ.get('KINDLE_' + key.upper()) or config.get(key)
    if required and not value:
        sys.exit('Missing setting: %s (config.local.json or KINDLE_%s).' % (key, key.upper()))
    return value


class NotebookParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = []
        self._cls = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get('class')
        if tag == 'div' and cls in ('bookTitle', 'authors', 'noteHeading', 'noteText'):
            self._cls, self._buf = cls, []

    def handle_endtag(self, tag):
        if tag == 'div' and self._cls:
            self.fields.append((self._cls, ' '.join(''.join(self._buf).split())))
            self._cls = None

    def handle_data(self, data):
        if self._cls:
            self._buf.append(data)


def parse_notebook(raw_html):
    parser = NotebookParser()
    parser.feed(raw_html)
    book = {'title': '', 'authors': '', 'items': []}
    heading = None
    for cls, text in parser.fields:
        if cls == 'bookTitle':
            book['title'] = text
        elif cls == 'authors':
            book['authors'] = text
        elif cls == 'noteHeading':
            heading = text
        elif cls == 'noteText' and heading is not None:
            if re.match(r'^(标注|Highlight)', heading, re.I):
                book['items'].append({'type': 'highlight', 'text': text, 'heading': heading, 'notes': []})
            elif re.match(r'^(笔记|备注|Note)', heading, re.I):
                if book['items'] and book['items'][-1]['type'] == 'highlight':
                    book['items'][-1]['notes'].append(text)
                else:
                    book['items'].append({'type': 'note', 'text': text, 'heading': heading, 'notes': []})
            heading = None
    if not book['title']:
        sys.exit('Not a Kindle notebook export: book title not found.')
    return book


def epub_title(zf):
    container = zf.read('META-INF/container.xml').decode('utf-8', 'replace')
    opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
    opf = zf.read(opf_path).decode('utf-8', 'replace')
    m = re.search(r'<dc:title[^>]*>(.*?)</dc:title>', opf, re.S)
    return (html.unescape(m.group(1)).strip() if m else ''), opf_path, opf


def find_epub(out_dir, title):
    want = ' '.join(title.split())
    hits = []
    for path in Path(out_dir).glob('*.epub'):
        try:
            with zipfile.ZipFile(path) as zf:
                if ' '.join(epub_title(zf)[0].split()) == want:
                    hits.append(path)
        except (zipfile.BadZipFile, KeyError, AttributeError):
            continue
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def read_epub(path):
    with zipfile.ZipFile(path) as zf:
        _, opf_path, opf = epub_title(zf)
        base = posixpath.dirname(opf_path)
        items = {}
        for tag in re.findall(r'<item\b[^>]*>', opf):
            i, h = re.search(r'id="([^"]+)"', tag), re.search(r'href="([^"]+)"', tag)
            if i and h:
                items[i.group(1)] = posixpath.normpath(posixpath.join(base, h.group(1)))
        docs = [zf.read(items[sid]).decode('utf-8', 'replace')
                for sid in re.findall(r'<itemref[^>]*idref="([^"]+)"', opf) if sid in items]
        names = [n for n in zf.namelist() if n.endswith(('.html', '.xhtml', '.htm'))]
        others = [zf.read(n).decode('utf-8', 'replace') for n in names]

    url = None
    for doc in others:
        m = re.search(r'downloaded by\s*<a[^>]*>calibre</a>\s*from\s*<a[^>]*href="(https?://[^"]+)"', doc, re.S | re.I) \
            or re.search(r'downloaded by.{0,200}?href="(https?://[^"]+)"', doc, re.S | re.I)
        if m:
            url = html.unescape(m.group(1))
            break
    if not url:
        for name, doc in zip(names, others):
            if 'reference' in name.lower():
                m = re.search(r'href="(https?://[^"]+)"', doc)
                if m:
                    url = html.unescape(m.group(1))
                    break

    paragraphs = []
    for doc in docs:
        body = doc[doc.find('<body'):] if '<body' in doc else doc
        body = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', body, flags=re.S)
        body = re.sub(BLOCK_TAGS, '\n', body)
        text = html.unescape(re.sub(r'<[^>]+>', '', body))
        paragraphs += [' '.join(line.split()) for line in text.split('\n') if line.strip()]
    return url, '\n'.join(paragraphs)


def restore_text(fulltext, exported):
    if not fulltext:
        return None
    ns_chars, ns_map = [], []
    for i, ch in enumerate(fulltext):
        if not ch.isspace():
            ns_chars.append(ch)
            ns_map.append(i)
    needle = ''.join(exported.split())
    pos = ''.join(ns_chars).find(needle)
    if not needle or pos < 0:
        return None
    segment = fulltext[ns_map[pos]:ns_map[pos + len(needle) - 1] + 1]
    return [line.strip() for line in segment.split('\n') if line.strip()]


def fix_spacing(text):
    text = ' '.join(text.split())
    text = re.sub(r'([%s])\s*([A-Za-z0-9])' % CJK, r'\1 \2', text)
    text = re.sub(r'([A-Za-z0-9%%])\s*([%s])' % CJK, r'\1 \2', text)
    text = re.sub(r'\s*([%s])\s*' % CJK_PUNCT, r'\1', text)
    return text


def rich_text(text, link=None):
    out = []
    for i in range(0, max(len(text), 1), 2000):
        part = {'type': 'text', 'text': {'content': text[i:i + 2000]}}
        if link:
            part['text']['link'] = {'url': link}
        out.append(part)
    return out


def block(kind, text, link=None):
    return {'object': 'block', 'type': kind, kind: {'rich_text': rich_text(text, link)}}


def build_blocks(book, url, fulltext):
    blocks = []
    if url:
        blocks += [block('heading_2', 'reference'), block('paragraph', url, link=url)]
    blocks.append(block('heading_2', '摘要'))
    restored = 0
    for n, item in enumerate(book['items']):
        if n:
            blocks.append(block('paragraph', '…'))
        lines = restore_text(fulltext, item['text']) if item['type'] == 'highlight' else None
        if lines:
            restored += 1
        if not lines and item['type'] == 'highlight':
            lines = [fix_spacing(item['text'])]
        for line in lines or [item['text']]:
            blocks.append(block('paragraph', line) if item['type'] == 'highlight' else block('quote', line))
        for note in item['notes']:
            blocks.append(block('quote', note))
    return blocks, restored


def blocks_to_markdown(blocks):
    lines = []
    for b in blocks:
        text = ''.join(r['text']['content'] for r in b[b['type']]['rich_text'])
        lines.append({'heading_2': '## ', 'quote': '> '}.get(b['type'], '') + text)
    return '\n'.join(lines)


def notion(token, method, path, payload=None):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    for attempt in range(5):
        req = urllib.request.Request(NOTION_API + path, data=data, method=method, headers={
            'Authorization': 'Bearer ' + token,
            'Notion-Version': NOTION_VERSION,
            'Content-Type': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8') or '{}')
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 4:
                time.sleep(float(exc.headers.get('Retry-After', '1')))
                continue
            detail = exc.read().decode('utf-8', 'replace')
            sys.exit('Notion API %s %s failed (%d): %s' % (method, path, exc.code, detail[:300]))


def write_notion(config, book, url, blocks):
    token = setting(config, 'notion_token')
    ds = setting(config, 'notion_data_source_id')
    props = {
        'Name': {'title': rich_text(book['title'])},
        'Type': {'select': {'name': 'Article'}},
        'Status': {'select': {'name': 'Finished'}},
    }
    if url:
        props['Link'] = {'url': url}

    found = notion(token, 'POST', '/data_sources/%s/query' % ds, {
        'filter': {'property': 'Name', 'title': {'equals': book['title']}}, 'page_size': 5})
    pages = found.get('results', [])

    if pages:
        page_id = pages[0]['id']
        notion(token, 'PATCH', '/pages/%s' % page_id, {'properties': props})
        child_ids, cursor = [], None
        while True:
            q = '?page_size=100' + ('&start_cursor=' + cursor if cursor else '')
            children = notion(token, 'GET', '/blocks/%s/children%s' % (page_id, q))
            child_ids += [child['id'] for child in children.get('results', [])]
            if not children.get('has_more'):
                break
            cursor = children.get('next_cursor')
        for child_id in child_ids:
            notion(token, 'DELETE', '/blocks/%s' % child_id)
        action = 'Updated'
    else:
        page = notion(token, 'POST', '/pages', {
            'parent': {'type': 'data_source_id', 'data_source_id': ds}, 'properties': props})
        page_id = page['id']
        action = 'Created'

    for i in range(0, len(blocks), 100):
        notion(token, 'PATCH', '/blocks/%s/children' % page_id, {'children': blocks[i:i + 100]})
    return action, page_id


def fetch_gmail(config, readonly):
    user, password = setting(config, 'mail_from'), setting(config, 'smtp_password')
    imap = imaplib.IMAP4_SSL('imap.gmail.com', 993)
    imap.login(user, password)
    imap.select('INBOX', readonly=readonly)
    _, data = imap.search(None, 'X-GM-RAW', '"%s"' % GMAIL_QUERY)
    messages = []
    for msg_id in data[0].split():
        _, raw = imap.fetch(msg_id, '(BODY.PEEK[])')
        msg = email.message_from_bytes(raw[0][1])
        for part in msg.walk():
            name = part.get_filename()
            if name and str(make_header(decode_header(name))).endswith('Notebook.html'):
                html_text = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', 'replace')
                try:
                    when = parsedate_to_datetime(msg['Date']).timestamp()
                except (TypeError, ValueError):
                    when = 0
                messages.append((msg_id, when, html_text))
                break
    return imap, messages


def mark_synced(imap, msg_ids):
    for msg_id in msg_ids:
        imap.store(msg_id, '+X-GM-LABELS', '("%s")' % SYNCED_LABEL)
        imap.store(msg_id, '+FLAGS', '\\Seen')


def main():
    parser = argparse.ArgumentParser(description='Sync Kindle notebook exports (Gmail) to Notion.')
    parser.add_argument('--file', help='process a local "<title> - Notebook.html" instead of Gmail')
    parser.add_argument('--dry-run', action='store_true',
                        help='print what would be written; do not touch Notion or Gmail')
    parser.add_argument('--out-dir', help='where the epubs live (default: ./output)')
    args = parser.parse_args()

    config = load_config()
    out_dir = Path(args.out_dir or config.get('out_dir') or HERE / 'output').expanduser()

    imap = None
    if args.file:
        groups = {None: [(None, 0, Path(args.file).read_text(encoding='utf-8'))]}
    else:
        imap, messages = fetch_gmail(config, readonly=args.dry_run)
        if not messages:
            print('No new Kindle notebook emails.')
            imap.logout()
            return
        groups = {}
        for m in messages:
            groups.setdefault(parse_notebook(m[2])['title'], []).append(m)

    try:
        for group in groups.values():
            group.sort(key=lambda m: m[1])
            book = parse_notebook(group[-1][2])
            epub = find_epub(out_dir, book['title'])
            url, fulltext = read_epub(epub) if epub else (None, '')
            blocks, restored = build_blocks(book, url, fulltext)
            highlights = sum(1 for i in book['items'] if i['type'] == 'highlight')
            notes = sum(len(i['notes']) + (i['type'] == 'note') for i in book['items'])

            print('Book   : %s' % book['title'])
            print('  Emails     : %d (using the latest)' % len(group) if group[0][0] else '  File       : %s' % args.file)
            print('  Epub       : %s' % (epub.name if epub else 'not found in %s' % out_dir))
            print('  Link       : %s' % (url or '-'))
            print('  Highlights : %d (%d restored from epub)  Notes: %d' % (highlights, restored, notes))

            if args.dry_run:
                print('----- preview -----')
                print(blocks_to_markdown(blocks))
                print('-------------------')
                continue

            action, page_id = write_notion(config, book, url, blocks)
            print('  %s Notion page %s' % (action, page_id))
            if imap is not None:
                mark_synced(imap, [m[0] for m in group])
                print('  Labeled %d email(s) as %s' % (len(group), SYNCED_LABEL))
    finally:
        if imap is not None:
            imap.logout()


if __name__ == '__main__':
    main()
