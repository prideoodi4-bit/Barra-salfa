"""Single-process SQLite persistence and durable outbound messages."""
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, chat INTEGER, topic INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS bindings(chat INTEGER PRIMARY KEY, topic INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(chat INTEGER PRIMARY KEY, seconds INTEGER NOT NULL,
                question_count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS used(chat INTEGER, word TEXT, PRIMARY KEY(chat,word));
            CREATE TABLE IF NOT EXISTS scores(chat INTEGER, uid INTEGER, name TEXT, points INTEGER,
                PRIMARY KEY(chat,uid));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, method TEXT, payload TEXT,
                game TEXT, essential INTEGER DEFAULT 0, due REAL DEFAULT 0, attempts INTEGER DEFAULT 0);
        ''')

    def save(self, game):
        self.db.execute('INSERT OR REPLACE INTO games VALUES(?,?,?,?)',
                        (game['id'], game['chat'], game['topic'], json.dumps(game, ensure_ascii=False)))

    def game(self, rid):
        row = self.db.execute('SELECT data FROM games WHERE id=?', (rid,)).fetchone()
        return json.loads(row[0]) if row else None

    def active(self, chat, topic=None):
        # One active game per group avoids overlapping private roles.
        for row in self.db.execute('SELECT data FROM games WHERE chat=?', (chat,)):
            g = json.loads(row[0])
            if g['stage'] != 'ended' and (topic is None or g['topic'] == topic):
                return g
        return None

    def active_games(self):
        return [g for row in self.db.execute('SELECT data FROM games')
                if (g := json.loads(row[0]))['stage'] != 'ended']

    def binding(self, chat):
        row = self.db.execute('SELECT topic FROM bindings WHERE chat=?', (chat,)).fetchone()
        return row[0] if row else None

    def used(self, chat):
        return {r[0] for r in self.db.execute('SELECT word FROM used WHERE chat=?', (chat,))}

    def settings(self, chat):
        row = self.db.execute('SELECT seconds,question_count FROM settings WHERE chat=?', (chat,)).fetchone()
        return dict(row) if row else {'seconds': 35, 'question_count': 12}

    def set_settings(self, chat, seconds, question_count):
        if not 20 <= seconds <= 90 or not 4 <= question_count <= 40 or question_count % 2:
            raise ValueError('Timer must be 20–90 seconds; question count must be even, 4–40.')
        self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?,?)', (chat, seconds, question_count))

    def enqueue(self, method, payload, game=None, essential=False):
        self.db.execute('INSERT INTO outbox(method,payload,game,essential) VALUES(?,?,?,?)',
                        (method, json.dumps(payload, ensure_ascii=False), game, int(essential)))

    def offset(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(value),))

    def add_score(self, chat, player, points):
        self.db.execute('''INSERT INTO scores VALUES(?,?,?,?) ON CONFLICT(chat,uid)
                           DO UPDATE SET points=points+excluded.points,name=excluded.name''',
                        (chat, player['uid'], player['name'], points))

    def scoreboard(self, chat):
        return [dict(r) for r in self.db.execute(
            'SELECT * FROM scores WHERE chat=? ORDER BY points DESC,uid LIMIT 15', (chat,))]
