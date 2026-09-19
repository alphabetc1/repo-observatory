"""Dashboard behind a loopback proxy with optional invitation-only access."""
from __future__ import annotations

import argparse
import hmac
from http.cookies import SimpleCookie
from access import AccessStore, AccessError
import gzip
import hashlib
import threading
import json
import mimetypes
import re
import time
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pr_status import refresh as refresh_status
from settings import CONFIG
from cacheboard import REPO
from localization import translate_data
from assets import render_asset

ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    access = None
    data_dir = ROOT / "data"
    static_dir = ROOT / "static"
    payload_cache = {}
    cache_lock = threading.Lock()
    document_signature = None
    document = None
    sync_status = None
    refresh_slots = threading.BoundedSemaphore(2)
    recent_refreshes = {}
    refresh_enabled = bool(os.environ.get("GITHUB_TOKEN") or (os.environ.get("CREDENTIALS_DIRECTORY") and (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "github-token").exists()))
    refresh_guard = threading.Lock()

    def do_GET(self):
        path = urlparse(self.path).path
        if self.access:
            if path == '/api/auth/me':
                user = self.current_user()
                return self.json_response(200, user) if user else self.json_response(401, {'error': '请先登录。'})
            if path == '/api/admin/members':
                try:
                    user = self.require_user()
                    return self.json_response(200, self.access.members(user['id']))
                except AccessError as error:
                    return self.json_response(error.status, {'error': str(error)})
            if path not in ('/login', '/activate', '/auth.js', '/auth.css', '/style.css', '/favicon.svg', '/locale.js', '/locale.css') and not self.current_user():
                if path.startswith('/api/'):
                    return self.json_response(401, {'error': '请先登录。'})
                self.send_response(302)
                self.send_header('Location', self.access.origin + '/login')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
        elif path == '/api/auth/me':
            return self.json_response(200, {'enabled': False})
        detail_match = re.fullmatch(r"/api/entry/(\d+)", path)
        if path in ("/api/entries", "/api/health") or detail_match:
            try:
                snapshot_path = self.data_dir / "snapshot.json"
                status_path = self.data_dir / "status.json"
                workflow_path = self.data_dir / 'workflow-status.json'
                signature = (workflow_path.stat().st_mtime_ns if workflow_path.exists() else 0, snapshot_path.stat().st_mtime_ns, status_path.stat().st_mtime_ns if status_path.exists() else 0)
                with self.cache_lock:
                    cls = type(self)
                    if cls.document_signature != signature:
                        cls.document = json.loads(snapshot_path.read_text())
                        if cls.document.get('repository', REPO) != REPO:
                            raise ValueError('Workspace repository mismatch')
                        workflows = json.loads(workflow_path.read_text()) if workflow_path.exists() else {'entries': {}, 'updated_at': None}
                        cls.document['workflow_updated_at'] = workflows.get('updated_at')
                        cls.document['workflow_refresh_enabled'] = cls.refresh_enabled
                        entries = []
                        for entry in cls.document['entries']:
                            entry['status_refresh_enabled'] = cls.refresh_enabled
                            workflow = workflows['entries'].get(str(entry['number']))
                            if workflow:
                                if workflow.get('state') in ('closed', 'merged'):
                                    continue
                                entry['workflow'] = workflow
                                if 'draft' in workflow:
                                    entry['draft'] = workflow['draft']
                            entries.append(entry)
                        cls.document['entries'] = entries
                        cls.sync_status = json.loads(status_path.read_text()) if status_path.exists() else {"ok": True}
                        cls.payload_cache.clear()
                        cls.document_signature = signature
                    snapshot, status = cls.document, cls.sync_status
                    if path not in cls.payload_cache:
                        if path == "/api/entries":
                            summaries = []
                            for entry in snapshot["entries"]:
                                summary = {key: value for key, value in entry.items() if key not in ("body", "explanation", "labels", "comments", "priority_reason", "related")}
                                if len(summary['summary']) > 180:
                                    summary['summary'] = summary['summary'][:180] + '…'
                                if summary.get('workflow'):
                                    workflow = {k: v for k, v in summary['workflow'].items() if k not in ('reviews', 'reviewers', 'error', 'source_updated_at', 'head_sha', 'base_ref', 'number')}
                                    workflow['error'] = bool(summary['workflow'].get('error'))
                                    if workflow.get('base_ci'):
                                        workflow['base_ci'] = {k: v for k, v in workflow['base_ci'].items() if k in ('state', 'url')}
                                    summary['workflow'] = workflow
                                if summary.get("editorial"):
                                    summary["editorial"] = {key: value for key, value in summary["editorial"].items() if key != "summary"}
                                summaries.append(summary)
                            payload = {**snapshot, "entries": summaries, "sync_status": status}
                        elif detail_match:
                            number = int(detail_match[1])
                            entry = next((entry for entry in snapshot["entries"] if entry["number"] == number), None)
                            if entry is None:
                                return self.send_content(404, b'{"error":"Entry is no longer in the open snapshot"}', "application/json")
                            payload = {"entry": entry, "synced_at": snapshot["synced_at"]}
                        else:
                            payload = {"ok": True, "sync_ok": status.get("ok", False), "synced_at": snapshot["synced_at"], "entries": len(snapshot["entries"])}
                        cls.payload_cache[path] = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
                    body = cls.payload_cache[path]
                return self.send_content(200, body, "application/json; charset=utf-8")
            except (OSError, ValueError, KeyError):
                return self.send_content(503, b'{"error":"The first complete snapshot is not available yet."}', "application/json")
        allowed = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
        allowed.update({'/login': 'auth.html', '/activate': 'auth.html', '/access': 'auth.html', '/auth.js': 'auth.js', '/auth.css': 'auth.css'})
        allowed.update({'/locale.js': 'locale.js', '/locale.css': 'locale.css'})
        if path not in allowed:
            return self.send_content(404, b"Not found", "text/plain")
        target = self.static_dir / allowed[path]
        try:
            body = target.read_bytes()
            if target.suffix in ('.html', '.js'):
                body = render_asset(target.name, body.decode(), self.language()).encode()
        except OSError:
            return self.send_content(404, b"Not found", "text/plain")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return self.send_content(200, body, content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else ""))

    def language(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            value = cookie['repo_language'].value if 'repo_language' in cookie else CONFIG['default_language']
            return value if value in ('en', 'zh-CN') else CONFIG['default_language']
        except Exception:
            return CONFIG['default_language']

    def current_user(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            self.session_token = cookie[self.access.cookie_name].value if self.access.cookie_name in cookie else ''
        except Exception:
            self.session_token = ''
        return self.access.session(self.session_token)

    def require_user(self):
        user = self.current_user()
        if not user:
            raise AccessError('请先登录。', 401)
        return user

    def json_response(self, status, value):
        return self.send_content(status, json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_POST(self):
        if self.access:
            try:
                if self.headers.get('Origin') != self.access.origin or self.headers.get('Host') != self.access.host:
                    raise AccessError('Same-origin request required', 403)
                path = urlparse(self.path).path
                public = path in ('/api/auth/login', '/api/auth/invitation', '/api/auth/activate')
                if not public:
                    user = self.require_user()
                    if not hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), user['csrf'].encode()):
                        raise AccessError('请刷新页面后重试。', 403)
                if path.startswith('/api/auth/') or path.startswith('/api/admin/'):
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise AccessError('JSON required', 415)
                    try:
                        size = int(self.headers.get('Content-Length', '0'))
                        if not 0 < size <= 8192:
                            raise ValueError()
                        payload = json.loads(self.rfile.read(size))
                        if not isinstance(payload, dict):
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        raise AccessError('Invalid request body')
                    if public or path == '/api/auth/password':
                        self.access.budget('ip:' + self.headers.get('X-Real-IP', self.client_address[0]), 40, 900)
                    result = {'ok': True}
                    token = None
                    if path == '/api/auth/login':
                        token = self.access.login(payload.get('username'), payload.get('password'))
                    elif path == '/api/auth/invitation':
                        result = self.access.invitation(payload.get('token'))
                    elif path == '/api/auth/activate':
                        token = self.access.activate(payload.get('token'), payload.get('password'))
                    elif path == '/api/auth/logout':
                        self.access.logout(self.session_token)
                        self.response_cookie = self.access.cookie('', clear=True)
                    elif path == '/api/auth/password':
                        token = self.access.change_password(user['id'], payload.get('old_password'), payload.get('password'))
                    elif path == '/api/admin/invite':
                        result = self.access.invite(user['id'], payload.get('username'), payload.get('role', 'viewer'))
                    elif path == '/api/admin/revoke':
                        if type(payload.get('user_id')) is not int:
                            raise AccessError('Invalid user id')
                        self.access.revoke(user['id'], payload['user_id'])
                    else:
                        raise AccessError('Not found', 404)
                    if token:
                        self.response_cookie = self.access.cookie(token)
                    return self.json_response(200, result)
            except AccessError as error:
                return self.json_response(error.status, {'error': str(error)})
        return self.refresh_post()

    def refresh_post(self):
        match = re.fullmatch(r'/api/entry/(\d+)/refresh-status', urlparse(self.path).path)
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        if not match:
            return self.send_content(404, b'Not found', 'text/plain')
        if not self.access and (host.split(':')[0] not in ('localhost', '127.0.0.1') or self.headers.get('X-Cache-Refresh') != '1' or (origin and origin != 'http://' + host)):
            return self.send_content(403, b'{"error":"Same-origin request required"}', 'application/json')
        if not self.refresh_enabled:
            return self.send_content(503, b'{"error":"GitHub authentication is not configured"}', 'application/json')
        number = int(match[1])
        try:
            document = json.loads((self.data_dir / 'snapshot.json').read_text())
            if not any(e['number'] == number for e in document['entries']):
                return self.send_content(404, b'{"error":"Entry is outside the snapshot"}', 'application/json')
        except (OSError, ValueError):
            return self.send_content(503, b'{"error":"Snapshot unavailable"}', 'application/json')
        with self.refresh_guard:
            if time.monotonic() - self.recent_refreshes.get(number, -60) < 60:
                return self.send_content(429, b'{"error":"Please wait one minute before refreshing this entry again"}', 'application/json')
            if not self.refresh_slots.acquire(blocking=False):
                return self.send_content(429, b'{"error":"Two refreshes are already running"}', 'application/json')
            self.recent_refreshes[number] = time.monotonic()
        try:
            result = refresh_status(self.data_dir, [number])
            return self.send_content(502 if result['errors'] else 200, json.dumps(result).encode(), 'application/json')
        except Exception:
            return self.send_content(502, b'{"error":"GitHub status refresh failed; previous values retained"}', 'application/json')
        finally:
            self.refresh_slots.release()

    def send_content(self, status, body, content_type):
        if content_type.startswith('application/json') and self.language() == 'en':
            body = json.dumps(translate_data(json.loads(body)), ensure_ascii=False).encode()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if not self.access and status == 200 and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        compressed = len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
        if compressed:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if self.access else "no-cache")
        if getattr(self, "response_cookie", None):
            self.send_header("Set-Cookie", self.response_cookie)
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding, Cookie")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    Handler.data_dir = args.data_dir
    if os.environ.get('CACHEBOARD_PUBLIC_ORIGIN'):
        Handler.access = AccessStore(args.data_dir / 'access.sqlite', os.environ['CACHEBOARD_PUBLIC_ORIGIN'])
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Cache Observatory listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()
