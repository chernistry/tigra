# Security

## Reporting a vulnerability

Please report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue, and do not attach a real data export;
a minimal synthetic one (see `tests/synthetic.py`) is enough to show a problem.

You will get an answer within 7 days. Fixes are released as a patch version and noted in `CHANGELOG.md`.

## What the tool is designed to guarantee

- It reads only the files you give it and makes no network requests, at build time or when the page opens.
- The generated `graph.html` carries a Content-Security-Policy of `default-src 'none'`: no scripts,
  styles, fonts or images from anywhere else, and no outgoing connections.
- Contact data is embedded as escaped JSON and inserted with `textContent`, never as HTML.
- Output files are created readable by your user only (mode 0600). An output folder the tool creates is 0700;
  a folder that already exists keeps its permissions.
- Message and invitation text is never stored or written out: only who, which conversation, when, which way,
  and whether an invitation had a note.

A way to break any of these is a vulnerability. So is a crafted export that makes the page run code.

## Supported versions

The latest release only.
