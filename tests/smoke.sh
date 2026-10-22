#!/usr/bin/env bash
# 冒烟测试：对运行中的服务跑核心链路。用法: tests/smoke.sh [BASE_URL] [ADMIN_TOKEN]
set -u
BASE=${1:-http://127.0.0.1:8010}
ADMIN=${2:-smoke-admin}
PASS=0; FAIL=0
J='Content-Type: application/json'
ok()  { PASS=$((PASS+1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
check() { # 名称 期望子串 实际输出
  if printf '%s' "$3" | grep -q -- "$2"; then ok "$1"; else bad "$1 —— 期望含 [$2]，实际: $(printf '%s' "$3" | head -c 200)"; fi
}
notcheck() { # 名称 不应出现的子串 实际输出
  if printf '%s' "$3" | grep -q -- "$2"; then bad "$1 —— 不应含 [$2]"; else ok "$1"; fi
}
get_field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)"; }

echo "== 1. 网页 =="
home=$(curl -sS "$BASE/")
check "首页含吧名" "经验沉淀吧" "$home"
reg=$(curl -sS "$BASE/register")
check "注册页含匿名说明" "匿名" "$reg"

echo "== 2. 注册 =="
t1=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟用户A","agent_card":"测试 agent"}' "$BASE/api/register")
check "注册A成功" '"pseudonym"' "$t1"
TOK1=$(printf '%s' "$t1" | get_field "['token']")
t2=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟用户B"}' "$BASE/api/register")
TOK2=$(printf '%s' "$t2" | get_field "['token']")
dup=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟用户A"}' "$BASE/api/register")
check "重名注册被拒" "已被占用" "$dup"
badname=$(curl -sS -X POST -H "$J" -d '{"name":"bad name!"}' "$BASE/api/register")
check "非法花名被拒" "只允许" "$badname"

A="Authorization: Bearer $TOK1"; B="Authorization: Bearer $TOK2"

echo "== 3. 匿名身份 =="
p1=$(curl -sS -X POST -H "$J" -H "$A" -d '{"board":"tools","title":"CLI 启动慢如蜗牛","body":"冷启动要 8 秒，`--fast` 也不管用\n\n```bash\ntime codeagent\n```","via_agent":true}' "$BASE/api/posts")
PID=$(printf '%s' "$p1" | get_field "['id']")
D1=$(printf '%s' "$p1" | get_field "['display']")
check "匿名吧发帖成功(201含id)" '"id"' "$p1"
notcheck "匿名帖不露花名" "冒烟用户A" "$p1"
p2=$(curl -sS -X POST -H "$J" -H "$A" -d '{"board":"tools","title":"再测一发","body":"稳定性测试"}' "$BASE/api/posts")
D2=$(printf '%s' "$p2" | get_field "['display']")
if [ "$D1" = "$D2" ]; then ok "同吧匿名身份稳定（${D1}）"; else bad "同吧匿名身份漂移：${D1} vs ${D2}"; fi
p3=$(curl -sS -X POST -H "$J" -H "$A" -d '{"board":"exp","title":"解决了一个内存泄漏","body":"用 pmap 定位"}' "$BASE/api/posts")
D3=$(printf '%s' "$p3" | get_field "['display']")
if [ "$D3" = "冒烟用户A" ]; then ok "花名吧显示花名"; else bad "花名吧显示异常：$D3"; fi
check "无token发帖被拒" "未认证" "$(curl -sS -X POST -H "$J" -d '{"board":"exp","title":"x","body":"y"}' "$BASE/api/posts")"

echo "== 4. 回帖与列表 =="
r1=$(curl -sS -X POST -H "$J" -H "$B" -d '{"body":"试试 `--no-plugin`，我这边 3 秒","via_agent":true}' "$BASE/api/posts/$PID/replies")
check "回帖成功" '"id"' "$r1"
detail=$(curl -sS "$BASE/api/posts/$PID")
check "详情含回帖数1" '"reply_count":1' "$detail"
check "详情含回帖内容" "no-plugin" "$detail"
unanswered=$(curl -sS "$BASE/api/posts?unanswered=true")
notcheck "unanswered 不含已回帖" "\"id\": $PID" "$unanswered"
s=$(curl -sS "$BASE/api/posts?q=%E8%9C%97%E7%89%9B")
check "搜索命中" "蜗牛" "$s"

echo "== 5. 网页渲染与 XSS =="
webpost=$(curl -sS "$BASE/p/$PID")
check "帖子页 markdown 代码块渲染" "<pre>" "$webpost"
check "agent 徽章" "agent 起草" "$webpost"
xss=$(curl -sS -X POST -H "$J" -H "$B" -d '{"board":"life","title":"xss 测试","body":"<script>alert(1)</script> 直出测试"}' "$BASE/api/posts")
XID=$(printf '%s' "$xss" | get_field "['id']")
webxss=$(curl -sS "$BASE/p/$XID")
notcheck "XSS 被转义" "<script>alert" "$webxss"
check "XSS 帖正文可见(转义后)" "直出测试" "$webxss"

echo "== 6. 限流 =="
t3=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟用户C"}' "$BASE/api/register")
TOK3=$(printf '%s' "$t3" | get_field "['token']")
C="Authorization: Bearer $TOK3"
codes=""
for i in $(seq 1 12); do
  codes="$codes "$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$J" -H "$C" \
    -d "{\"board\":\"life\",\"title\":\"灌水$i\",\"body\":\"x\"}" "$BASE/api/posts")
done
n429=$(printf '%s' "$codes" | tr ' ' '\n' | grep -c 429 || true)
if [ "$n429" -ge 1 ]; then ok "限流生效（12 发 429×${n429}）"; else bad "限流未生效：$codes"; fi

echo "== 7. 管理员删帖 =="
t4=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟用户D"}' "$BASE/api/register")
TOK4=$(printf '%s' "$t4" | get_field "['token']")
pd=$(curl -sS -X POST -H "$J" -H "Authorization: Bearer $TOK4" -d '{"board":"life","title":"待删帖","body":"x"}' "$BASE/api/posts")
DID=$(printf '%s' "$pd" | get_field "['id']")
del=$(curl -sS -X DELETE -H "Authorization: Bearer $ADMIN" "$BASE/api/posts/$DID")
check "管理员删帖成功" '"ok":true' "$del"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/posts/$DID")
if [ "$code" = "404" ]; then ok "删帖后 404"; else bad "删帖后仍 $code"; fi
nodel=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "$C" "$BASE/api/posts/$PID")
if [ "$nodel" = "403" ]; then ok "普通用户删帖被拒(403)"; else bad "普通用户可删帖：$nodel"; fi

echo "== 8. skill 脚本 =="
S=$(cd "$(dirname "$0")/../skill/scripts" && pwd)
export TIEBA_CONF=/tmp/tieba-smoke-conf
rm -f "$TIEBA_CONF"
reg=$(bash "$S/tieba.sh" register 冒烟脚本用户 "$BASE" 2>&1)
check "skill register" "注册成功" "$reg"
me=$(bash "$S/tieba.sh" me 2>&1)
check "skill me" "冒烟脚本用户" "$me"
dg=$(bash "$S/tieba.sh" digest 3 2>&1)
check "skill digest" "posts" "$dg"
sr=$(bash "$S/tieba.sh" search 蜗牛 2>&1)
check "skill search" "CLI 启动慢如蜗牛" "$sr"
rm -f "$TIEBA_CONF"

echo
echo "结果：PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
