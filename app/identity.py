"""匿名展示名生成：同一用户在同一吧内花名稳定，跨吧不可关联。"""
import hashlib

_ADJ = [
    "深夜修bug的", "刚下班的", "摸鱼中的", "喝第三杯咖啡的", "改需求的",
    "在会议室装睡的", "信奉重启大法的", "等CI跑完的", "值班待命的", "背着KPI的",
]
_NOUN = [
    "橘猫", "哈士奇", "机器猫", "扫地机器人", "小恐龙",
    "机械键盘", "保温杯", "工牌", "服务器", "行军床",
]


def anon_name(user_id: int, board_key: str) -> str:
    h = hashlib.sha256(f"{user_id}:{board_key}".encode()).digest()
    return _ADJ[h[0] % len(_ADJ)] + _NOUN[h[1] % len(_NOUN)]
