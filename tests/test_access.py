"""Exercise real SQLite accounts and the HTTP access boundary."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from access import AccessStore, AccessError, digest
from server import Handler, ThreadingHTTPServer


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AccessStore(Path(self.temp.name) / 'access.sqlite', 'https://observatory.example')
        self.invite = self.store.bootstrap('owner')
        self.invite_token = self.invite['url'].split('token=')[1]
        self.password = 'a-long-real-test-password'
        self.token = self.store.activate(self.invite_token, self.password)
        self.admin = self.store.session(self.token)

    def tearDown(self):
        self.temp.cleanup()

    def test_invite_one_use_and_hashed_credentials(self):
        with self.assertRaises(AccessError):
            self.store.activate(self.invite_token, self.password)
        with self.store.db() as db:
            self.assertNotEqual(db.execute('SELECT password_hash FROM users').fetchone()[0], self.password)
            self.assertEqual(db.execute('SELECT token_hash FROM sessions').fetchone()[0], digest(self.token))
            self.assertEqual(db.execute('SELECT token_hash FROM invites').fetchone()[0], digest(self.invite_token))
        with self.assertRaises(AccessError):
            self.store.bootstrap('another-admin')
        self.assertIn('; Secure', self.store.cookie(self.token))
        self.assertIn('HttpOnly', self.store.cookie(self.token))

    def test_viewer_permissions_revoke_and_reinvite(self):
        invitation = self.store.invite(self.admin['id'], 'visitor')
        token = self.store.activate(invitation['url'].split('token=')[1], self.password)
        viewer = self.store.session(token)
        for action in (lambda: self.store.members(viewer['id']), lambda: self.store.invite(viewer['id'], 'intruder'), lambda: self.store.revoke(viewer['id'], self.admin['id'])):
            with self.assertRaises(AccessError) as result:
                action()
            self.assertEqual(result.exception.status, 403)
        self.store.revoke(self.admin['id'], viewer['id'])
        self.assertIsNone(self.store.session(token))
        with self.assertRaises(AccessError):
            self.store.login('visitor', self.password)
        invitation = self.store.invite(self.admin['id'], 'visitor')
        self.assertTrue(self.store.invitation(invitation['url'].split('token=')[1]))
        with self.assertRaises(AccessError):
            self.store.revoke(self.admin['id'], self.admin['id'])

    def test_password_rotation_logout_expiration(self):
        other = self.store.login('owner', self.password)
        token = self.store.change_password(self.admin['id'], self.password, self.password + '-new')
        self.assertIsNone(self.store.session(other))
        self.assertIsNone(self.store.session(self.token))
        self.assertIsNotNone(self.store.session(token))
        self.store.logout(token)
        self.assertIsNone(self.store.session(token))
        token = self.store.login('owner', self.password + '-new')
        with self.store.db() as db:
            db.execute('UPDATE sessions SET expires_at=0')
        self.assertIsNone(self.store.session(token))
        invite = self.store.invite(self.admin['id'], 'expired')
        with self.store.db() as db:
            db.execute('UPDATE invites SET expires_at=0')
        with self.assertRaises(AccessError):
            self.store.invitation(invite['url'].split('token=')[1])

    def test_rate_limit_and_weak_password(self):
        self.store.budget('test', 1, 60)
        with self.assertRaises(AccessError) as result:
            self.store.budget('test', 1, 60)
        self.assertEqual(result.exception.status, 429)
        invitation = self.store.invite(self.admin['id'], 'weak-password')
        with self.assertRaises(AccessError):
            self.store.activate(invitation['url'].split('token=')[1], 'short')

    def test_http_boundary(self):
        class TestHandler(Handler):
            access = self.store
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, payload=None, cookie=None, origin=None, csrf=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            headers = {'Host':self.store.host}
            if cookie: headers['Cookie'] = self.store.cookie_name + '=' + cookie
            if origin: headers['Origin'] = origin
            if csrf: headers['X-CSRF-Token'] = csrf
            if payload is not None: headers['Content-Type'] = 'application/json'
            conn.request('POST' if payload is not None else 'GET', path, json.dumps(payload) if payload is not None else None, headers)
            response = conn.getresponse()
            result = (response.status, dict(response.getheaders()), response.read())
            conn.close()
            return result
        try:
            for path in ('/api/entries', '/api/entry/28045', '/api/health', '/api/admin/members'):
                self.assertEqual(request(path)[0], 401)
            self.assertEqual(request('/')[0], 302)
            self.assertEqual(request('/login')[0], 200)
            self.assertEqual(request('/api/auth/login', {'username':'owner','password':self.password}, origin='https://evil.example')[0], 403)
            status, headers, body = request('/api/auth/login', {'username':'owner','password':self.password}, origin=self.store.origin)
            self.assertEqual(status, 200)
            self.assertIn('Secure', headers['Set-Cookie'])
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(request('/api/admin/members', cookie=self.token)[0], 200)
            self.assertEqual(request('/api/admin/invite', {'username':'new-viewer'}, cookie=self.token, origin=self.store.origin)[0], 403)
            self.assertEqual(request('/api/admin/invite', {'username':'new-viewer'}, cookie=self.token, origin=self.store.origin, csrf=self.admin['csrf'])[0], 200)
            self.assertEqual(request('/api/auth/logout', {}, cookie=self.token, origin=self.store.origin, csrf=self.admin['csrf'])[0], 200)
            self.assertEqual(request('/api/auth/me', cookie=self.token)[0], 401)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
