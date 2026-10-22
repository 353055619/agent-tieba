#!/usr/bin/env bash
# 冒烟测试：对运行中的服务跑全悬赏核心链路。
# 用法: tests/smoke.sh [BASE_URL] [ADMIN_TOKEN] [DB_PATH]
set -u
BASE=${1:-http://127.0.0.1:8010}
ADMIN=${2:-smoke-admin}
DB=${3:-/tmp/tieba-smoke.db}
PASS=0; FAIL=0; SKIP=0
J='Content-Type: application/json'
ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP  $1"; }
check() { # 名称 期望子串 实际
  if printf '%s' "$3" | grep -q -- "$2"; then ok "$1"; else bad "$1 —— 期望含 [$2]，实际: $(printf '%s' "$3" | head -c 200)"; fi
}
notcheck() { # 名称 不应出现的子串 实际
  if printf '%s' "$3" | grep -q -- "$2"; then bad "$1 —— 不应含 [$2]"; else ok "$1"; fi
}
get_field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)"; }
me() { curl -sS -H "Authorization: Bearer $1" "$BASE/api/me"; }

echo "== 1. 网页 =="
hall=$(curl -sS "$BASE/")
check "大厅页标题" "悬赏大厅" "$hall"
check "大厅页规则文案" "7 天无人回答自动退回" "$hall"
lb=$(curl -sS "$BASE/leaderboard")
check "排行榜页" "被采纳" "$lb"

echo "== 2. 注册 =="
tA=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟A"}' "$BASE/api/register")
check "注册A成功" '"persona"' "$tA"
check "注册送100币" '"balance":100' "$tA"
TOKA=$(printf '%s' "$tA" | get_field "['token']")
PA=$(printf '%s' "$tA" | get_field "['persona']")
tB=$(curl -sS -X POST -H "$J" -d '{}' "$BASE/api/register")
check "无名注册自动身份" '"persona"' "$tB"
TOKB=$(printf '%s' "$tB" | get_field "['token']")
PB=$(printf '%s' "$tB" | get_field "['persona']")
dup=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟A"}' "$BASE/api/register")
check "重名注册自动改花名" '"persona"' "$dup"
notcheck "重名不冒用原花名" '"pseudonym":"冒烟A"' "$dup"
tC=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟C"}' "$BASE/api/register")
TOKC=$(printf '%s' "$tC" | get_field "['token']")
A="Authorization: Bearer $TOKA"; B="Authorization: Bearer $TOKB"; C="Authorization: Bearer $TOKC"

echo "== 3. 发悬赏 =="
bad0=$(curl -sS -X POST -H "$J" -H "$A" -d '{"title":"零币","body":"x","stake":0}' "$BASE/api/posts")
check "押币下限拦截" "押币需为" "$bad0"
bad51=$(curl -sS -X POST -H "$J" -H "$A" -d '{"title":"超上限","body":"x","stake":51}' "$BASE/api/posts")
check "押币上限拦截" "押币需为" "$bad51"
p1=$(curl -sS -X POST -H "$J" -H "$A" -d '{"title":"CLI 启动慢如蜗牛","body":"冷启动要 8 秒，`--fast` 也不管用\n\n```bash\ntime codeagent\n```","stake":30,"via_agent":true}' "$BASE/api/posts")
PID=$(printf '%s' "$p1" | get_field "['id']")
check "发悬赏成功" '"status":"open"' "$p1"
check "A余额扣至70" '"balance":70' "$(me $TOKA)"
c50=$(curl -sS -X POST -H "$J" -H "$C" -d '{"title":"C的大单","body":"x","stake":50}' "$BASE/api/posts")
check "C押50成功" '"id"' "$c50"
c50b=$(curl -sS -X POST -H "$J" -H "$C" -d '{"title":"C的第二次大单","body":"x","stake":50}' "$BASE/api/posts")
check "C再押50成功(清零)" '"id"' "$c50b"
check "清零后下次请求自动周回填到50" '"balance":50' "$(me $TOKC)"
c50c=$(curl -sS -X POST -H "$J" -H "$C" -d '{"title":"C的第三次大单","body":"x","stake":50}' "$BASE/api/posts")
check "C押掉回填款(再清零)" '"id"' "$c50c"
cbroke=$(curl -sS -X POST -H "$J" -H "$C" -d '{"title":"C没钱了","body":"x","stake":1}' "$BASE/api/posts")
check "本周已回填，余额不足拦截" "余额不足" "$cbroke"

echo "== 4. 回答 =="
sup=$(curl -sS -X POST -H "$J" -H "$A" -d '{"body":"补充：版本 3.2.1，macOS 环境"}' "$BASE/api/posts/$PID/replies")
SUP=$(printf '%s' "$sup" | get_field "['id']")
check "楼主可补充说明" '"is_asker_note":true' "$sup"
r1=$(curl -sS -X POST -H "$J" -H "$B" -d '{"body":"试试 `--no-plugin`，我这边 3 秒\n\n```bash\ncodeagent --no-plugin\n```","via_agent":true}' "$BASE/api/posts/$PID/replies")
RID=$(printf '%s' "$r1" | get_field "['id']")
check "B回答成功" '"id"' "$r1"
check "B回答带匿名身份" "\"display\":\"${PB}\"" "$r1"
check "B回答不是楼主" '"is_asker_note":false' "$r1"

echo "== 5. 采纳 =="
notasker=$(curl -sS -X POST -H "$J" -H "$B" -d "{\"reply_id\":$RID}" "$BASE/api/posts/$PID/adopt")
check "非提问者采纳被拒" "只有提问者" "$notasker"
badreply=$(curl -sS -X POST -H "$J" -H "$A" -d '{"reply_id":99999}' "$BASE/api/posts/$PID/adopt")
check "采纳不存在的回答被拒" "回答不存在" "$badreply"
ownnote=$(curl -sS -X POST -H "$J" -H "$A" -d "{\"reply_id\":$SUP}" "$BASE/api/posts/$PID/adopt")
check "不能采纳楼主补充" "不能采纳楼主" "$ownnote"
adopt=$(curl -sS -X POST -H "$J" -H "$A" -d "{\"reply_id\":$RID}" "$BASE/api/posts/$PID/adopt")
check "提问者采纳成功" '"answerer_gets":35' "$adopt"
readopt=$(curl -sS -X POST -H "$J" -H "$A" -d "{\"reply_id\":$RID}" "$BASE/api/posts/$PID/adopt")
check "重复采纳被拒(409)" "已结束" "$readopt"
late=$(curl -sS -X POST -H "$J" -H "$B" -d '{"body":"晚了"}' "$BASE/api/posts/$PID/replies")
check "已采纳后不能回答" "已结束" "$late"
check "B进账135币" '"balance":135' "$(me $TOKB)"
check "B被采纳1次" '"adopted_count":1' "$(me $TOKB)"
board=$(curl -sS "$BASE/api/leaderboard")
check "排行榜B登顶" "\"persona\":\"${PB}\"" "$board"
solved=$(curl -sS "$BASE/solved")
check "沉淀库含该问题" "CLI 启动慢如蜗牛" "$solved"
postweb=$(curl -sS "$BASE/p/$PID")
check "帖子页已解决徽章" "已解决" "$postweb"
check "采纳答案高亮" "已采纳 · +35 币" "$postweb"
check "agent 起草徽章" "agent 起草" "$postweb"
check "markdown 代码块渲染" "<pre>" "$postweb"

echo "== 6. XSS =="
xss=$(curl -sS -X POST -H "$J" -H "$B" -d '{"title":"xss 测试","body":"<script>alert(1)</script> 直出测试\n\n```js\nvar a=1\n```","stake":1}' "$BASE/api/posts")
XID=$(printf '%s' "$xss" | get_field "['id']")
webxss=$(curl -sS "$BASE/p/$XID")
notcheck "XSS 被转义" "<script>alert" "$webxss"
check "XSS 帖正文可见" "直出测试" "$webxss"

echo "== 7. 7天无人回答退款 =="
if command -v sqlite3 >/dev/null && [ -f "$DB" ]; then
  p7=$(curl -sS -X POST -H "$J" -H "$A" -d '{"title":"没人答的老单","body":"x","stake":10}' "$BASE/api/posts")
  PID7=$(printf '%s' "$p7" | get_field "['id']")
  check "老单押币后A余额60" '"balance":60' "$(me $TOKA)"
  old=$(date -v-8d +%Y-%m-%dT%H:%M:%S)
  sqlite3 "$DB" "UPDATE posts SET created_at='$old' WHERE id=$PID7"
  curl -sS "$BASE/api/posts?status=open" >/dev/null
  ref=$(curl -sS "$BASE/api/posts/$PID7")
  check "自动退款状态" '"status":"refunded"' "$ref"
  check "退款回到A余额70" '"balance":70' "$(me $TOKA)"
else
  skip "退款扫描（需要本机 sqlite3 与 DB 路径）"
fi

echo "== 8. 周回填 =="
if command -v sqlite3 >/dev/null && [ -f "$DB" ]; then
  CID=$(sqlite3 "$DB" "SELECT id FROM users WHERE pseudonym='冒烟C'")
  sqlite3 "$DB" "UPDATE users SET balance=5, last_refill_week='2020-W01' WHERE id=$CID"
  check "低余额周回填到50" '"balance":50' "$(me $TOKC)"
else
  skip "周回填（需要本机 sqlite3 与 DB 路径）"
fi

echo "== 9. 管理员代裁 =="
p3=$(curl -sS -X POST -H "$J" -H "$A" -d '{"title":"失联单","body":"x","stake":5}' "$BASE/api/posts")
P3=$(printf '%s' "$p3" | get_field "['id']")
curl -sS -X POST -H "$J" -H "$B" -d '{"body":"我答了"}' "$BASE/api/posts/$P3/replies" >/dev/null
fr=$(curl -sS -X POST -H "Authorization: Bearer $ADMIN" "$BASE/api/admin/refund/$P3")
check "有回答仍可代裁退款" '"status":"refunded"' "$fr"
check "代裁退款回到A" '"balance":70' "$(me $TOKA)"
p4=$(curl -sS -X POST -H "$J" -H "$A" -d '{"title":"失联单2","body":"x","stake":5}' "$BASE/api/posts")
P4=$(printf '%s' "$p4" | get_field "['id']")
r4=$(curl -sS -X POST -H "$J" -H "$B" -d '{"body":"最佳答案"}' "$BASE/api/posts/$P4/replies")
RID4=$(printf '%s' "$r4" | get_field "['id']")
fa=$(curl -sS -X POST -H "Authorization: Bearer $ADMIN" "$BASE/api/admin/adopt/$P4/$RID4")
check "代裁采纳" '"answerer_gets":10' "$fa"
check "B累计144币2采纳" '"balance":144' "$(me $TOKB)"
del=$(curl -sS -X DELETE -H "Authorization: Bearer $ADMIN" "$BASE/api/posts/$XID")
check "管理员删帖" '"ok":true' "$del"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/posts/$XID")
if [ "$code" = "404" ]; then ok "删帖后404"; else bad "删帖后仍 ${code}"; fi
nodel=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE -H "$C" "$BASE/api/posts/$PID")
if [ "$nodel" = "403" ]; then ok "普通用户删帖被拒"; else bad "普通用户可删帖：${nodel}"; fi
noaudit=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/admin/audit")
if [ "$noaudit" = "403" ]; then ok "非管理员审计被拒"; else bad "非管理员可看审计：${noaudit}"; fi
audit=$(curl -sS -H "Authorization: Bearer $ADMIN" "$BASE/api/admin/audit")
check "管理员审计总览含花名" "冒烟A" "$audit"
check "审计含匿名身份映射" '"persona"' "$audit"
check "审计含token(找回用)" '"token"' "$audit"

echo "== 10. 限流 =="
tD=$(curl -sS -X POST -H "$J" -d '{"name":"冒烟D"}' "$BASE/api/register")
TOKD=$(printf '%s' "$tD" | get_field "['token']")
codes=""
for i in $(seq 1 12); do
  codes="$codes "$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$J" -H "Authorization: Bearer $TOKD" \
    -d "{\"title\":\"灌水${i}\",\"body\":\"x\",\"stake\":1}" "$BASE/api/posts")
done
n429=$(printf '%s' "$codes" | tr ' ' '\n' | grep -c 429 || true)
if [ "$n429" -ge 1 ]; then ok "限流生效（12 发 429×${n429}）"; else bad "限流未生效：$codes"; fi

echo "== 11. skill 脚本 =="
S=$(cd "$(dirname "$0")/../skill/scripts" && pwd)
export TIEBA_CONF=/tmp/tieba-smoke-conf
rm -f "$TIEBA_CONF"
reg=$(bash "$S/tieba.sh" register 脚本用户 "$BASE" 2>&1)
check "skill register" "注册成功" "$reg"
sme=$(bash "$S/tieba.sh" me 2>&1)
check "skill me 余额100" '"balance":100' "$sme"
sb=$(bash "$S/tieba.sh" bounty "脚本测试单：怎么清理缓存" "skill 发单正文" 3 2>&1)
check "skill bounty" '"status":"open"' "$sb"
sme2=$(bash "$S/tieba.sh" me 2>&1)
check "skill 发单后余额97" '"balance":97' "$sme2"
sdg=$(bash "$S/tieba.sh" digest 3 2>&1)
check "skill digest" "posts" "$sdg"
slb=$(bash "$S/tieba.sh" leaderboard 2>&1)
check "skill leaderboard" "persona" "$slb"
ss=$(bash "$S/tieba.sh" search 蜗牛 2>&1)
check "skill search" "慢如蜗牛" "$ss"
rm -f "$TIEBA_CONF"

echo
echo "结果：PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
[ "$FAIL" -eq 0 ]
