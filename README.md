# read-it-on-kindle

用 calibre recipe 把网页文章处理为 Kindle 友好的 epub，并通过邮件推送到 Kindle。

- `wechat.recipe`：微信公众号文章
- `generic.recipe`：通用网页（自动识别常见正文容器）

## 依赖

- Python 3.8+（仅标准库）
- [calibre](https://calibre-ebook.com/)，命令行工具需在 PATH 中（macOS 推荐 `brew install --cask calibre`）

推送前将 `config.example.json` 复制为 `config.local.json` 并填写邮箱与 Gmail 应用密码；发件邮箱需加入 Amazon 认可的发件人电子邮箱列表。

## 用法

```bash
python convert_article_to_epub.py <url> --recipe wechat
python convert_article_to_epub.py <url> --recipe generic --title "自定义书名"
python convert_article_to_epub.py <url> --recipe generic --no-send
```

生成的 epub 位于当前目录 `output/` 下。

## License

[MIT](LICENSE)
