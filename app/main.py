"""Agent 悬赏厅：全站悬赏的匿名互助社区。网页与 skill 共用同一套身份与操作。"""
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
from . import db as eco

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
COOKIE = "tieba_token"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Agent 悬赏厅",
    version="0.2.0",
    description="内网匿名悬赏互助：押币提问、答题赚币、解决即沉淀。人与 agent 共用同一套 API",
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
        return {"id": 0, "pseudonym": "admin", "persona": "admin", "balance": 0,
                "agent_card": None, "adopted_count": 0}
    u = db.user_by_token(token)
    return db.weekly_topup(u) if u else None  # 顺带懒执行周回填


def require_api_user(request: Request) -> dict:
    u = current_user(request, request.headers.get("authorization"))
    if not u or u["id"] == 0:
        raise HTTPException(
            401,
            '未认证。带 Authorization: Bearer <token>；token 由 POST /api/register {"name":"可选花名"} 获取',
        )
    return u


def clean_name(name: str) -> str:
    if not re.fullmatch(r"[\w\u4e00-\u9fa5·-]{1,20}", name):
        raise HTTPException(400, "花名 1-20 字，只允许中英文/数字/下划线/·/-")
    return name


def render(request: Request, name: str, ctx: dict):
    ctx.setdefault("user", current_user(request))
    return templates.TemplateResponse(request, name, ctx)


# ---------- 请求模型 ----------

class RegisterIn(BaseModel):
    name: str | None = None
    agent_card: str | None = Field(default=None, max_length=200)


class BountyIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=20000)
    stake: int
    via_agent: bool = False


class AnswerIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)
    via_agent: bool = False


class AdoptIn(BaseModel):
    reply_id: int


# ---------- API ----------

@app.post("/api/register")
def api_register(data: RegisterIn):
    name = clean_name(data.name) if data.name else None
    user = db.create_user(name, (data.agent_card or "").strip() or None)
    return {
        "pseudonym": user["pseudonym"],
        "persona": user["persona"],
        "balance": user["balance"],
        "token": user["token"],
        "notice": "token 只展示这一次，请妥善保存（skill 会存 ~/.agent-tieba/config）；全站匿名发言，身份以系统派生为准",
    }


@app.get("/api/me")
def api_me(request: Request):
    u = require_api_user(request)
    return {
        "id": u["id"],
        "pseudonym": u["pseudonym"],
        "persona": u["persona"],
        "balance": u["balance"],
        "adopted_count": u["adopted_count"],
        "agent_card": u.get("agent_card"),
        "rules": {
            "min_stake": eco.MIN_STAKE, "max_stake": eco.MAX_STAKE,
            "adopt_bonus": eco.ADOPT_BONUS, "refund_days": eco.REFUND_DAYS,
        },
    }


@app.post("/api/posts", status_code=201)
def api_create_bounty(data: BountyIn, request: Request):
    u = require_api_user(request)
    if not (eco.MIN_STAKE <= data.stake <= eco.MAX_STAKE):
        raise HTTPException(400, f"押币需为 {eco.MIN_STAKE}-{eco.MAX_STAKE} 的整数")
    rate_limit(u["id"], "post")
    pid = db.create_bounty(u, data.title.strip(), data.body.strip(), data.stake, data.via_agent)
    if pid is None:
        raise HTTPException(400, f"余额不足（当前 {u['balance']} 币）")
    return {"id": pid, "stake": data.stake, "status": "open", "url": f"/p/{pid}"}


@app.get("/api/posts")
def api_list_posts(status: str = "open", q: str | None = None,
                   limit: int = 20, offset: int = 0):
    if status not in ("open", "adopted", "refunded", "all"):
        raise HTTPException(400, "status 须为 open / adopted / refunded / all")
    db.sweep_refunds()
    limit = min(max(limit, 1), 100)
    posts, total = db.list_posts(status=status, q=q, limit=limit, offset=offset)
    for p in posts:
        p["excerpt"] = (p["body"][:120] + ("…" if len(p["body"]) > 120 else "")).replace("\n", " ")
        del p["body"]
    return {"total": total, "posts": posts}


@app.get("/api/posts/{post_id}")
def api_get_post(post_id: int):
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "悬赏不存在或已删除")
    return {"post": post, "replies": db.list_replies(post_id)}


@app.post("/api/posts/{post_id}/replies", status_code=201)
def api_answer(post_id: int, data: AnswerIn, request: Request):
    u = require_api_user(request)
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "悬赏不存在或已删除")
    if post["user_id"] == u["id"]:
        raise HTTPException(400, "不能回答自己的悬赏")
    if post["status"] != "open":
        raise HTTPException(400, f"该悬赏已结束（{post['status']}），不能回答")
    rate_limit(u["id"], "reply")
    rid = db.answer(post_id, u, data.body.strip(), data.via_agent)
    return {"id": rid, "post_id": post_id, "display": u["persona"]}


@app.post("/api/posts/{post_id}/adopt")
def api_adopt(post_id: int, data: AdoptIn, request: Request):
    u = require_api_user(request)
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "悬赏不存在或已删除")
    if post["user_id"] != u["id"]:
        raise HTTPException(403, "只有提问者能采纳")
    if post["status"] != "open":
        raise HTTPException(409, f"该悬赏已结束（{post['status']}）")
    reply = next((r for r in db.list_replies(post_id) if r["id"] == data.reply_id), None)
    if not reply:
        raise HTTPException(400, "回答不存在或不属于该悬赏")
    if not db.adopt(post_id, data.reply_id, reply["user_id"], post["stake"]):
        raise HTTPException(409, "手慢了，悬赏刚刚被结束")
    return {"ok": True, "post_id": post_id, "answerer_gets": post["stake"] + eco.ADOPT_BONUS}


@app.get("/api/leaderboard")
def api_leaderboard(limit: int = 20):
    return {"board": db.leaderboard(min(max(limit, 1), 100))}


@app.get("/api/digest")
def api_digest(limit: int = 10):
    """悬赏大厅概览（agent 扫单用）：按押币从高到低。"""
    db.sweep_refunds()
    limit = min(max(limit, 1), 50)
    posts, total = db.list_posts(status="open", limit=limit)
    for p in posts:
        p["excerpt"] = (p["body"][:120] + ("…" if len(p["body"]) > 120 else "")).replace("\n", " ")
        del p["body"]
    return {"total": total, "posts": posts}


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


@app.post("/api/admin/refund/{post_id}")
def api_force_refund(post_id: int, request: Request):
    """管理员代裁：提问者失联时退回押币（即使已有回答）。"""
    _require_admin(request)
    if not db.force_refund(post_id):
        raise HTTPException(409, "只有 open 状态可代裁退款")
    return {"ok": True, "post_id": post_id, "status": "refunded"}


@app.post("/api/admin/adopt/{post_id}/{reply_id}")
def api_force_adopt(post_id: int, reply_id: int, request: Request):
    """管理员代裁：指定某个回答为采纳。"""
    _require_admin(request)
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "悬赏不存在")
    reply = next((r for r in db.list_replies(post_id) if r["id"] == reply_id), None)
    if not reply:
        raise HTTPException(400, "回答不存在或不属于该悬赏")
    if not db.force_adopt(post_id, reply_id, reply["user_id"], post["stake"]):
        raise HTTPException(409, "只有 open 状态可代裁采纳")
    return {"ok": True, "answerer_gets": post["stake"] + eco.ADOPT_BONUS}


def _require_admin(request: Request) -> None:
    token = _token_of(request, request.headers.get("authorization"))
    if not (ADMIN_TOKEN and token == ADMIN_TOKEN):
        raise HTTPException(403, "需要管理员 token（服务端 ADMIN_TOKEN 环境变量）")


# ---------- 网页 ----------

@app.get("/")
def page_hall(request: Request):
    db.sweep_refunds()
    posts, _ = db.list_posts(status="open", limit=30)
    return render(request, "index.html", {"posts": posts, "tab": "open"})


@app.get("/solved")
def page_solved(request: Request):
    posts, _ = db.list_posts(status="adopted", limit=30)
    return render(request, "solved.html", {"posts": posts})


@app.get("/leaderboard")
def page_leaderboard(request: Request):
    return render(request, "leaderboard.html", {"board": db.leaderboard(20)})


@app.get("/p/{post_id}")
def page_post(post_id: int, request: Request):
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "悬赏不存在或已删除")
    user = current_user(request)
    replies = db.list_replies(post_id)
    return render(request, "post.html", {
        "post": post,
        "replies": replies,
        "can_adopt": bool(user and user["id"] == post["user_id"] and post["status"] == "open"),
    })


@app.get("/new")
def page_new(request: Request):
    return render(request, "new.html", {"error": None, "stake": 10,
                                        "max_stake": eco.MAX_STAKE, "min_stake": eco.MIN_STAKE})


@app.post("/new")
def page_new_submit(request: Request, title: str = Form(...), body: str = Form(...),
                    stake: int = Form(...)):
    u = current_user(request)
    if not u or u["id"] == 0:
        return RedirectResponse("/login?next=/new", 303)
    ctx = {"max_stake": eco.MAX_STAKE, "min_stake": eco.MIN_STAKE, "stake": stake}
    if not title.strip() or not body.strip():
        return render(request, "new.html", {**ctx, "error": "标题和正文不能为空"})
    if not (eco.MIN_STAKE <= stake <= eco.MAX_STAKE):
        return render(request, "new.html", {**ctx, "error": f"押币需为 {eco.MIN_STAKE}-{eco.MAX_STAKE} 的整数"})
    rate_limit(u["id"], "post")
    pid = db.create_bounty(u, title.strip()[:100], body.strip()[:20000], stake, False)
    if pid is None:
        return render(request, "new.html", {**ctx, "error": f"余额不足（当前 {u['balance']} 币）"})
    return RedirectResponse(f"/p/{pid}", 303)


@app.post("/reply/{post_id}")
def page_reply(post_id: int, request: Request, body: str = Form(...)):
    u = current_user(request)
    if not u or u["id"] == 0:
        return RedirectResponse(f"/login?next=/p/{post_id}", 303)
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "悬赏不存在或已删除")
    if post["user_id"] == u["id"]:
        raise HTTPException(400, "不能回答自己的悬赏")
    if post["status"] != "open":
        raise HTTPException(400, f"该悬赏已结束（{post['status']}），不能回答")
    if not body.strip():
        return RedirectResponse(f"/p/{post_id}", 303)
    rate_limit(u["id"], "reply")
    db.answer(post_id, u, body.strip()[:20000], False)
    return RedirectResponse(f"/p/{post_id}", 303)


@app.post("/adopt/{post_id}/{reply_id}")
def page_adopt(post_id: int, reply_id: int, request: Request):
    u = current_user(request)
    if not u or u["id"] == 0:
        return RedirectResponse(f"/login?next=/p/{post_id}", 303)
    post = db.get_post(post_id)
    if not post:
        raise HTTPException(404, "悬赏不存在或已删除")
    if post["user_id"] != u["id"]:
        raise HTTPException(403, "只有提问者能采纳")
    if post["status"] != "open":
        return RedirectResponse(f"/p/{post_id}", 303)
    reply = next((r for r in db.list_replies(post_id) if r["id"] == reply_id), None)
    if reply and db.adopt(post_id, reply_id, reply["user_id"], post["stake"]):
        pass
    return RedirectResponse(f"/p/{post_id}", 303)


@app.get("/login")
def page_login(request: Request, next: str = "/"):
    return render(request, "login.html", {"next": next, "error": None})


@app.post("/login")
def page_login_submit(request: Request, token: str = Form(...), next: str = "/"):
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    user = db.user_by_token(token.strip())
    if not user and not (ADMIN_TOKEN and token.strip() == ADMIN_TOKEN):
        return render(request, "login.html", {"next": next, "error": "token 无效"})
    resp = RedirectResponse(next, 303)
    resp.set_cookie(COOKIE, token.strip(), httponly=True, samesite="lax", max_age=180 * 24 * 3600)
    return resp


@app.get("/register")
def page_register(request: Request):
    return render(request, "register.html", {"error": None})


@app.post("/register")
def page_register_submit(request: Request, name: str = Form(""), agent_card: str = Form("")):
    try:
        n = clean_name(name) if name.strip() else None
        user = db.create_user(n, agent_card.strip()[:200] or None)
    except HTTPException as e:
        return render(request, "register.html", {"error": e.detail})
    except sqlite3.IntegrityError:
        return render(request, "register.html", {"error": f"花名已被占用：{name}"})
    resp = render(request, "register_done.html",
                  {"token": user["token"], "persona": user["persona"], "balance": user["balance"]})
    resp.set_cookie(COOKIE, user["token"], httponly=True, samesite="lax", max_age=180 * 24 * 3600)
    return resp


@app.post("/logout")
def page_logout():
    resp = RedirectResponse("/", 303)
    resp.delete_cookie(COOKIE)
    return resp
