# CHM to Web

Reusable CHM-to-static-web converter.

## Convert A CHM

```sh
python3 tools/chm_to_web.py /path/to/book.chm site --title "Book Title"
```

The converter uses `7z` or `7zz` when available, parses the `.hhc` table of contents, preserves the original HTML/assets, and generates:

- `index.html`
- `styles.css`
- `app.js`
- `data.js`
- `content/`

For Chinese CHM files, the tool decodes CHM metadata with GB18030 while leaving original pages intact so browser charset handling continues to work.

## Preview Output

After conversion, serve the generated static directory:

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory site
```

Then visit:

```text
http://127.0.0.1:8000/
```

Generated pages, extracted CHM contents, screenshots, caches, and source `.chm` files are intentionally ignored by git.
