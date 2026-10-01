import html
import http.server
import io
import os
import socketserver
import sys
from http import HTTPStatus
from urllib.parse import quote, unquote

import markdown
from markdown.extensions.tables import TableExtension

PORT = 8002
HOST = "127.0.0.1"

SCRIPT_PATH = os.path.abspath(__file__)
BASE_DIR = os.path.dirname(SCRIPT_PATH)
REPORTS_DIR = os.path.join(BASE_DIR, "reports")


class DirtyHandler(http.server.SimpleHTTPRequestHandler):
    dirs_to_skip = ["static"]
    file_patterns_to_skip = [".swo", ".swp", ".json", ".gitkeep"]

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=REPORTS_DIR, **kwargs)

    def send_head(self):
        # Let the standard handler serve directories and assets normally.
        path = self.translate_path(self.path)
        if not path.endswith(".md") or os.path.isdir(path):
            return super().send_head()

        try:
            with open(path, encoding="utf-8") as source:
                text = source.read()
        except OSError:
            self.send_error(404, "Cannot open file")
            return None
        except UnicodeError:
            self.send_error(500, "Markdown file must use UTF-8")
            return None

        title = os.path.basename(path)
        title = html.escape(title)
        extensions = [TableExtension(use_align_attribute=True), "extra"]
        content = markdown.markdown(text, extensions=extensions)

        # A default link target keeps both old and new reports open while browsing.
        html_text = """
        <!DOCTYPE HTML>
        <html>
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <base target="_blank">
                <title>%s</title>
                <link rel="stylesheet" href="/static/reports.css" />
            </head>
            <body>
            %s
            </body>
        </html>
        """ % (title, content)
        body = html_text.encode("utf-8")
        body_size = len(body)
        content_length = str(body_size)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", content_length)
        self.end_headers()

        return io.BytesIO(body)

    def _skip_entry(self, name, full_path):
        # Directory rules match names; file patterns can also match editor backups.
        if os.path.isdir(full_path):
            return name in self.dirs_to_skip

        # Keep an ordinary loop so each skip rule is easy to inspect.
        for pattern in self.file_patterns_to_skip:  # noqa: SIM110
            if pattern in name:
                return True

        return False

    def list_directory(self, path):
        try:
            names = os.listdir(path)
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "No permission to list directory")
            return None

        names.sort(key=str.lower)

        # Decode only for display; escape it before inserting it into HTML.
        displaypath = self.path
        displaypath = displaypath.split("#", 1)[0]
        displaypath = displaypath.split("?", 1)[0]

        try:
            displaypath = unquote(displaypath, errors="surrogatepass")
        except UnicodeDecodeError:
            displaypath = unquote(displaypath)

        displaypath = html.escape(displaypath, quote=False)
        enc = sys.getfilesystemencoding()

        if displaypath == "/":
            displaypath = "Reports"

        title = "Directory listing for %s" % displaypath
        lines = [
            "<!DOCTYPE HTML>",
            '<html lang="en">',
            "<head>",
            '<meta charset="%s">' % enc,
            "<style>:root { color-scheme: light dark; }</style>",
            "<title>%s</title></head>" % title,
            "<body><h1>%s</h1>" % title,
            "<hr><ul>",
        ]

        for name in names:
            fullname = os.path.join(path, name)
            # Continue the entry loop, not an inner loop over skip patterns.
            if self._skip_entry(name, fullname):
                continue

            displayname = name
            linkname = name

            # Append / for directories or @ for symbolic links.
            if os.path.isdir(fullname):
                displayname = name + "/"
                linkname = name + "/"
            if os.path.islink(fullname):
                displayname = name + "@"

            url = quote(linkname, errors="surrogatepass")
            label = html.escape(displayname, quote=False)
            line = '<li><a href="%s">%s</a></li>' % (url, label)
            lines.append(line)

        lines.append("</ul><hr></body></html>")
        page = "\n".join(lines)
        encoded = page.encode(enc, "surrogateescape")
        body_size = len(encoded)
        content_length = str(body_size)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", "text/html; charset=%s" % enc)
        self.send_header("Content-Length", content_length)
        self.end_headers()

        return io.BytesIO(encoded)


if __name__ == "__main__":
    with socketserver.TCPServer((HOST, PORT), DirtyHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
            httpd.server_close()
