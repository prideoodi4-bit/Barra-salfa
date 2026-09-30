#!/usr/bin/env python3
"""Dependency-free Telegram long-polling runner (Python 3.11+)."""
import fcntl
import json
import logging
import os
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path

from engine import Engine
from store import Store

LOG = logging.getLogger('bara')


class APIError(Exception):
    def __init__(self, code, retry_after=0):
        super().__init__(f'Telegram API error {code}')
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, **payload):
        request = urllib.request.Request(
            self.base + method, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read())
            except (ValueError, OSError):
                raise APIError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # Do not log exceptions with URLs: Telegram URLs contain the bot token.
            raise APIError(0) from None
        if not data.get('ok'):
            raise APIError(data.get('error_code', 0), data.get('parameters', {}).get('retry_after', 0))
        return data['result']

    def authorize(self, chat, uid, admin=False):
        try:
            member = self.call('getChatMember', chat_id=chat, user_id=uid)
        except APIError as exc:
            if exc.code in (400, 403):
                return False
            raise
        status = member.get('status')
        if admin:
            return status in ('creator', 'administrator')
        return status in ('creator', 'administrator', 'member') or (
            status == 'restricted' and member.get('is_member', False))


def load_env(path):
    """Small .env reader; process environment always takes precedence."""
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Runner:
    def __init__(self, store, engine, api):
        self.s, self.engine, self.api = store, engine, api
        self.running = True
        self.chat_next = {}

    def process(self, update):
        if update['update_id'] < self.s.offset():
            return
        with self.s.db:
            self.engine.handle(update)
            self.s.set_offset(update['update_id'] + 1)

    def flush(self, limit=8):
        """Outbound retries survive restarts. Delivery may duplicate after a crash.

        Telegram sendMessage has no idempotency key; game transitions and scores
        are nevertheless exactly-once through the SQLite update transaction.
        """
        rows = self.s.db.execute('SELECT * FROM outbox WHERE due<=? ORDER BY id LIMIT 100',
                                 (time.time(),)).fetchall()
        sent = 0
        blocked = set()
        for row in rows:
            if sent >= limit:
                break
            payload = json.loads(row['payload'])
            chat = payload.get('chat_id')
            if chat in blocked:
                continue
            if row['method'] == 'sendMessage' and self.chat_next.get(chat, 0) > time.time():
                blocked.add(chat)
                continue
            # finish() may have removed rows since this batch was read.
            if not self.s.db.execute('SELECT 1 FROM outbox WHERE id=?', (row['id'],)).fetchone():
                continue
            try:
                self.api.call(row['method'], **payload)
            except APIError as exc:
                permanent = exc.code in (400, 401, 403, 404) or row['attempts'] >= 9
                with self.s.db:
                    if permanent:
                        self.s.db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
                        game = self.s.game(row['game']) if row['game'] else None
                        if row['essential'] and game and game['stage'] != 'ended':
                            if chat == game['chat']:
                                # Topic deleted/closed: cancel silently; never fall back to General.
                                self.s.db.execute('DELETE FROM outbox WHERE game=?', (game['id'],))
                                game.update(stage='ended', result='delivery_failed', deadline=0)
                                self.s.save(game)
                            else:
                                self.engine.finish(game, 'cancelled', 'تعذّر إيصال رسالة خاصة لأحد اللاعبين. الكيم انلغى بدون نقاط.')
                    else:
                        delay = max(exc.retry_after, min(60, 2 ** min(row['attempts'], 6)))
                        self.s.db.execute('UPDATE outbox SET attempts=attempts+1,due=? WHERE id=?',
                                          (time.time() + delay, row['id']))
                blocked.add(chat)
                LOG.warning('Outbound %s failed with code %s; retry=%s', row['method'], exc.code, not permanent)
                if exc.code == 401:
                    raise RuntimeError('Bot token was rejected; update BOT_TOKEN.') from None
            else:
                with self.s.db:
                    self.s.db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
                if row['method'] == 'sendMessage':
                    self.chat_next[chat] = time.time() + 1.1
                sent += 1

    def run(self):
        backoff = 1
        while self.running:
            try:
                with self.s.db:
                    self.engine.tick()
                self.flush()
                updates = self.api.call('getUpdates', offset=self.s.offset(), timeout=1, limit=30,
                                        allowed_updates=['message', 'callback_query'])
                for update in updates:
                    self.process(update)
                backoff = 1
            except APIError as exc:
                if exc.code in (401, 409):
                    raise RuntimeError('Invalid token or another polling/webhook instance is active.') from None
                LOG.warning('Telegram temporarily unavailable (code %s).', exc.code)
                time.sleep(min(30, max(backoff, exc.retry_after)))
                backoff = min(30, backoff * 2)
        self.s.db.close()


def main():
    os.umask(0o077)
    load_env(Path(__file__).with_name('.env'))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or token == 'PASTE_BOTFATHER_TOKEN_HERE':
        raise SystemExit('حط توكن BotFather في BOT_TOKEN داخل .env أو متغيرات البيئة أولاً.')
    path = os.getenv('DATABASE_PATH', str(Path(__file__).with_name('data') / 'game.sqlite3'))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    lock = open(path + '.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another bot process already uses this database.') from None
    api = Telegram(token)
    me = api.call('getMe')
    webhook = api.call('getWebhookInfo')
    if webhook.get('url'):
        raise SystemExit('This bot has an active webhook. Remove it before running the polling version.')
    store = Store(path)
    engine = Engine(store, me['username'], api.authorize)
    runner = Runner(store, engine, api)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: setattr(runner, 'running', False))
    LOG.info('Bot started: @%s', me['username'])
    runner.run()


if __name__ == '__main__':
    main()
