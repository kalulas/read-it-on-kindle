# read-it-on-kindle

用 calibre recipe 把网页文章处理为 Kindle 友好的 epub，并通过邮件推送到 Kindle；读完后把 Kindle 导出的笔记同步回 Notion。

- `wechat.recipe`：微信公众号文章
- `generic.recipe`：通用网页（自动识别常见正文容器）
- `zhihu.clean.json`：知乎文章（有反爬，先用浏览器插件存成 epub，再交给脚本清洗）

## 依赖

- Python 3.8+（仅标准库）
- [calibre](https://calibre-ebook.com/)，命令行工具需在 PATH 中（macOS 推荐 `brew install --cask calibre`）

推送前将 `config.example.json` 复制为 `config.local.json` 并填写邮箱与 Gmail 应用密码；发件邮箱需加入 Amazon 认可的发件人电子邮箱列表。

## 用法

```bash
python read_it_on_kindle.py <url>
python read_it_on_kindle.py <url> --title "自定义书名" --no-send
python read_it_on_kindle.py <url> --recipe generic
python read_it_on_kindle.py <xxx.epub> --clean zhihu

# 在 Kindle 手机 app 里把笔记本导出发到自己的 Gmail 后
python sync_kindle_notes_to_notion.py --dry-run
python sync_kindle_notes_to_notion.py
```

生成的 epub 位于当前目录 `output/` 下。

## License

[MIT](LICENSE)
