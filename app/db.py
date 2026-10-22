"""SQLite 数据层：用户（钱包）/ 悬赏帖 / 回答 / 账本。"""
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

from .identity import anon_name

DB_PATH = os.environ.get("AGENT_TIEBA_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tieba.db"
)

INITIAL_COINS = 100   # 注册即得
MIN_STAKE = 1         # 悬赏押币下限
MAX_STAKE = 50        # 悬赏押币上限
ADOPT_BONUS = 5       # 被采纳时系统额外铸币
REFUND_DAYS = 7       # 无人回答自动退款天数
TOPUP_THRESHOLD = 10  # 余额低于此值触发周回填
TOPUP_TO = 50         # 回填目标

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pseudonym TEXT UNIQUE NOT NULL,
  persona TEXT NOT NULL,
  token TEXT UNIQUE NOT NULL,
  agent_card TEXT,
  balance INTEGER NOT NULL DEFAULT 100,
  adopted_count INTEGER NOT NULL DEFAULT 0,
  last_refill_week TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  display TEXT NOT NULL,
  stake INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','adopted','refunded')),
  via_agent INTEGER NOT NULL DEFAULT 0,
  adopted_reply_id INTEGER,
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
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  post_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, stake DESC);
CREATE INDEX IF NOT EXISTS idx_replies_post ON replies(post_id, id);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id DESC);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ledger(conn, user_id: int, delta: int, reason: str, post_id: int | None) -> None:
    conn.execute(
        "INSERT INTO ledger(user_id, delta, reason, post_id, created_at) VALUES(?,?,?,?,?)",
        (user_id, delta, reason, post_id, _now()),
    )


def week_id(dt: datetime | None = None) -> str:
    y, w, _ = (dt or datetime.now()).isocalendar()
    return f"{y}-W{w:02d}"


def create_user(name: str | None, agent_card: str | None) -> dict:
    now = _now()
    with connect() as conn:
        # 先占坑拿到 id，再由 id 派生全站稳定的匿名身份（pseudonym 有 UNIQUE 约束，占位符用随机串避免空串互撞）
        cur = conn.execute(
            "INSERT INTO users(pseudonym, persona, token, agent_card, created_at) VALUES(?,?,?,?,?)",
            (f"__reg{secrets.token_hex(6)}", "", secrets.token_urlsafe(24), agent_card, now),
        )
        uid = cur.lastrowid
        persona = anon_name(uid, "hall")
        for attempt in range(5):
            try:
                if not name:
                    name = persona if attempt == 0 else f"{persona}{1000 + secrets.randbelow(9000)}"
                conn.execute(
                    "UPDATE users SET pseudonym=?, persona=? WHERE id=?", (name, persona, uid)
                )
                break
            except sqlite3.IntegrityError:
                name = None
                continue
        user = dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
        _ledger(conn, uid, INITIAL_COINS, "reg_bonus", None)
        return user


def user_by_token(token: str) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
        return dict(r) if r else None


def weekly_topup(user: dict) -> dict:
    """余额低于阈值且本周未回填过 → 回填到 TOPUP_TO（系统铸币）。"""
    if user["balance"] >= TOPUP_THRESHOLD or user["last_refill_week"] == week_id():
        return user
    with connect() as conn:
        conn.execute(
            "UPDATE users SET balance=?, last_refill_week=? WHERE id=?",
            (TOPUP_TO, week_id(), user["id"]),
        )
        topup = TOPUP_TO - user["balance"]
        _ledger(conn, user["id"], topup, "weekly_topup", None)
    return user_by_token(user["token"])


def create_bounty(user: dict, title: str, body: str, stake: int, via_agent: bool) -> int | None:
    """押币发悬赏；余额不足返回 None。"""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?",
            (stake, user["id"], stake),
        )
        if cur.rowcount == 0:
            return None
        cur2 = conn.execute(
            "INSERT INTO posts(title, body, user_id, display, stake, via_agent, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (title, body, user["id"], user["persona"], stake, int(via_agent), _now()),
        )
        pid = cur2.lastrowid
        _ledger(conn, user["id"], -stake, "stake", pid)
        return pid


def answer(post_id: int, user: dict, body: str, via_agent: bool) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO replies(post_id, body, user_id, display, via_agent, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (post_id, body, user["id"], user["persona"], int(via_agent), _now()),
        )
        rid = cur.lastrowid
        conn.execute("UPDATE posts SET reply_count = reply_count + 1 WHERE id=?", (post_id,))
        return rid


def adopt(post_id: int, reply_id: int, reply_user_id: int, stake: int) -> bool:
    """原子采纳：状态翻转成功后转账（押币 + 系统奖励给答主）。"""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE posts SET status='adopted', adopted_reply_id=? "
            "WHERE id=? AND status='open' AND deleted=0",
            (reply_id, post_id),  # 回答归属已在路由层校验
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE users SET balance = balance + ?, adopted_count = adopted_count + 1 WHERE id=?",
            (stake + ADOPT_BONUS, reply_user_id),
        )
        _ledger(conn, reply_user_id, stake, "adopt_reward", post_id)
        _ledger(conn, reply_user_id, ADOPT_BONUS, "sys_bonus", post_id)
        return True


def sweep_refunds() -> int:
    """懒执行：挂出超 REFUND_DAYS 天且零回答的悬赏自动退款。"""
    cutoff = (datetime.now() - timedelta(days=REFUND_DAYS)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, stake FROM posts WHERE status='open' AND deleted=0 "
            "AND reply_count=0 AND created_at < ?",
            (cutoff,),
        ).fetchall()
        for r in rows:
            cur = conn.execute(
                "UPDATE posts SET status='refunded' WHERE id=? AND status='open'", (r["id"],)
            )
            if cur.rowcount == 0:
                continue
            conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (r["stake"], r["user_id"]))
            _ledger(conn, r["user_id"], r["stake"], "refund", r["id"])
        return len(rows)


def force_refund(post_id: int) -> bool:
    """管理员代裁：即使已有回答也可退回押币。"""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE posts SET status='refunded' WHERE id=? AND status='open'", (post_id,)
        )
        if cur.rowcount == 0:
            return False
        r = conn.execute("SELECT user_id, stake FROM posts WHERE id=?", (post_id,)).fetchone()
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (r["stake"], r["user_id"]))
        _ledger(conn, r["user_id"], r["stake"], "refund", post_id)
        return True


def force_adopt(post_id: int, reply_id: int, reply_user_id: int, stake: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE posts SET status='adopted', adopted_reply_id=? WHERE id=? AND status='open'",
            (reply_id, post_id),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE users SET balance = balance + ?, adopted_count = adopted_count + 1 WHERE id=?",
            (stake + ADOPT_BONUS, reply_user_id),
        )
        _ledger(conn, reply_user_id, stake, "adopt_reward", post_id)
        _ledger(conn, reply_user_id, ADOPT_BONUS, "sys_bonus", post_id)
        return True


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_posts(status: str = "open", q: str | None = None, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    where, args = ["p.deleted = 0"], []
    if status != "all":
        where.append("p.status = ?")
        args.append(status)
    if q:
        where.append("(p.title LIKE ? ESCAPE '\\' OR p.body LIKE ? ESCAPE '\\')")
        args += [_like(q), _like(q)]
    cond = " AND ".join(where)
    order = "p.stake DESC, p.id DESC" if status == "open" else "p.id DESC"
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM posts p WHERE {cond}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT p.id, p.title, p.body, p.display, p.stake, p.status, p.via_agent, "
            f"p.adopted_reply_id, p.created_at, p.reply_count FROM posts p "
            f"WHERE {cond} ORDER BY {order} LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], total


def get_post(post_id: int) -> dict | None:
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM posts WHERE id=? AND deleted=0", (post_id,)
        ).fetchone()
        return dict(r) if r else None


def list_replies(post_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, body, user_id, display, via_agent, created_at FROM replies "
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


def leaderboard(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT u.persona, u.adopted_count, u.balance, "
            "(SELECT COUNT(*) FROM posts p WHERE p.user_id=u.id AND p.deleted=0) AS posted "
            "FROM users u ORDER BY u.adopted_count DESC, u.balance DESC, u.id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def audit_users() -> list[dict]:
    """管理员视角：匿名身份与账号的对应关系 + 余额 + token（找回用）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT u.id, u.pseudonym, u.persona, u.balance, u.adopted_count, u.token, u.created_at, "
            "(SELECT COUNT(*) FROM posts p WHERE p.user_id=u.id AND p.deleted=0) AS posted "
            "FROM users u ORDER BY u.id"
        ).fetchall()
        return [dict(r) for r in rows]
