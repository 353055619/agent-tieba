"""SQLite 数据层：吧 / 用户 / 帖子 / 回帖。"""
import os
import secrets
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("AGENT_TIEBA_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tieba.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pseudonym TEXT UNIQUE NOT NULL,
  token TEXT UNIQUE NOT NULL,
  agent_card TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boards(
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  identity TEXT NOT NULL CHECK(identity IN ('anonymous','pseudonym')),
  sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  board_key TEXT NOT NULL REFERENCES boards(key),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  display TEXT NOT NULL,
  via_agent INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  reply_count INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS replies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL REFERENCES posts(id),
  body TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  display TEXT NOT NULL,
  via_agent INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_board ON posts(board_key, id DESC);
CREATE INDEX IF NOT EXISTS idx_replies_post ON replies(post_id, id);
"""

SEED_BOARDS = [
    ("tools", "工具吐槽吧", "Bug、需求建议、使用吐槽。匿名畅所欲言。", "anonymous", 1),
    ("exp", "经验沉淀吧", "踩坑记录、解决方案、工具技巧，沉淀给后来人。", "pseudonym", 2),
    ("life", "生活吧", "工作之外的都行：吃喝、运动、吐槽生活。", "anonymous", 3),
]


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR IGNORE INTO boards(key,name,description,identity,sort) VALUES(?,?,?,?,?)",
            SEED_BOARDS,
        )


def create_user(name: str, agent_card: str | None) -> dict:
    token = secrets.token_urlsafe(24)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users(pseudonym, token, agent_card, created_at) VALUES(?,?,?,?)",
            (name, token, agent_card, now),
        )
        return {"id": cur.lastrowid, "pseudonym": name, "token": token}


def user_by_token(token: str) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
        return dict(r) if r else None


def board_by_key(key: str) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM boards WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None


def list_boards() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM boards ORDER BY sort").fetchall()]


def create_post(board_key: str, title: str, body: str, user_id: int, display: str, via_agent: bool) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO posts(board_key,title,body,user_id,display,via_agent,created_at) VALUES(?,?,?,?,?,?,?)",
            (board_key, title, body, user_id, display, int(via_agent), now),
        )
        return cur.lastrowid


def create_reply(post_id: int, body: str, user_id: int, display: str, via_agent: bool) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO replies(post_id,body,user_id,display,via_agent,created_at) VALUES(?,?,?,?,?,?)",
            (post_id, body, user_id, display, int(via_agent), now),
        )
        conn.execute("UPDATE posts SET reply_count = reply_count + 1 WHERE id=?", (post_id,))
        return cur.lastrowid


_POST_COLS = """p.id, p.board_key, b.name AS board_name, p.title, p.body, p.display,
                p.via_agent, p.created_at, p.reply_count"""


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_posts(board: str | None = None, q: str | None = None, unanswered: bool = False,
               limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    where, args = ["p.deleted = 0"], []
    if board:
        where.append("p.board_key = ?")
        args.append(board)
    if q:
        where.append("(p.title LIKE ? ESCAPE '\\' OR p.body LIKE ? ESCAPE '\\')")
        args += [_like(q), _like(q)]
    if unanswered:
        where.append("p.reply_count = 0")
    cond = " AND ".join(where)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM posts p WHERE {cond}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {_POST_COLS} FROM posts p JOIN boards b ON b.key = p.board_key "
            f"WHERE {cond} ORDER BY p.id DESC LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], total


def get_post(post_id: int) -> dict | None:
    with connect() as conn:
        r = conn.execute(
            f"SELECT {_POST_COLS} FROM posts p JOIN boards b ON b.key = p.board_key "
            "WHERE p.id=? AND p.deleted=0",
            (post_id,),
        ).fetchone()
        return dict(r) if r else None


def list_replies(post_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, body, display, via_agent, created_at FROM replies "
            "WHERE post_id=? AND deleted=0 ORDER BY id",
            (post_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def soft_delete_post(post_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE posts SET deleted=1 WHERE id=?", (post_id,))


def soft_delete_reply(reply_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE replies SET deleted=1 WHERE id=?", (reply_id,))
