"""Separate SQLite publication state, shared safely by listener and result CLI.

An operation is reserved BEFORE a network write. Interrupted/ambiguous operations
are never automatically resent. Reconcile those records against Discord first.
"""
import json
from pathlib import Path
import sqlite3
from datetime import datetime, UTC


def timestamp():
    return datetime.now(UTC).isoformat(timespec="seconds")


class OrchestrationState:
    def __init__(self,path=":memory:"):
        if str(path)!=":memory:":
            Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.connection=sqlite3.connect(str(path),timeout=10)
        self.connection.row_factory=sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
                operation_key TEXT PRIMARY KEY, status TEXT NOT NULL,
                details TEXT NOT NULL, message_id TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS leaderboards (
                guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, season TEXT NOT NULL,
                leaderboard_type TEXT NOT NULL, message_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(guild_id,channel_id,season,leaderboard_type));
        """)

    def close(self):
        self.connection.close()

    def get(self,key):
        row=self.connection.execute("SELECT * FROM operations WHERE operation_key=?",(key,)).fetchone()
        return dict(row) if row else None

    def claim(self,key,details):
        with self.connection:
            cursor=self.connection.execute("INSERT OR IGNORE INTO operations VALUES (?,'pending',?,NULL,?)",
                (key,json.dumps(details,sort_keys=True),timestamp()))
            return cursor.rowcount==1

    def finish(self,key,message_id):
        with self.connection:
            self.connection.execute("UPDATE operations SET status='posted',message_id=?,updated_at=? WHERE operation_key=?",
                                    (str(message_id),timestamp(),key))

    def fail(self,key):
        with self.connection:
            self.connection.execute("UPDATE operations SET status='uncertain',updated_at=? WHERE operation_key=?",(timestamp(),key))

    def release(self,key):
        with self.connection:
            self.connection.execute("DELETE FROM operations WHERE operation_key=?",(key,))

    def leaderboard(self,guild,channel,season,kind):
        row=self.connection.execute("SELECT * FROM leaderboards WHERE guild_id=? AND channel_id=? AND season=? AND leaderboard_type=?",
                                    (guild,channel,season,kind)).fetchone()
        return dict(row) if row else None

    def save_leaderboard(self,guild,channel,season,kind,message_id):
        with self.connection:
            self.connection.execute("""INSERT INTO leaderboards VALUES (?,?,?,?,?,?)
                ON CONFLICT(guild_id,channel_id,season,leaderboard_type) DO UPDATE SET
                message_id=excluded.message_id,updated_at=excluded.updated_at""",
                (guild,channel,season,kind,str(message_id),timestamp()))
