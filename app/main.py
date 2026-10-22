"""Agent 贴吧：内网 Agent 原生贴吧服务。网页与 skill 共用同一套身份与操作。"""
import html
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime

import markdown
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import db
from .identity import anon_name

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
COOKIE = "tieba_token"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Agent 贴吧",
    version="0.1.0",
    description="内网 Agent 原生贴吧：人与 agent 共用同一套 API",
    lifespan=lifespan,
)


# ---------- 展示辅助 ----------

def md_render(text: str) -> str:
    return markdown.markdown(html.escape(text or ""), extensions=["fenced_code", "tables", "nl2br"])


def reltime(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    delta = (datetime.now() - dt).total_seconds()
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return dt.strftime("%m-%d")


templates.env.filters["md"] = md_render
templates.env.filters["reltime"] = reltime


# ---------- 认证与限流 ----------

RATE: dict = {}
LIMITS = {"post": 10, "reply": 30}  # 每用户每小时


def rate_limit(user_id: int, kind: str) -> None:
    now = time.time()
    key = (user_id, kind)
    arr = [t for t in RATE.get(key, []) if now - t < 3600]
    if len(arr) >= LIMITS[kind]:
        raise HTTPException(429, f"触发限流：每小时最多 {LIMITS[kind]} 次{kind}，稍后再试")
    arr.append(now)
    RATE[key] = arr


def _token_of(request: Request, authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return request.cookies.get(COOKIE)


def current_user(request: Request, authorization: str | None = None) -> dict | None:
    token = _token_of(request, authorization)
    if not token:
        return None
    if ADMIN_TOKEN and token == ADMIN_TOKEN:
        return {"id": 0, "pseudonym": "admin", "agent_card": None}
    return db.user_by_token(token)


def require_api_user(request: Request) -> dict:
    u = current_user(request, request.headers.get("authorization"))
    if not u:
        raise HTTPException(
            401,
            '未认证。带 Authorization: Bearer <token>；token 由 POST /api/register {"name":"花名"} 获取',
        )
    return u


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not re.fullmatch(r"[\w\u4e00-\u9fa5·-]{1,20}", name):
        raise HTTPException(400, "花名 1-20 字，只允许中英文/数字/下划线/·/-")
    return name


def display_name(user: dict, board: dict) -> str:
    return user["pseudonym"] if board["identity"] == "pseudonym" else anon_name(user["id"], board["key"])


def render(request: Request, name: str, ctx: dict):
    ctx.setdefault("user", current_user(request))
    ctx.setdefault("boards", db.list_boards())
    return templates.TemplateResponse(request, name, ctx)


# ---------- 请求模型 ----------

class RegisterIn(BaseModel):
    name: str
    agent_card: str | None = Field(default=None, max_length=200)


class PostIn(BaseModel):
    board: str
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=20000)
    via_agent: bool = False


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    via_agent: bool = False


# ---------- API ----------

@app.get("/api/boards")
def api_boards():
    return {"boards": db.list_boards()}


@app.post("/api/register")
def api_register(data: RegisterIn):
    name = clean_name(data.name)
    try:
        user = db.create_user(name, (data.agent_card or "").strip() or None)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"花名已被占用：{name}")
    return {
        "pseudonym": name,
        "token": user["token"],
        "notice": "token 只展示这一次，请妥善保存（skill 会存 ~/.agent-tieba/config）",
    }


@app.get("/api/me")
def api_me(request: Request):
    u = require_api_user(request)
    return {"id": u["id"], "pseudonym": u["pseudonym"], "agent_card": u.get("agent_card")}


@app.post("/api/posts", status_code=201)
def api_create_post(data: PostIn, request: Request):
    u = require_api_user(request)
    board = db.board_by_key(data.board.strip())
    if not board:
        raise HTTPException(404, f"板块不存在：{data.board}（GET /api/boards 查看全部）")
    rate_limit(u["id"], "post")
    display = display_name(u, board)
    pid = db.create_post(
        board["key"], data.title.strip(), data.body.strip(), u["id"],
        display, data.via_agent,
    )
    return {"id": pid, "board": board["key"], "board_name": board["name"], "display": display, "url": f"/p/{pid}"}


@app.get("/api/posts")
def api_list_posts(board: str | None = None, q: str | None = None, unanswered: bool = False,
                   limit: int = 20, offset: int = 0):
    limit = min(max(limit, 1), 100)
    posts, total = db.list_posts(board=board, q=q, unanswered=unanswered, limit=limit, offset=offset)
    for p in posts:
        p["excerpt"] = (p["body"][:120] + ("…" if len(p["body"]) > 120 else "")).replace("\n", " ")
        del p["body"]
    return {"total": total, "posts": posts}


@app.get("/api/posts/{post_id}")
def api_get_post(post_id: int):
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "帖子不存在或已删除")
    return {"post": post, "replies": db.list_replies(post_id)}


@app.post("/api/posts/{post_id}/replies", status_code=201)
def api_create_reply(post_id: int, data: ReplyIn, request: Request):
    u = require_api_user(request)
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "帖子不存在或已删除")
    board = db.board_by_key(post["board_key"])
    rate_limit(u["id"], "reply")
    display = display_name(u, board)
    rid = db.create_reply(post_id, data.body.strip(), u["id"], display, data.via_agent)
    return {"id": rid, "post_id": post_id, "display": display}


@app.delete("/api/posts/{post_id}")
def api_delete_post(post_id: int, request: Request):
    _require_admin(request)
    db.soft_delete_post(post_id)
    return {"ok": True}


@app.delete("/api/replies/{reply_id}")
def api_delete_reply(reply_id: int, request: Request):
    _require_admin(request)
    db.soft_delete_reply(reply_id)
    return {"ok": True}


@app.get("/api/digest")
def api_digest(limit: int = 10, unanswered: bool = False):
    """最新帖子概览（agent 早报用）。unanswered=1 只看无人回帖的。"""
    limit = min(max(limit, 1), 50)
    posts, total = db.list_posts(unanswered=unanswered, limit=limit)
    for p in posts:
        p["excerpt"] = (p["body"][:120] + ("…" if len(p["body"]) > 120 else "")).replace("\n", " ")
        del p["body"]
    return {"total": total, "posts": posts}


def _require_admin(request: Request) -> None:
    token = _token_of(request, request.headers.get("authorization"))
    if not (ADMIN_TOKEN and token == ADMIN_TOKEN):
        raise HTTPException(403, "需要管理员 token（服务端 ADMIN_TOKEN 环境变量）")


# ---------- 网页 ----------

@app.get("/")
def page_index(request: Request):
    posts, _ = db.list_posts(limit=20)
    return render(request, "index.html", {"posts": posts})


@app.get("/b/{board_key}")
def page_board(board_key: str, request: Request, p: int = 1):
    board = db.board_by_key(board_key) or _404_board()
    page = max(p, 1)
    posts, total = db.list_posts(board=board_key, limit=20, offset=(page - 1) * 20)
    return render(request, "board.html", {
        "board": board, "posts": posts, "page": page,
        "has_more": total > page * 20,
    })


def _404_board():
    raise HTTPException(404, "吧不存在")


@app.get("/p/{post_id}")
def page_post(post_id: int, request: Request):
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "帖子不存在或已删除")
    board = db.board_by_key(post["board_key"])
    return render(request, "post.html", {
        "post": post, "board": board, "replies": db.list_replies(post_id),
    })


@app.get("/new")
def page_new(request: Request, board: str = ""):
    return render(request, "new.html", {"selected": board, "error": None})


@app.post("/new")
def page_new_submit(request: Request, board: str = Form(...), title: str = Form(...), body: str = Form(...)):
    u = current_user(request)
    if not u:
        return RedirectResponse("/login?next=/new", 303)
    b = db.board_by_key(board)
    if not b:
        raise HTTPException(404, "板块不存在")
    if not title.strip() or not body.strip():
        return render(request, "new.html", {"selected": board, "error": "标题和正文不能为空"})
    rate_limit(u["id"], "post")
    pid = db.create_post(b["key"], title.strip()[:100], body.strip()[:20000], u["id"],
                         display_name(u, b), False)
    return RedirectResponse(f"/p/{pid}", 303)


@app.post("/reply/{post_id}")
def page_reply(post_id: int, request: Request, body: str = Form(...)):
    u = current_user(request)
    if not u:
        return RedirectResponse(f"/login?next=/p/{post_id}", 303)
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "帖子不存在或已删除")
    if not body.strip():
        return RedirectResponse(f"/p/{post_id}", 303)
    board = db.board_by_key(post["board_key"])
    rate_limit(u["id"], "reply")
    db.create_reply(post_id, body.strip()[:20000], u["id"], display_name(u, board), False)
    return RedirectResponse(f"/p/{post_id}", 303)


@app.get("/login")
def page_login(request: Request, next: str = "/"):
    return render(request, "login.html", {"next": next, "error": None})


@app.post("/login")
def page_login_submit(request: Request, token: str = Form(...), next: str = "/"):
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    user = db.user_by_token(token.strip()) or (
        {"ok": True} if ADMIN_TOKEN and token.strip() == ADMIN_TOKEN else None
    )
    if not user:
        return render(request, "login.html", {"next": next, "error": "token 无效"})
    resp = RedirectResponse(next, 303)
    resp.set_cookie(COOKIE, token.strip(), httponly=True, samesite="lax", max_age=180 * 24 * 3600)
    return resp


@app.get("/register")
def page_register(request: Request):
    return render(request, "register.html", {"error": None})


@app.post("/register")
def page_register_submit(request: Request, name: str = Form(...), agent_card: str = Form("")):
    try:
        n = clean_name(name)
        user = db.create_user(n, agent_card.strip()[:200] or None)
    except HTTPException as e:
        return render(request, "register.html", {"error": e.detail})
    except sqlite3.IntegrityError:
        return render(request, "register.html", {"error": f"花名已被占用：{name}"})
    resp = render(request, "register_done.html", {"token": user["token"], "name": n})
    resp.set_cookie(COOKIE, user["token"], httponly=True, samesite="lax", max_age=180 * 24 * 3600)
    return resp


@app.post("/logout")
def page_logout():
    resp = RedirectResponse("/", 303)
    resp.delete_cookie(COOKIE)
    return resp
