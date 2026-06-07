#!/usr/bin/env python3
"""Serve a static directory with pre-compressed gzip assets when available."""

from __future__ import annotations

import argparse
import os
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


COMPRESSIBLE_SUFFIXES = {
    ".css",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".svg",
    ".txt",
    ".xml",
}


class GzipStaticHandler(SimpleHTTPRequestHandler):
    server_version = "CHMStatic/1.0"

    def end_headers(self) -> None:
        url_path = urllib.parse.urlsplit(self.path).path
        cache = "no-cache" if url_path in {"", "/", "/index.html"} else "public, max-age=3600"
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_head(self):  # type: ignore[override]
        path = self.translate_path(self.path)
        if self._can_serve_gzip(path):
            return self._send_gzip(path)
        return super().send_head()

    def _can_serve_gzip(self, path: str) -> bool:
        if os.path.isdir(path):
            return False
        if "range" in {key.lower() for key in self.headers.keys()}:
            return False
        if "gzip" not in self.headers.get("Accept-Encoding", ""):
            return False
        if os.path.splitext(path)[1].lower() not in COMPRESSIBLE_SUFFIXES:
            return False
        return os.path.exists(path) and os.path.exists(f"{path}.gz")

    def _send_gzip(self, path: str):
        gzip_path = f"{path}.gz"
        original_stat = os.stat(path)
        gzip_stat = os.stat(gzip_path)
        self.send_response(200)
        self.send_header("Content-type", self.guess_type(path))
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(gzip_stat.st_size))
        self.send_header("Last-Modified", self.date_time_string(original_stat.st_mtime))
        self.end_headers()
        return open(gzip_path, "rb")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve static files with optional gzip_static support.")
    parser.add_argument("--bind", default="0.0.0.0", help="Address to bind")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--directory", default=".", help="Directory to serve")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    handler = partial(GzipStaticHandler, directory=args.directory)
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"Serving {args.directory} on http://{args.bind}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
