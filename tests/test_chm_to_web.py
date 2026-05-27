import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.chm_to_web import build_search_index, write_text


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
