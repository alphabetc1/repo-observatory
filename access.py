"""Invitation-only accounts with revocable, opaque server-side sessions."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import base64
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

SESSION_SECONDS = 7 * 24 * 3600
INVITE_SECONDS = 2 * 24 * 3600
HASH_SLOTS = threading.BoundedSemaphore(2)


class AccessError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise AccessError('密码长度需要为 12–128 个字符。')
    salt = salt or secrets.token_bytes(16)
    with HASH_SLOTS:
        value = hashlib.scrypt(password.encode(), salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=256 * 1024 * 1024)
    return 'scrypt$131072$8$1$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(value).decode()


def check_password(password, encoded):
    try:
        if not encoded or not encoded.startswith('scrypt$131072$8$1$'):
            return False
        salt = base64.b64decode(encoded.split('$')[4], validate=True)
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, AccessError):
        return False


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{2,39}', value):
        raise AccessError('用户名需为 3–40 位字母、数字、点、横线或下划线。')
    return value.lower()


class AccessStore:
    def __init__(self, path, origin):
        self.path = Path(path)
        parsed = urlsplit(origin)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or not parsed.hostname:
            raise ValueError('Expected a public origin without a path, credentials or query')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')):
            raise ValueError('Public account access requires HTTPS')
        self.origin = origin.rstrip('/')
        self.host = parsed.netloc
        self.secure = parsed.scheme == 'https'
        self.cookie_name = '__Host-cache_session' if self.secure else 'cache_session_dev'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','viewer')),
                    password_hash TEXT, disabled INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invites (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL, used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    bucket TEXT PRIMARY KEY, window_start INTEGER NOT NULL, count INTEGER NOT NULL
                );
            ''')
        self.path.chmod(0o600)
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def budget(self, key, limit, seconds):
        moment = int(time.time())
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM rate_limits WHERE window_start < ?', (moment - 3600,))
            key = digest(key)
            row = db.execute('SELECT * FROM rate_limits WHERE bucket=?', (key,)).fetchone()
            if row and row['window_start'] > moment - seconds:
                if row['count'] >= limit:
                    raise AccessError('尝试次数较多，请稍后再试。', 429)
                db.execute('UPDATE rate_limits SET count=count+1 WHERE bucket=?', (key,))
            else:
                db.execute('INSERT OR REPLACE INTO rate_limits VALUES (?,?,1)', (key, moment))

    def _admin(self, db, actor):
        row = db.execute('SELECT * FROM users WHERE id=? AND disabled=0 AND password_hash IS NOT NULL', (actor,)).fetchone()
        if not row or row['role'] != 'admin':
            raise AccessError('需要管理员权限。', 403)

    def _invite(self, db, name, role, expires):
        name = username(name)
        if role not in ('viewer', 'admin'):
            raise AccessError('无效的账号角色。')
        user = db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
        if user and user['password_hash'] and not user['disabled']:
            raise AccessError('该账号已启用。需要重新邀请时，请先停用账号。', 409)
        if user:
            user_id = user['id']
            db.execute('UPDATE users SET role=?, disabled=0, password_hash=NULL WHERE id=?', (role, user_id))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM invites WHERE user_id=?', (user_id,))
        else:
            user_id = db.execute('INSERT INTO users(username,role,created_at) VALUES (?,?,?)', (name, role, int(time.time()))).lastrowid
        token = secrets.token_urlsafe(32)
        db.execute('INSERT INTO invites VALUES (?,?,?,NULL)', (digest(token), user_id, expires))
        return {'username': name, 'role': role, 'expires_at': expires, 'url': self.origin + '/activate#token=' + token}

    def bootstrap(self, name):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
                raise AccessError('管理员初始化已完成，不能重复初始化。', 409)
            return self._invite(db, name, 'admin', int(time.time()) + 7 * 24 * 3600)

    def invite(self, actor, name, role='viewer'):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._admin(db, actor)
            return self._invite(db, name, role, int(time.time()) + INVITE_SECONDS)

    def _invitation(self, db, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            raise AccessError('邀请链接无效、已使用或已过期。', 410)
        row = db.execute('''SELECT i.*, u.username, u.role, u.disabled, u.password_hash
                            FROM invites i JOIN users u ON u.id=i.user_id
                            WHERE token_hash=?''', (digest(token),)).fetchone()
        if not row or row['used_at'] or row['expires_at'] <= int(time.time()) or row['disabled'] or row['password_hash']:
            raise AccessError('邀请链接无效、已使用或已过期。', 410)
        return row

    def invitation(self, token):
        with self.db() as db:
            row = self._invitation(db, token)
            return {k: row[k] for k in ('username', 'role', 'expires_at')}

    def _session(self, db, user_id):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        moment = int(time.time())
        db.execute('DELETE FROM sessions WHERE expires_at <= ?', (moment,))
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), user_id, csrf, moment + SESSION_SECONDS))
        return token

    def activate(self, token, password):
        self.invitation(token)
        encoded = password_hash(password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._invitation(db, token)
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (encoded, row['user_id']))
            db.execute('UPDATE invites SET used_at=? WHERE token_hash=?', (int(time.time()), digest(token)))
            return self._session(db, row['user_id'])

    def login(self, name, password):
        try:
            name = username(name)
        except AccessError:
            name = ''
        self.budget('user-login:' + name, 12, 900)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
            encoded = user['password_hash'] if user and user['password_hash'] else self.dummy_hash
            valid = check_password(password, encoded)
            if not valid or not user or user['disabled'] or not user['password_hash']:
                raise AccessError('用户名或密码不正确。', 401)
            return self._session(db, user['id'])

    def session(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            return None
        with self.db() as db:
            row = db.execute('''SELECT u.id, u.username, u.role, s.csrf, s.expires_at
                                FROM sessions s JOIN users u ON u.id=s.user_id
                                WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0
                                AND u.password_hash IS NOT NULL''', (digest(token), int(time.time()))).fetchone()
            return dict(row) if row else None

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest(token),))

    def change_password(self, user_id, old_password, new_password):
        encoded = password_hash(new_password)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT * FROM users WHERE id=? AND disabled=0', (user_id,)).fetchone()
            if not user or not check_password(old_password, user['password_hash']):
                raise AccessError('当前密码不正确。', 401)
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (encoded, user_id))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            return self._session(db, user_id)

    def members(self, actor):
        with self.db() as db:
            self._admin(db, actor)
            rows = db.execute('''SELECT u.id,u.username,u.role,u.disabled,u.created_at,
                              (u.password_hash IS NOT NULL) AS activated,
                              MAX(CASE WHEN i.used_at IS NULL THEN i.expires_at END) AS invite_expires_at
                              FROM users u LEFT JOIN invites i ON i.user_id=u.id
                              GROUP BY u.id ORDER BY u.created_at,u.id''').fetchall()
            return [dict(row) for row in rows]

    def revoke(self, actor, user_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._admin(db, actor)
            if actor == user_id:
                raise AccessError('不能停用自己的账号。', 409)
            user = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not user:
                raise AccessError('账号不存在。', 404)
            db.execute('UPDATE users SET disabled=1 WHERE id=?', (user_id,))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM invites WHERE user_id=?', (user_id,))

    def cookie(self, token, clear=False):
        return f'{self.cookie_name}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={0 if clear else SESSION_SECONDS}' + ('; Secure' if self.secure else '')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--bootstrap', metavar='USERNAME', required=True)
    parser.add_argument('--output', type=Path, required=True, help='Private file for the one-time owner setup link')
    args = parser.parse_args()
    store = AccessStore(args.data_dir / 'access.sqlite', args.origin)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
        invite = store.bootstrap(args.bootstrap)
        json.dump(invite, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    print('Owner setup link written to the requested private file')


if __name__ == '__main__':
    main()
