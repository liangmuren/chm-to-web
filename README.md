# CHM 转 Web

可复用的 CHM 到静态网页转换工具。

## 转换 CHM

```sh
python3 tools/chm_to_web.py /path/to/book.chm site --title "书籍标题"
```

转换器会在可用时优先使用 `7z` 或 `7zz` 解压 CHM，也支持 `extract_chmLib` / `chmextract`。在 Windows 上，如果这些工具都不可用，会自动回退到系统自带的 `hh.exe -decompile`。

macOS / Linux 用户通常需要先安装一个 CHM 解包工具，例如：

```sh
brew install p7zip
```

也可以显式指定解包器：

```sh
python3 tools/chm_to_web.py /path/to/book.chm site --extractor /path/to/7zz
```

转换器会解析 `.hhc` 目录文件，保留原始 HTML/资源文件，并生成以下内容：

- `index.html`
- `styles.css`
- `app.js`
- `data.js`
- `search-data.js`
- `content/`
- `favicon.svg`

`data.js` 只包含首屏需要的目录和元数据，全文搜索索引会写入 `search-data.js`，并在用户首次使用搜索时按需加载。

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

如果已经为静态文件预生成 `.gz` 文件，也可以使用内置的 gzip 静态服务器，它会在浏览器支持时返回压缩资源：

```sh
python3 tools/gzip_static_server.py --port 8000 --bind 127.0.0.1 --directory site
```

然后访问：

```text
http://127.0.0.1:8000/
```

生成页面、CHM 解压内容、截图、缓存以及源 `.chm` 文件已在 git 中被有意忽略。
