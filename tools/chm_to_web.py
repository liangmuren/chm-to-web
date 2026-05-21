#!/usr/bin/env python3
"""Convert a CHM file into a self-contained static web reader."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


CHARSET_RE = re.compile(rb"charset\s*=\s*['\"]?([a-zA-Z0-9_\-]+)", re.I)
BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}


class SitemapParser(HTMLParser):
    """Parse the HTML Help .hhc sitemap format into a flat depth list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.current: dict[str, Any] | None = None
        self.items: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {name.lower(): value or "" for name, value in attrs}

        if tag == "ul":
            self.depth += 1
            return

        if tag == "object":
            object_type = attrs_dict.get("type", "").lower()
            if object_type == "text/sitemap":
                self.current = {"depth": self.depth, "params": {}}
            return

        if tag == "param" and self.current is not None:
            param_name = attrs_dict.get("name", "")
            if param_name:
                self.current["params"][param_name.lower()] = attrs_dict.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "object" and self.current is not None:
            self.items.append(self.current)
            self.current = None
            return

        if tag == "ul":
            self.depth = max(0, self.depth - 1)


class TextExtractor(HTMLParser):
    """Extract a readable title and body preview from old CHM HTML pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.in_title = False
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in BLOCK_TAGS and not self.skip_depth:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in BLOCK_TAGS and not self.skip_depth:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        elif not self.skip_depth:
            self.text_parts.append(data)

    @property
    def title(self) -> str:
        return normalize_space("".join(self.title_parts))

    @property
    def text(self) -> str:
        return normalize_space(" ".join(self.text_parts))


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def detect_encoding(data: bytes, fallback: str = "gb18030") -> str:
    head = data[:4096]
    match = CHARSET_RE.search(head)
    if not match:
        return fallback

    charset = match.group(1).decode("ascii", errors="ignore").lower()
    if charset in {"gb2312", "gbk", "gb18030"}:
        return "gb18030"
    return charset


def decode_bytes(data: bytes, fallback: str = "gb18030") -> str:
    encodings = [detect_encoding(data, fallback), "utf-8-sig", "utf-8", "gb18030", "big5"]
    seen: set[str] = set()
    for encoding in encodings:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode(fallback, errors="replace")


def find_extractor(explicit: str | None = None) -> str:
    if explicit:
        return explicit

    for name in ("7z", "7zz", "extract_chmLib", "chmextract"):
        found = shutil.which(name)
        if found:
            return found

    raise SystemExit("No CHM extractor found. Install 7z/7zz or pass --extractor.")


def extract_chm(source: Path, content_dir: Path, extractor: str) -> None:
    content_dir.mkdir(parents=True, exist_ok=True)

    tool_name = Path(extractor).name.lower()
    if tool_name in {"extract_chmlib", "chmextract"}:
        command = [extractor, str(source), str(content_dir)]
    else:
        command = [extractor, "x", "-y", f"-o{content_dir}", str(source)]

    subprocess.run(command, check=True)


def find_toc(content_dir: Path, explicit: Path | None = None) -> Path:
    if explicit:
        toc = explicit if explicit.is_absolute() else content_dir / explicit
        if not toc.exists():
            raise SystemExit(f"TOC file not found: {toc}")
        return toc

    candidates = sorted(content_dir.glob("*.hhc"), key=lambda item: item.stat().st_size, reverse=True)
    if not candidates:
        raise SystemExit(f"No .hhc table of contents found under {content_dir}")
    return candidates[0]


def split_fragment(local: str) -> tuple[str, str]:
    if "#" not in local:
        return local, ""
    path, fragment = local.split("#", 1)
    return path, fragment


def normalize_local(local: str) -> str:
    local = local.strip().replace("\\", "/")
    local = local.lstrip("/")
    return local


def file_exists_for_local(content_dir: Path, local: str) -> bool:
    path_part, _fragment = split_fragment(local)
    if not path_part or re.match(r"^[a-z]+:", path_part, re.I):
        return False
    return (content_dir / path_part).exists()


def build_tree(items: list[dict[str, Any]], content_dir: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    title_by_path: dict[str, str] = {}
    next_id = 1

    for item in items:
        params = item["params"]
        title = normalize_space(params.get("name", ""))
        local = normalize_local(params.get("local", ""))
        path = local if local and file_exists_for_local(content_dir, local) else ""

        if not title:
            title = Path(split_fragment(path or local)[0]).stem or "Untitled"

        node = {
            "id": f"n{next_id}",
            "title": title,
            "path": path,
            "children": [],
        }
        if local and local != path:
            node["_sourcePath"] = local
        next_id += 1

        if path and path not in title_by_path:
            title_by_path[path] = title

        depth = max(1, int(item["depth"]))
        while stack and stack[-1][0] >= depth:
            stack.pop()

        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((depth, node))

    return roots, title_by_path


def build_aliases(nodes: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}

    def visit(node: dict[str, Any]) -> None:
        source_path = node.get("_sourcePath", "")
        target = node.get("path") or first_page(node.get("children", []))
        if source_path and target:
            aliases[source_path] = target

        for child in node.get("children", []):
            visit(child)

    for root in nodes:
        visit(root)

    return aliases


def strip_private_tree_fields(nodes: list[dict[str, Any]]) -> None:
    for node in nodes:
        node.pop("_sourcePath", None)
        strip_private_tree_fields(node.get("children", []))


def parse_toc(toc: Path, content_dir: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    parser = SitemapParser()
    parser.feed(decode_bytes(toc.read_bytes(), fallback="gb18030"))
    return build_tree(parser.items, content_dir)


def parse_page(path: Path, max_chars: int) -> tuple[str, str, str]:
    html = decode_bytes(path.read_bytes())
    extractor = TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        pass

    title = extractor.title or path.stem
    text = extractor.text
    excerpt = text[:320]
    return title, text[:max_chars], excerpt


def build_search_index(content_dir: Path, title_by_path: dict[str, str], max_chars: int) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for path in sorted(content_dir.rglob("*.htm")):
        relative = path.relative_to(content_dir).as_posix()
        parsed_title, text, excerpt = parse_page(path, max_chars=max_chars)
        title = title_by_path.get(relative, parsed_title)
        entries.append(
            {
                "title": title,
                "path": relative,
                "text": text,
                "excerpt": excerpt,
            }
        )
    return entries


def first_page(nodes: list[dict[str, Any]]) -> str:
    for node in nodes:
        if node.get("path"):
            return str(node["path"])
        child_page = first_page(node.get("children", []))
        if child_page:
            return child_page
    return ""


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_site(
    output_dir: Path,
    title: str,
    source: Path,
    content_dir_name: str,
    tree: list[dict[str, Any]],
    search_index: list[dict[str, str]],
    aliases: dict[str, str],
    toc_path: Path,
) -> None:
    stats = {
        "source": str(source),
        "toc": toc_path.name,
        "pages": len(search_index),
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    data = {
        "title": title,
        "contentBase": content_dir_name,
        "firstPage": first_page(tree) or (search_index[0]["path"] if search_index else ""),
        "tree": tree,
        "search": search_index,
        "aliases": aliases,
        "stats": stats,
    }

    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    write_text(output_dir / "data.js", f"window.CHM_DATA = {data_json};\n")
    write_text(output_dir / "index.html", INDEX_HTML.format(title=html_escape(title)))
    write_text(output_dir / "styles.css", STYLES_CSS)
    write_text(output_dir / "app.js", APP_JS)
    write_text(output_dir / "favicon.svg", FAVICON_SVG)


def html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar" id="sidebar">
      <header class="brand">
        <div class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" role="img"><path d="M5 4.5h9.5A4.5 4.5 0 0 1 19 9v10.5H8.5A3.5 3.5 0 0 1 5 16V4.5Zm3 3V16c0 .28.22.5.5.5H16V9a1.5 1.5 0 0 0-1.5-1.5H8Z"/></svg>
        </div>
        <div>
          <h1 id="appTitle">{title}</h1>
          <p id="appMeta"></p>
        </div>
      </header>

      <div class="searchbar">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.8 4a6.8 6.8 0 1 1-4.81 11.61 6.8 6.8 0 0 1 4.81-11.6Zm0 2a4.8 4.8 0 1 0 0 9.6 4.8 4.8 0 0 0 0-9.6Zm4.95 8.33 4.46 4.46-1.42 1.42-4.46-4.46 1.42-1.42Z"/></svg>
        <input id="searchInput" type="search" autocomplete="off" spellcheck="false" placeholder="搜索">
      </div>

      <div class="side-tabs" role="tablist" aria-label="侧栏视图">
        <button class="side-tab is-active" type="button" role="tab" aria-controls="tree" aria-selected="true" data-panel="tree">目录</button>
        <button class="side-tab" type="button" role="tab" aria-controls="favoritesPanel" aria-selected="false" data-panel="favorites">收藏</button>
        <button class="side-tab" type="button" role="tab" aria-controls="recentPanel" aria-selected="false" data-panel="recent">最近</button>
      </div>

      <nav class="tree side-panel" id="tree" role="tabpanel" aria-label="目录"></nav>
      <section class="saved-panel side-panel" id="favoritesPanel" role="tabpanel" aria-label="收藏" hidden></section>
      <section class="saved-panel side-panel" id="recentPanel" role="tabpanel" aria-label="最近" hidden></section>
      <section class="results" id="results" hidden></section>
    </aside>

    <main class="reader">
      <header class="readerbar">
        <button class="icon-button nav-toggle" id="navToggle" type="button" aria-label="目录">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v2H4V6Zm0 5h16v2H4v-2Zm0 5h16v2H4v-2Z"/></svg>
        </button>
        <div class="crumbs" id="crumbs"></div>
        <div class="highlight-tools" id="highlightTools" hidden>
          <span id="highlightLabel"></span>
          <button class="highlight-button" id="highlightPrev" type="button" aria-label="上一个命中">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 7-6 6 1.4 1.4L12 9.8l4.6 4.6L18 13l-6-6Z"/></svg>
          </button>
          <button class="highlight-button" id="highlightNext" type="button" aria-label="下一个命中">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 17 6-6-1.4-1.4L12 14.2 7.4 9.6 6 11l6 6Z"/></svg>
          </button>
          <button class="highlight-button" id="highlightClear" type="button" aria-label="清除高亮">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6.4 5 5.6 5.6L17.6 5 19 6.4 13.4 12l5.6 5.6-1.4 1.4-5.6-5.6L6.4 19 5 17.6l5.6-5.6L5 6.4 6.4 5Z"/></svg>
          </button>
        </div>
        <button class="icon-button favorite-toggle" id="favoriteToggle" type="button" aria-label="收藏当前页面" title="收藏当前页面">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3.35 2.65 5.37 5.93.86-4.29 4.18 1.01 5.9L12 16.87l-5.3 2.79 1.01-5.9-4.29-4.18 5.93-.86L12 3.35Zm0 4.5-1.33 2.69-2.97.43 2.15 2.1-.51 2.95L12 14.63l2.66 1.4-.51-2.96 2.15-2.1-2.97-.43L12 7.85Z"/></svg>
        </button>
        <a class="open-page" id="openPage" href="#" target="_blank" rel="noopener">单页</a>
      </header>
      <iframe id="viewer" title="正文"></iframe>
    </main>
  </div>

  <script src="data.js"></script>
  <script src="app.js"></script>
</body>
</html>
"""


STYLES_CSS = """:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --panel-2: #eef3f1;
  --text: #172026;
  --muted: #68717a;
  --border: #d9dee5;
  --accent: #b43c3c;
  --accent-2: #1f6b5f;
  --focus: #2c5aa0;
  --shadow: 0 16px 44px rgba(26, 35, 44, 0.10);
}

* {
  box-sizing: border-box;
}

html,
body {
  height: 100%;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  letter-spacing: 0;
}

button,
input {
  font: inherit;
}

.app-shell {
  display: grid;
  grid-template-columns: minmax(290px, 360px) minmax(0, 1fr);
  height: 100vh;
  overflow: hidden;
}

.sidebar {
  display: flex;
  height: 100vh;
  min-width: 0;
  flex-direction: column;
  overflow: hidden;
  border-right: 1px solid var(--border);
  background: var(--panel);
  box-shadow: var(--shadow);
  z-index: 2;
}

.brand {
  display: grid;
  flex: 0 0 auto;
  grid-template-columns: 42px minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  padding: 20px 18px 16px;
  border-bottom: 1px solid var(--border);
}

.brand-mark {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 8px;
  color: #fff;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
}

.brand-mark svg {
  width: 24px;
  height: 24px;
  fill: currentColor;
}

.brand h1 {
  margin: 0;
  overflow: hidden;
  color: var(--text);
  font-size: 17px;
  font-weight: 760;
  line-height: 1.25;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.brand p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
}

.searchbar {
  display: grid;
  flex: 0 0 auto;
  grid-template-columns: 18px minmax(0, 1fr);
  gap: 9px;
  align-items: center;
  margin: 14px 14px 10px;
  padding: 10px 11px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fbfcfd;
}

.searchbar:focus-within {
  border-color: var(--focus);
  box-shadow: 0 0 0 3px rgba(44, 90, 160, 0.12);
}

.searchbar svg {
  width: 18px;
  height: 18px;
  fill: var(--muted);
}

.searchbar input {
  width: 100%;
  min-width: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text);
  font-size: 14px;
}

.side-tabs {
  display: grid;
  flex: 0 0 auto;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 4px;
  margin: 0 14px 10px;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #f3f5f7;
}

.side-tab {
  min-width: 0;
  height: 28px;
  padding: 0 6px;
  overflow: hidden;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.side-tab:hover {
  color: var(--text);
}

.side-tab.is-active {
  background: #fff;
  color: var(--text);
  box-shadow: 0 1px 4px rgba(26, 35, 44, 0.08);
}

.tree,
.results,
.saved-panel {
  flex: 1;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: scroll;
  padding: 4px 6px 20px 8px;
  scrollbar-color: #b9c1cc #edf0f3;
  scrollbar-gutter: stable;
  scrollbar-width: thin;
}

.tree::-webkit-scrollbar,
.results::-webkit-scrollbar,
.saved-panel::-webkit-scrollbar {
  width: 10px;
}

.tree::-webkit-scrollbar-track,
.results::-webkit-scrollbar-track,
.saved-panel::-webkit-scrollbar-track {
  background: #edf0f3;
  border-radius: 999px;
}

.tree::-webkit-scrollbar-thumb,
.results::-webkit-scrollbar-thumb,
.saved-panel::-webkit-scrollbar-thumb {
  min-height: 42px;
  border: 2px solid #edf0f3;
  border-radius: 999px;
  background: #b9c1cc;
}

.tree::-webkit-scrollbar-thumb:hover,
.results::-webkit-scrollbar-thumb:hover,
.saved-panel::-webkit-scrollbar-thumb:hover {
  background: #98a3b1;
}

.tree ul {
  margin: 0;
  padding: 0;
  list-style: none;
}

.tree li {
  --depth: 0;
  margin: 1px 0;
}

.tree-node {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  align-items: center;
  min-height: 30px;
  padding-left: calc(var(--depth) * 15px);
  border-radius: 8px;
}

.tree-node:hover {
  background: #f0f3f6;
}

.tree-node.is-active {
  background: #f7e8e8;
  color: #8f2626;
}

.tree-toggle,
.tree-title {
  border: 0;
  background: transparent;
  color: inherit;
}

.tree-toggle {
  display: grid;
  width: 22px;
  height: 30px;
  place-items: center;
  padding: 0;
  color: var(--muted);
  cursor: pointer;
}

.tree-toggle svg {
  width: 14px;
  height: 14px;
  fill: currentColor;
  transition: transform 150ms ease;
}

.tree li.is-collapsed > .tree-node .tree-toggle svg {
  transform: rotate(-90deg);
}

.tree-title {
  min-width: 0;
  padding: 6px 7px 6px 3px;
  overflow: hidden;
  cursor: pointer;
  font-size: 13px;
  line-height: 1.35;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tree-title.folder {
  font-weight: 650;
}

.tree li.is-collapsed > ul {
  display: none;
}

.tree .empty-toggle {
  width: 22px;
}

.results[hidden],
.side-panel[hidden] {
  display: none;
}

.saved-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  line-height: 1.2;
}

.saved-action {
  border: 0;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font-size: 12px;
  font-weight: 700;
}

.saved-action:disabled {
  cursor: default;
  opacity: 0.45;
}

.saved-item {
  display: grid;
  width: 100%;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  border-bottom: 1px solid var(--border);
  color: var(--text);
}

.saved-item:hover,
.saved-item:focus-within {
  background: #f0f3f6;
  outline: 0;
}

.saved-item.is-active {
  background: #f7e8e8;
  color: #8f2626;
}

.saved-open {
  min-width: 0;
  padding: 10px 8px 10px 10px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.saved-open:focus {
  outline: 0;
}

.saved-title {
  overflow: hidden;
  font-size: 13px;
  font-weight: 700;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.saved-meta {
  margin-top: 3px;
  overflow: hidden;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.saved-remove {
  display: grid;
  width: 26px;
  height: 26px;
  margin-right: 6px;
  place-items: center;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.saved-remove:hover {
  background: rgba(180, 60, 60, 0.10);
  color: var(--accent);
}

.saved-remove svg {
  width: 15px;
  height: 15px;
  fill: currentColor;
}

.result-button {
  display: block;
  width: 100%;
  padding: 10px 10px 11px;
  border: 0;
  border-bottom: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  cursor: pointer;
  text-align: left;
}

.result-summary,
.result-empty {
  padding: 8px 10px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.4;
}

.result-empty {
  margin: 8px;
  border: 1px dashed var(--border);
  border-radius: 8px;
  background: #fbfcfd;
}

.result-button:hover,
.result-button:focus {
  background: #f0f3f6;
  outline: 0;
}

.result-title {
  overflow: hidden;
  font-size: 13px;
  font-weight: 700;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result-text {
  display: -webkit-box;
  margin-top: 4px;
  overflow: hidden;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.reader {
  display: grid;
  min-width: 0;
  grid-template-rows: 48px minmax(0, 1fr);
  background: var(--panel-2);
}

.readerbar {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto auto;
  gap: 10px;
  align-items: center;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(10px);
}

.icon-button,
.open-page {
  display: inline-grid;
  height: 32px;
  align-items: center;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  color: var(--text);
}

.icon-button {
  width: 34px;
  place-items: center;
  padding: 0;
  cursor: pointer;
}

.icon-button svg {
  width: 18px;
  height: 18px;
  fill: currentColor;
}

.favorite-toggle.is-active {
  border-color: #e2c861;
  background: #fff8d7;
  color: #9a6a00;
}

.nav-toggle {
  display: none;
}

.open-page {
  padding: 0 12px;
  font-size: 13px;
  font-weight: 650;
  text-decoration: none;
}

.open-page:hover,
.icon-button:hover {
  border-color: #c4ccd6;
  background: #f9fafb;
}

.highlight-tools {
  display: inline-flex;
  max-width: min(420px, 42vw);
  height: 32px;
  align-items: center;
  gap: 4px;
  padding: 0 5px 0 10px;
  overflow: hidden;
  border: 1px solid #e2c861;
  border-radius: 8px;
  background: #fff8d7;
  color: #59420a;
}

.highlight-tools[hidden] {
  display: none;
}

#highlightLabel {
  min-width: 0;
  overflow: hidden;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.highlight-button {
  display: grid;
  width: 24px;
  height: 24px;
  flex: 0 0 auto;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.highlight-button:hover {
  background: rgba(89, 66, 10, 0.10);
}

.highlight-button:disabled {
  cursor: default;
  opacity: 0.45;
}

.highlight-button svg {
  width: 16px;
  height: 16px;
  fill: currentColor;
}

.result-mark {
  border-radius: 3px;
  background: #ffe58a;
  color: inherit;
  font-weight: 800;
}

.crumbs {
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

iframe {
  width: 100%;
  height: 100%;
  border: 0;
  background: #fff;
}

@media (max-width: 760px) {
  .app-shell {
    grid-template-columns: 1fr;
  }

  .sidebar {
    position: fixed;
    inset: 0 auto 0 0;
    width: min(88vw, 360px);
    transform: translateX(-105%);
    transition: transform 180ms ease;
  }

  body.nav-open .sidebar {
    transform: translateX(0);
  }

  .nav-toggle {
    display: inline-grid;
  }

  .reader {
    grid-column: 1;
    grid-template-rows: auto minmax(0, 1fr);
  }

  .readerbar {
    grid-template-columns: auto minmax(0, 1fr) auto auto;
  }

  .highlight-tools {
    width: 100%;
    max-width: none;
    grid-column: 1 / -1;
    grid-row: 2;
  }

  .favorite-toggle {
    grid-column: 3;
    grid-row: 1;
  }

  .open-page {
    grid-column: 4;
    grid-row: 1;
  }
}
"""


APP_JS = """(() => {
  const data = window.CHM_DATA;
  const treeEl = document.getElementById("tree");
  const resultsEl = document.getElementById("results");
  const searchInput = document.getElementById("searchInput");
  const viewer = document.getElementById("viewer");
  const openPage = document.getElementById("openPage");
  const crumbs = document.getElementById("crumbs");
  const appMeta = document.getElementById("appMeta");
  const navToggle = document.getElementById("navToggle");
  const sideTabs = [...document.querySelectorAll(".side-tab")];
  const favoritesPanel = document.getElementById("favoritesPanel");
  const recentPanel = document.getElementById("recentPanel");
  const favoriteToggle = document.getElementById("favoriteToggle");
  const highlightTools = document.getElementById("highlightTools");
  const highlightLabel = document.getElementById("highlightLabel");
  const highlightPrev = document.getElementById("highlightPrev");
  const highlightNext = document.getElementById("highlightNext");
  const highlightClear = document.getElementById("highlightClear");

  const nodeByPath = new Map();
  const liByPath = new Map();
  const parentById = new Map();
  const aliasByPath = new Map(Object.entries(data.aliases || {}));
  let activePath = "";
  let activeHighlightQuery = "";
  let highlightMatches = [];
  let highlightIndex = -1;
  let activeSidePanel = "tree";
  let readerState = loadReaderState();
  let restoreScrollAfterLoad = false;
  let iframeScrollTimer = 0;

  appMeta.textContent = `${data.stats.pages} 页`;

  function encodeContentPath(path) {
    const index = path.indexOf("#");
    const file = index === -1 ? path : path.slice(0, index);
    const fragment = index === -1 ? "" : path.slice(index + 1);
    const encodedFile = file.split("/").map(encodeURIComponent).join("/");
    return fragment ? `${encodedFile}#${encodeURIComponent(fragment)}` : encodedFile;
  }

  function contentUrl(path) {
    return `${data.contentBase}/${encodeContentPath(path)}`;
  }

  function contentPathFromUrl(url) {
    const base = new URL(`${data.contentBase}/`, window.location.href);
    if (!url.href.startsWith(base.href)) return "";
    const relative = decodeURIComponent(url.href.slice(base.href.length));
    const hashIndex = relative.indexOf("#");
    const file = hashIndex === -1 ? relative : relative.slice(0, hashIndex);
    const fragment = hashIndex === -1 ? "" : relative.slice(hashIndex + 1);
    const normalizedFile = file.replace(/^\\.\\//, "");
    return fragment ? `${normalizedFile}#${fragment}` : normalizedFile;
  }

  function resolveContentPath(path) {
    if (!path) return "";
    if (nodeByPath.has(path)) return path;

    const hashIndex = path.indexOf("#");
    const file = hashIndex === -1 ? path : path.slice(0, hashIndex);
    const fragment = hashIndex === -1 ? "" : path.slice(hashIndex + 1);
    const alias = aliasByPath.get(path) || aliasByPath.get(file);
    if (!alias) return "";
    return fragment && !alias.includes("#") ? `${alias}#${fragment}` : alias;
  }

  function storageKey() {
    const raw = `${data.title}|${data.stats?.toc || ""}|${data.stats?.pages || 0}`;
    return `chm-to-web:${raw}`;
  }

  function loadReaderState() {
    const fallback = { favorites: [], recent: [], scroll: {}, lastPath: "" };
    try {
      const parsed = JSON.parse(localStorage.getItem(storageKey()) || "null");
      if (!parsed || typeof parsed !== "object") return fallback;
      return {
        favorites: Array.isArray(parsed.favorites) ? parsed.favorites : [],
        recent: Array.isArray(parsed.recent) ? parsed.recent : [],
        scroll: parsed.scroll && typeof parsed.scroll === "object" ? parsed.scroll : {},
        lastPath: typeof parsed.lastPath === "string" ? parsed.lastPath : "",
      };
    } catch (_error) {
      return fallback;
    }
  }

  function saveReaderState() {
    try {
      localStorage.setItem(storageKey(), JSON.stringify(readerState));
    } catch (_error) {
      // Saved reader state is optional.
    }
  }

  function pageTitle(path) {
    return nodeByPath.get(path)?.title || data.search.find((entry) => entry.path === path)?.title || path;
  }

  function pageCrumb(path) {
    const node = nodeByPath.get(path);
    if (!node) return "";
    return ancestry(node).map((item) => item.title).join(" / ");
  }

  function normalizeSavedList(list, limit) {
    const seen = new Set();
    const normalized = [];
    list.forEach((item) => {
      const path = typeof item === "string" ? item : item?.path;
      const resolved = resolveContentPath(path || "");
      if (!resolved || seen.has(resolved)) return;
      seen.add(resolved);
      normalized.push({
        path: resolved,
        title: item?.title || pageTitle(resolved),
        time: Number(item?.time) || Date.now(),
      });
    });
    return normalized.slice(0, limit);
  }

  function sanitizeReaderState() {
    readerState.favorites = normalizeSavedList(readerState.favorites, 200);
    readerState.recent = normalizeSavedList(readerState.recent, 50);
    readerState.lastPath = resolveContentPath(readerState.lastPath) || "";
    Object.keys(readerState.scroll).forEach((path) => {
      if (!resolveContentPath(path)) delete readerState.scroll[path];
    });
    saveReaderState();
  }

  function searchTerms(query) {
    const unique = new Set();
    query
      .trim()
      .split(/\\s+/)
      .map((term) => term.trim())
      .filter(Boolean)
      .forEach((term) => unique.add(term));
    return [...unique].sort((a, b) => b.length - a.length).slice(0, 8);
  }

  function appendHighlightedText(parent, text, terms) {
    if (!terms.length) {
      parent.textContent = text;
      return;
    }

    const lower = text.toLocaleLowerCase();
    const loweredTerms = terms.map((term) => term.toLocaleLowerCase());
    let offset = 0;

    while (offset < text.length) {
      let nextIndex = -1;
      let nextTerm = "";

      loweredTerms.forEach((term, index) => {
        const found = lower.indexOf(term, offset);
        if (found !== -1 && (nextIndex === -1 || found < nextIndex)) {
          nextIndex = found;
          nextTerm = terms[index];
        }
      });

      if (nextIndex === -1) {
        parent.appendChild(document.createTextNode(text.slice(offset)));
        break;
      }

      if (nextIndex > offset) {
        parent.appendChild(document.createTextNode(text.slice(offset, nextIndex)));
      }

      const mark = document.createElement("mark");
      mark.className = "result-mark";
      mark.textContent = text.slice(nextIndex, nextIndex + nextTerm.length);
      parent.appendChild(mark);
      offset = nextIndex + nextTerm.length;
    }
  }

  function flatten(nodes, parent = null) {
    nodes.forEach((node) => {
      if (parent) parentById.set(node.id, parent);
      if (node.path) nodeByPath.set(node.path, node);
      flatten(node.children || [], node);
    });
  }

  function setCollapsed(li, collapsed) {
    li.classList.toggle("is-collapsed", collapsed);
    const toggle = li.querySelector(":scope > .tree-node .tree-toggle");
    if (toggle) {
      toggle.setAttribute("aria-expanded", String(!collapsed));
      toggle.setAttribute("aria-label", collapsed ? "展开" : "折叠");
    }
  }

  function showTreePanel() {
    showSidePanel("tree", true);
  }

  function showSidePanel(panel, clearSearch = true) {
    activeSidePanel = panel;
    if (clearSearch) searchInput.value = "";

    const showingSearch = searchInput.value.trim().length > 0;
    resultsEl.hidden = !showingSearch;
    treeEl.hidden = showingSearch || panel !== "tree";
    favoritesPanel.hidden = showingSearch || panel !== "favorites";
    recentPanel.hidden = showingSearch || panel !== "recent";

    sideTabs.forEach((tab) => {
      const active = tab.dataset.panel === panel;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
    });

    if (panel === "favorites") renderFavorites();
    if (panel === "recent") renderRecent();
    if (panel === "tree" && activePath) updateActive(activePath);
  }

  function renderSavedPanel(panel, heading, items, options = {}) {
    panel.replaceChildren();

    const header = document.createElement("div");
    header.className = "saved-heading";
    const label = document.createElement("span");
    label.textContent = heading;
    header.appendChild(label);

    if (options.clearAction) {
      const clear = document.createElement("button");
      clear.className = "saved-action";
      clear.type = "button";
      clear.textContent = "清空";
      clear.disabled = !items.length;
      clear.addEventListener("click", options.clearAction);
      header.appendChild(clear);
    }
    panel.appendChild(header);

    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "result-empty";
      empty.textContent = options.emptyText || "还没有内容。";
      panel.appendChild(empty);
      return;
    }

    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "saved-item";
      row.classList.toggle("is-active", item.path === activePath);

      const open = document.createElement("button");
      open.className = "saved-open";
      open.type = "button";
      open.addEventListener("click", () => {
        loadPage(item.path, true);
        document.body.classList.remove("nav-open");
      });

      const text = document.createElement("div");
      const title = document.createElement("div");
      title.className = "saved-title";
      title.textContent = pageTitle(item.path);
      const meta = document.createElement("div");
      meta.className = "saved-meta";
      meta.textContent = options.metaText ? options.metaText(item) : pageCrumb(item.path);
      text.append(title, meta);
      open.appendChild(text);
      row.appendChild(open);

      if (options.removeAction) {
        const remove = document.createElement("button");
        remove.className = "saved-remove";
        remove.type = "button";
        remove.setAttribute("aria-label", `移除 ${pageTitle(item.path)}`);
        remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6.4 5 5.6 5.6L17.6 5 19 6.4 13.4 12l5.6 5.6-1.4 1.4-5.6-5.6L6.4 19 5 17.6l5.6-5.6L5 6.4 6.4 5Z"/></svg>';
        remove.addEventListener("click", () => options.removeAction(item.path));
        row.appendChild(remove);
      }

      panel.appendChild(row);
    });
  }

  function renderFavorites() {
    renderSavedPanel(favoritesPanel, `收藏 · ${readerState.favorites.length}`, readerState.favorites, {
      emptyText: "点击顶部星标收藏当前页面。",
      removeAction(path) {
        readerState.favorites = readerState.favorites.filter((item) => item.path !== path);
        saveReaderState();
        renderFavorites();
        updateFavoriteButton();
      },
    });
  }

  function renderRecent() {
    renderSavedPanel(recentPanel, `最近 · ${readerState.recent.length}`, readerState.recent, {
      emptyText: "打开页面后会自动记录最近阅读。",
      clearAction() {
        readerState.recent = [];
        saveReaderState();
        renderRecent();
      },
      metaText(item) {
        return `${formatRelativeTime(item.time)} · ${pageCrumb(item.path)}`;
      },
    });
  }

  function renderSavedPanels() {
    if (!favoritesPanel.hidden) renderFavorites();
    if (!recentPanel.hidden) renderRecent();
  }

  function formatRelativeTime(time) {
    const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
    if (seconds < 60) return "刚刚";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days} 天前`;
    return new Date(time).toLocaleDateString();
  }

  function updateFavoriteButton() {
    const active = readerState.favorites.some((item) => item.path === activePath);
    favoriteToggle.classList.toggle("is-active", active);
    favoriteToggle.setAttribute("aria-label", active ? "取消收藏当前页面" : "收藏当前页面");
    favoriteToggle.title = active ? "取消收藏当前页面" : "收藏当前页面";
  }

  function toggleFavorite() {
    if (!activePath) return;
    const exists = readerState.favorites.some((item) => item.path === activePath);
    if (exists) {
      readerState.favorites = readerState.favorites.filter((item) => item.path !== activePath);
    } else {
      readerState.favorites.unshift({ path: activePath, title: pageTitle(activePath), time: Date.now() });
    }
    readerState.favorites = normalizeSavedList(readerState.favorites, 200);
    saveReaderState();
    updateFavoriteButton();
    renderSavedPanels();
  }

  function recordRecent(path) {
    if (!path) return;
    readerState.lastPath = path;
    readerState.recent = [
      { path, title: pageTitle(path), time: Date.now() },
      ...readerState.recent.filter((item) => item.path !== path),
    ].slice(0, 50);
    saveReaderState();
    renderSavedPanels();
  }

  function getIframeScrollTop() {
    try {
      const doc = viewer.contentDocument;
      const win = viewer.contentWindow;
      const positions = [
        win?.scrollY,
        win?.pageYOffset,
        doc?.scrollingElement?.scrollTop,
        doc?.documentElement?.scrollTop,
        doc?.body?.scrollTop,
      ];
      return Math.max(0, Math.round(Math.max(...positions.map((value) => Number(value) || 0))));
    } catch (_error) {
      return 0;
    }
  }

  function saveCurrentScroll() {
    if (!activePath) return;
    const top = getIframeScrollTop();
    if (top > 0) readerState.scroll[activePath] = top;
    else delete readerState.scroll[activePath];
    saveReaderState();
  }

  function restoreCurrentScroll() {
    if (!restoreScrollAfterLoad || activeHighlightQuery) return;
    const top = Number(readerState.scroll[activePath] || 0);
    if (!top) return;

    try {
      const doc = viewer.contentDocument;
      const win = viewer.contentWindow;
      const scrollingElement = doc?.scrollingElement || doc?.documentElement || doc?.body;
      requestAnimationFrame(() => {
        win?.scrollTo?.(0, top);
        if (scrollingElement) scrollingElement.scrollTop = top;
        if (doc?.documentElement) doc.documentElement.scrollTop = top;
        if (doc?.body) doc.body.scrollTop = top;
      });
    } catch (_error) {
      // Restoring scroll is enhancement-only.
    }
  }

  function wireIframeScrollTracking() {
    try {
      const doc = viewer.contentDocument;
      const win = viewer.contentWindow;
      if (!doc || doc.__chmReaderScrollWired) return;
      doc.__chmReaderScrollWired = true;
      const saveSoon = () => {
        clearTimeout(iframeScrollTimer);
        iframeScrollTimer = window.setTimeout(saveCurrentScroll, 160);
      };
      doc.addEventListener("scroll", saveSoon, true);
      win?.addEventListener?.("scroll", saveSoon, { passive: true });
    } catch (_error) {
      // Saved progress is enhancement-only.
    }
  }

  function scrollTreeItemIntoView(li) {
    if (treeEl.hidden) return;
    const row = li.querySelector(".tree-node");
    if (!row) return;

    const treeRect = treeEl.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    const margin = 12;

    if (rowRect.top < treeRect.top + margin) {
      treeEl.scrollTop -= treeRect.top + margin - rowRect.top;
    } else if (rowRect.bottom > treeRect.bottom - margin) {
      treeEl.scrollTop += rowRect.bottom - (treeRect.bottom - margin);
    }
  }

  function renderTree(nodes, depth = 0) {
    const ul = document.createElement("ul");
    nodes.forEach((node) => {
      const li = document.createElement("li");
      li.dataset.id = node.id;
      li.style.setProperty("--depth", depth);

      const row = document.createElement("div");
      row.className = "tree-node";

      if (node.children && node.children.length) {
        const toggle = document.createElement("button");
        toggle.className = "tree-toggle";
        toggle.type = "button";
        toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7.4 8.6 4.6 4.6 4.6-4.6L18 10l-6 6-6-6 1.4-1.4Z"/></svg>';
        toggle.addEventListener("click", (event) => {
          event.stopPropagation();
          setCollapsed(li, !li.classList.contains("is-collapsed"));
        });
        row.appendChild(toggle);
      } else {
        const spacer = document.createElement("span");
        spacer.className = "empty-toggle";
        row.appendChild(spacer);
      }

      const title = document.createElement("button");
      title.className = `tree-title${node.path ? "" : " folder"}`;
      title.type = "button";
      title.textContent = node.title;
      title.title = node.title;
      title.addEventListener("click", () => {
        if (node.path) {
          loadPage(node.path, true);
          document.body.classList.remove("nav-open");
        } else if (node.children && node.children.length) {
          setCollapsed(li, !li.classList.contains("is-collapsed"));
        }
      });
      row.appendChild(title);

      li.appendChild(row);
      if (node.path) liByPath.set(node.path, li);
      if (node.children && node.children.length) li.appendChild(renderTree(node.children, depth + 1));
      if (depth > 0 && node.children && node.children.length) setCollapsed(li, true);
      ul.appendChild(li);
    });
    return ul;
  }

  function ancestry(node) {
    const trail = [];
    let current = node;
    while (current) {
      trail.unshift(current);
      current = parentById.get(current.id);
    }
    return trail;
  }

  function updateActive(path) {
    document.querySelectorAll(".tree-node.is-active").forEach((el) => el.classList.remove("is-active"));
    const li = liByPath.get(path);
    const node = nodeByPath.get(path);

    if (li) {
      li.querySelector(".tree-node")?.classList.add("is-active");
      let parent = li.parentElement?.closest("li");
      while (parent) {
        setCollapsed(parent, false);
        parent = parent.parentElement?.closest("li");
      }
      scrollTreeItemIntoView(li);
    }

    crumbs.textContent = node ? ancestry(node).map((item) => item.title).join(" / ") : path;
    updateFavoriteButton();
    renderSavedPanels();
  }

  function updateHash(path, pushHash) {
    const nextHash = `#${encodeURIComponent(path)}`;
    if (window.location.hash === nextHash) return;
    if (pushHash) {
      history.pushState({ path }, "", nextHash);
    } else {
      history.replaceState({ path }, "", nextHash);
    }
  }

  function loadPage(path, pushHash, highlightQuery = "") {
    saveCurrentScroll();
    activePath = path;
    activeHighlightQuery = highlightQuery;
    highlightMatches = [];
    highlightIndex = -1;
    restoreScrollAfterLoad = !highlightQuery;
    viewer.src = contentUrl(path);
    openPage.href = contentUrl(path);
    updateActive(path);
    recordRecent(path);
    updateHighlightTools();
    updateHash(path, pushHash);
  }

  function wireIframeLinks() {
    try {
      const doc = viewer.contentDocument;
      if (!doc || doc.__chmReaderLinksWired) return;
      doc.__chmReaderLinksWired = true;

      doc.addEventListener("click", (event) => {
        const anchor = event.target.closest?.("a[href]");
        if (!anchor) return;

        const href = anchor.getAttribute("href");
        if (!href || href.startsWith("#")) return;

        const url = new URL(href, viewer.contentWindow.location.href);
        const internalPath = contentPathFromUrl(url);
        if (internalPath) {
          event.preventDefault();
          const targetPath = resolveContentPath(internalPath);
          if (targetPath) loadPage(targetPath, true);
          return;
        }

        if (url.protocol === "http:" || url.protocol === "https:") {
          event.preventDefault();
          window.open(url.href, "_blank", "noopener");
        }
      });
    } catch (_error) {
      // Some browsers restrict file iframe access; direct iframe navigation still works.
    }
  }

  function ensureIframeHighlightStyle(doc) {
    if (doc.getElementById("chm-reader-highlight-style")) return;
    const style = doc.createElement("style");
    style.id = "chm-reader-highlight-style";
    style.textContent = `
      mark.chm-search-hit {
        border-radius: 2px;
        padding: 0 1px;
        background: #ffe16a;
        color: inherit;
      }
      mark.chm-search-hit.is-current {
        background: #ff9f43;
        box-shadow: 0 0 0 2px rgba(255, 159, 67, 0.35);
      }
    `;
    (doc.head || doc.documentElement).appendChild(style);
  }

  function clearIframeHighlights() {
    try {
      const doc = viewer.contentDocument;
      if (!doc) return;
      [...doc.querySelectorAll("mark.chm-search-hit")].forEach((mark) => {
        mark.replaceWith(doc.createTextNode(mark.textContent || ""));
      });
      doc.body?.normalize();
    } catch (_error) {
      // Highlighting is enhancement-only.
    }
    highlightMatches = [];
    highlightIndex = -1;
  }

  function highlightTextNode(doc, node, terms, created, maxHits) {
    const text = node.nodeValue || "";
    const lower = text.toLocaleLowerCase();
    const loweredTerms = terms.map((term) => term.toLocaleLowerCase());
    const fragment = doc.createDocumentFragment();
    let offset = 0;
    let changed = false;

    while (offset < text.length) {
      if (created >= maxHits) {
        fragment.appendChild(doc.createTextNode(text.slice(offset)));
        break;
      }

      let nextIndex = -1;
      let nextTerm = "";

      loweredTerms.forEach((term, index) => {
        const found = lower.indexOf(term, offset);
        if (found !== -1 && (nextIndex === -1 || found < nextIndex)) {
          nextIndex = found;
          nextTerm = terms[index];
        }
      });

      if (nextIndex === -1) {
        fragment.appendChild(doc.createTextNode(text.slice(offset)));
        break;
      }

      changed = true;
      if (nextIndex > offset) {
        fragment.appendChild(doc.createTextNode(text.slice(offset, nextIndex)));
      }

      const mark = doc.createElement("mark");
      mark.className = "chm-search-hit";
      mark.textContent = text.slice(nextIndex, nextIndex + nextTerm.length);
      fragment.appendChild(mark);
      created += 1;
      offset = nextIndex + nextTerm.length;
    }

    if (changed) node.parentNode.replaceChild(fragment, node);
    return created;
  }

  function applyIframeHighlights() {
    clearIframeHighlights();
    const terms = searchTerms(activeHighlightQuery);
    if (!terms.length) {
      updateHighlightTools();
      return;
    }

    try {
      const doc = viewer.contentDocument;
      const win = viewer.contentWindow;
      if (!doc?.body || !win?.NodeFilter) {
        updateHighlightTools();
        return;
      }

      ensureIframeHighlightStyle(doc);
      const nodes = [];
      const loweredTerms = terms.map((term) => term.toLocaleLowerCase());
      const walker = doc.createTreeWalker(doc.body, win.NodeFilter.SHOW_TEXT, {
        acceptNode(node) {
          const parent = node.parentElement;
          if (!parent || parent.closest("script, style, noscript, mark")) {
            return win.NodeFilter.FILTER_REJECT;
          }
          const text = node.nodeValue || "";
          if (!text.trim()) return win.NodeFilter.FILTER_REJECT;
          const lower = text.toLocaleLowerCase();
          return loweredTerms.some((term) => lower.includes(term))
            ? win.NodeFilter.FILTER_ACCEPT
            : win.NodeFilter.FILTER_REJECT;
        },
      });

      let current = walker.nextNode();
      while (current && nodes.length < 2000) {
        nodes.push(current);
        current = walker.nextNode();
      }

      let created = 0;
      const maxHits = 500;
      nodes.forEach((node) => {
        if (created < maxHits) created = highlightTextNode(doc, node, terms, created, maxHits);
      });

      highlightMatches = [...doc.querySelectorAll("mark.chm-search-hit")];
      setCurrentHighlight(0, true);
    } catch (_error) {
      updateHighlightTools();
    }
  }

  function setCurrentHighlight(nextIndex, shouldScroll) {
    highlightMatches.forEach((mark) => mark.classList.remove("is-current"));
    if (!highlightMatches.length) {
      highlightIndex = -1;
      updateHighlightTools();
      return;
    }

    highlightIndex = (nextIndex + highlightMatches.length) % highlightMatches.length;
    const mark = highlightMatches[highlightIndex];
    mark.classList.add("is-current");
    if (shouldScroll) mark.scrollIntoView({ block: "center", inline: "nearest" });
    updateHighlightTools();
  }

  function updateHighlightTools() {
    const terms = searchTerms(activeHighlightQuery);
    const hasQuery = terms.length > 0;
    highlightTools.hidden = !hasQuery;
    if (!hasQuery) return;

    const capped = highlightMatches.length >= 500 ? "+" : "";
    const countText = highlightMatches.length
      ? `${highlightIndex + 1}/${highlightMatches.length}${capped}`
      : "0";
    highlightLabel.textContent = `${terms.join(" ")} · ${countText}`;
    const disabled = highlightMatches.length < 2;
    highlightPrev.disabled = disabled;
    highlightNext.disabled = disabled;
  }

  function clearActiveHighlight() {
    activeHighlightQuery = "";
    clearIframeHighlights();
    updateHighlightTools();
  }

  function renderResults(matches, query) {
    resultsEl.replaceChildren();
    const fragment = document.createDocumentFragment();
    const summary = document.createElement("div");
    summary.className = "result-summary";
    summary.textContent = matches.length ? `${matches.length} 个结果` : "没有找到匹配结果";
    fragment.appendChild(summary);

    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "result-empty";
      empty.textContent = "换个关键词试试。支持标题和正文内容搜索。";
      fragment.appendChild(empty);
      resultsEl.appendChild(fragment);
      return;
    }

    matches.slice(0, 80).forEach((entry) => {
      const button = document.createElement("button");
      button.className = "result-button";
      button.type = "button";
      const title = document.createElement("div");
      title.className = "result-title";
      appendHighlightedText(title, entry.title, searchTerms(query));
      const text = document.createElement("div");
      text.className = "result-text";
      appendHighlightedText(text, makeSnippet(entry, query), searchTerms(query));
      button.append(title, text);
      button.addEventListener("click", () => {
        showTreePanel();
        loadPage(entry.path, true, query);
        document.body.classList.remove("nav-open");
      });
      fragment.appendChild(button);
    });
    resultsEl.appendChild(fragment);
  }

  function makeSnippet(entry, query) {
    const text = entry.text || entry.excerpt || "";
    const lower = text.toLocaleLowerCase();
    const term = query.toLocaleLowerCase().split(/\\s+/).find(Boolean) || "";
    const index = term ? lower.indexOf(term) : -1;
    if (index === -1) return entry.excerpt || text.slice(0, 180);
    return text.slice(Math.max(0, index - 45), index + 150);
  }

  function scoreEntry(entry, terms, query) {
    const title = entry.title.toLocaleLowerCase();
    const text = entry.text.toLocaleLowerCase();
    let score = 0;

    if (title === query) score -= 1000;
    else if (title.startsWith(query)) score -= 600;
    else if (title.includes(query)) score -= 350;

    terms.forEach((term) => {
      if (title === term) score -= 220;
      else if (title.startsWith(term)) score -= 160;
      else if (title.includes(term)) score -= 90;
      else if (text.includes(term)) score += 30;
    });

    score += Math.min(text.length / 1000, 40);
    return score;
  }

  function runSearch() {
    const query = searchInput.value.trim();
    if (!query) {
      showSidePanel(activeSidePanel, false);
      return;
    }

    const terms = query.toLocaleLowerCase().split(/\\s+/).filter(Boolean);
    const queryLower = query.toLocaleLowerCase();
    const matches = data.search
      .filter((entry) => {
        const haystack = `${entry.title} ${entry.text}`.toLocaleLowerCase();
        return terms.every((term) => haystack.includes(term));
      })
      .map((entry, index) => ({ entry, index, score: scoreEntry(entry, terms, queryLower) }))
      .sort((a, b) => a.score - b.score || a.index - b.index)
      .map((item) => item.entry);

    treeEl.hidden = true;
    favoritesPanel.hidden = true;
    recentPanel.hidden = true;
    resultsEl.hidden = false;
    renderResults(matches, query);
  }

  flatten(data.tree);
  sanitizeReaderState();
  treeEl.appendChild(renderTree(data.tree));
  showSidePanel("tree", false);

  sideTabs.forEach((tab) => {
    tab.addEventListener("click", () => showSidePanel(tab.dataset.panel || "tree", true));
  });
  searchInput.addEventListener("input", runSearch);
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && searchInput.value) {
      event.preventDefault();
      showTreePanel();
      updateActive(activePath);
    }
  });
  navToggle.addEventListener("click", () => document.body.classList.toggle("nav-open"));
  favoriteToggle.addEventListener("click", toggleFavorite);
  window.addEventListener("beforeunload", saveCurrentScroll);

  viewer.addEventListener("load", () => {
    try {
      const url = new URL(viewer.contentWindow.location.href);
      const decoded = contentPathFromUrl(url);
      if (decoded) {
        const targetPath = resolveContentPath(decoded);
        if (targetPath && targetPath !== activePath) {
          loadPage(targetPath, true);
          return;
        } else if (targetPath) {
          updateActive(targetPath);
          updateHash(targetPath, false);
        }
      }
      wireIframeLinks();
      applyIframeHighlights();
      restoreCurrentScroll();
      wireIframeScrollTracking();
    } catch (_error) {
      // Some browsers restrict file iframe access; the reader still works.
    }
  });

  highlightPrev.addEventListener("click", () => setCurrentHighlight(highlightIndex - 1, true));
  highlightNext.addEventListener("click", () => setCurrentHighlight(highlightIndex + 1, true));
  highlightClear.addEventListener("click", clearActiveHighlight);
  window.addEventListener("popstate", () => {
    const hashPath = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : "";
    const targetPath = resolveContentPath(hashPath) || data.firstPage;
    if (targetPath) loadPage(targetPath, false);
  });

  const hashPath = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : "";
  const startPath = resolveContentPath(hashPath) || (!hashPath ? readerState.lastPath : "") || data.firstPage;
  if (startPath) loadPage(startPath, false);
})();
"""


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="g" x1="12" y1="10" x2="54" y2="54" gradientUnits="userSpaceOnUse">
      <stop stop-color="#b43c3c"/>
      <stop offset="1" stop-color="#1f6b5f"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="12" fill="url(#g)"/>
  <path fill="#fff" d="M20 14h25a7 7 0 0 1 7 7v29H27a7 7 0 0 1-7-7V14Zm9 10v18h13V24H29Zm4 4h5v10h-5V28Z"/>
</svg>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a CHM file into a static web reader.")
    parser.add_argument("source", type=Path, help="Path to the .chm file")
    parser.add_argument("output", type=Path, nargs="?", default=Path("site"), help="Output directory")
    parser.add_argument("--title", help="Reader title. Defaults to the CHM filename.")
    parser.add_argument("--toc", type=Path, help="Specific .hhc file inside the extracted CHM")
    parser.add_argument("--extractor", help="Path to 7z/7zz/extract_chmLib/chmextract")
    parser.add_argument("--content-dir", default="content", help="Content folder name inside the output")
    parser.add_argument("--search-chars", type=int, default=6000, help="Max body characters indexed per page")
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="Reuse an existing output content directory instead of extracting the CHM again",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    source = args.source.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    content_dir = output_dir / args.content_dir

    if not source.exists():
        raise SystemExit(f"CHM file not found: {source}")

    if not args.skip_extract:
        extractor = find_extractor(args.extractor)
        extract_chm(source, content_dir, extractor)
    elif not content_dir.exists():
        raise SystemExit(f"Content directory does not exist: {content_dir}")

    toc_path = find_toc(content_dir, args.toc)
    tree, title_by_path = parse_toc(toc_path, content_dir)
    aliases = build_aliases(tree)
    strip_private_tree_fields(tree)
    search_index = build_search_index(content_dir, title_by_path, max_chars=max(200, args.search_chars))
    title = args.title or source.stem

    write_site(
        output_dir=output_dir,
        title=title,
        source=source,
        content_dir_name=args.content_dir,
        tree=tree,
        search_index=search_index,
        aliases=aliases,
        toc_path=toc_path,
    )

    print(f"Converted {source.name} to {output_dir}")
    print(f"Pages: {len(search_index)}")
    print(f"Open: {output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
