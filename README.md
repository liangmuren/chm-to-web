# CHM 转 Web

可复用的 CHM 到静态网页转换工具。

## 转换 CHM

```sh
python3 tools/chm_to_web.py /path/to/book.chm site --title "书籍标题"
```

转换器会在可用时优先使用 `7z` 或 `7zz`，解析 `.hhc` 目录文件，保留原始 HTML/资源文件，并生成以下内容：

- `index.html`
- `styles.css`
- `app.js`
- `data.js`
- `content/`
- `favicon.svg`

对于中文 CHM 文件，工具会使用 GB18030 解码 CHM 元数据，同时保持原始页面不变，以便浏览器继续按页面自身字符集正确渲染。

## 阅读器功能

生成的阅读器包含：

- 多级目录导航
- 全文搜索
- 页内搜索高亮
- 收藏功能
- 最近阅读
- 上次阅读页面恢复
- 基于浏览器本地存储的每页滚动位置恢复

## 预览输出

转换完成后，可以将生成的静态目录作为本地站点启动：

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

然后访问：

```text
http://127.0.0.1:8000/
```

生成页面、CHM 解压内容、截图、缓存以及源 `.chm` 文件已在 git 中被有意忽略。
