import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.chm_to_web import build_extract_command, build_search_index, write_site, write_text


def test_build_search_index_includes_htm_and_html(tmp_path: Path) -> None:
    content_dir = tmp_path / "content"
    write_text(content_dir / "chapter1.htm", "<html><title>One</title><body>A</body></html>")
    write_text(content_dir / "chapter2.html", "<html><title>Two</title><body>B</body></html>")

    entries = build_search_index(content_dir, title_by_path={}, max_chars=100)
    paths = [entry["path"] for entry in entries]

    assert paths == ["chapter1.htm", "chapter2.html"]


def test_build_search_index_prefers_toc_title_for_matched_path(tmp_path: Path) -> None:
    content_dir = tmp_path / "content"
    write_text(content_dir / "topic.htm", "<html><title>Page Title</title><body>Body</body></html>")

    entries = build_search_index(content_dir, title_by_path={"topic.htm": "TOC Title"}, max_chars=100)

    assert entries[0]["title"] == "TOC Title"


def test_build_extract_command_supports_windows_hh_decompile(tmp_path: Path) -> None:
    source = tmp_path / "book.chm"
    content_dir = tmp_path / "content"

    command = build_extract_command(source, content_dir, r"C:\Windows\hh.exe")

    assert command == [r"C:\Windows\hh.exe", "-decompile", str(content_dir), str(source)]


def test_build_extract_command_supports_chmextract(tmp_path: Path) -> None:
    source = tmp_path / "book.chm"
    content_dir = tmp_path / "content"

    command = build_extract_command(source, content_dir, "chmextract")

    assert command == ["chmextract", str(source), str(content_dir)]


def test_build_extract_command_defaults_to_7z_style(tmp_path: Path) -> None:
    source = tmp_path / "book.chm"
    content_dir = tmp_path / "content"

    command = build_extract_command(source, content_dir, "7zz")

    assert command == ["7zz", "x", "-y", f"-o{content_dir}", str(source)]


def test_write_site_splits_search_index_from_bootstrap_data(tmp_path: Path) -> None:
    tree = [{"id": "n1", "title": "Topic", "path": "topic.htm", "children": []}]
    search_index = [{"title": "Topic", "path": "topic.htm", "text": "Body text", "excerpt": "Body"}]

    write_site(
        output_dir=tmp_path,
        title="Book",
        source=tmp_path / "book.chm",
        content_dir_name="content",
        tree=tree,
        search_index=search_index,
        aliases={},
        toc_path=tmp_path / "book.hhc",
    )

    data_prefix = "window.CHM_DATA = "
    data = json.loads((tmp_path / "data.js").read_text(encoding="utf-8")[len(data_prefix) :].rstrip(";\n"))
    assert data["search"] == []
    assert data["searchSource"] == "search-data.js"
    assert data["firstPage"] == "topic.htm"

    search_prefix = "window.CHM_SEARCH = "
    search = json.loads(
        (tmp_path / "search-data.js").read_text(encoding="utf-8")[len(search_prefix) :].rstrip(";\n")
    )
    assert search == search_index
