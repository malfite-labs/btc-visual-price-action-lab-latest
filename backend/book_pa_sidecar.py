"""Loopback read-only BOOK-PA result service. No account or trading routes."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.book_pa_research import KNOWLEDGE, latest_research

PATH = '/api/v1/research/book-pa/latest'


def create_server(host='127.0.0.1', port=8813, db_path=KNOWLEDGE / 'research_results.sqlite3'):
    if host != '127.0.0.1':
        raise ValueError('LOOPBACK_REQUIRED')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != PATH:
                self.send_error(404)
                return
            body = json.dumps(latest_research(Path(db_path)), ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            origin = self.headers.get('Origin')
            if origin in {'http://127.0.0.1:5174', 'http://localhost:5174'}:
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_error(405)

        def log_message(self, fmt, *args):
            pass
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == '__main__':
    create_server().serve_forever()
