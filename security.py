"""安全层（1 号负责）：注入检测、权限分级、确认令牌、数值溯源。

这一层独立于模型，任何模型输出的动作都必须过这里 —— 这是"防幻觉"的第一道闸门。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# 权限分级
# --------------------------------------------------------------------------
LEVEL_QUERY = "query"
LEVEL_CHANGE = "change"
LEVEL_HIGH = "high"

SINGLE_LIMIT_HIGH = 5000.00      # 单笔高风险阈值
DAILY_LIMIT_HIGH = 20000.00      # 单日累计高风险阈值


@dataclass
class ActionMeta:
    name: str
    level: str
    label: str
    description: str


def level_of(tool: str, args: dict, daily_out: float = 0.0) -> str:
    """返回该次调用的实际权限级别（冲突时取更严）。"""
    if tool == "transfer.create":
        amt = float(args.get("amount") or 0)
        if args.get("payee_first_time"):
            return LEVEL_HIGH
        if amt >= SINGLE_LIMIT_HIGH or daily_out + amt >= DAILY_LIMIT_HIGH:
            return LEVEL_HIGH
        if args.get("currency") and args["currency"] != "CNY":
            return LEVEL_HIGH
        return LEVEL_CHANGE
    if tool in ("card.report_loss", "card.limit_adjust"):
        return LEVEL_HIGH
    if tool in ("transfer.cancel", "subscription.cancel", "wealth.purchase",
                "wealth.redeem", "card.lock", "card.unlock", "card.apply", "transfer.schedule"):
        return LEVEL_CHANGE
    return LEVEL_QUERY


# --------------------------------------------------------------------------
# 提示注入检测
# --------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"忽略(之前|以上|前面|所有)的?(指令|规则|要求)",
    r"(打印|输出|显示|告诉我).{0,6}(系统提示|提示词|prompt|配置|规则)",
    r"(进入|开启).{0,4}(开发者|调试|管理员)模式",
    r"(你现在是|假装你是|从现在起你是).{0,6}(管理员|超级用户|开发者|root)",
    r"我(就)?是(银行)?(管理员|风控|客服|工作人员)",
    r"(跳过|绕过|免去).{0,4}(验证|风控|密码|确认)",
    r"ignore (all )?(previous|above) (instructions|prompts)",
    r"system\s*[:：]",
    r"(帮我|替我).{0,4}(查|看|操作).{0,6}(老婆|老公|爸爸|妈妈|父母|朋友|别人|他人)的",
    r"(查|看|调|操作).{0,6}(老婆|老公|爸爸|妈妈|父母|朋友|别人|他人)的",
    r"用(他|她|别人)的(卡|账户)",
    r"(随便|大概|估算).{0,6}(给|说|按).{0,4}(收益率|利息|数字)",
    r"(先按|就按).{0,6}%?\s*算",
]

_INJ = [re.compile(p, re.I) for p in INJECTION_PATTERNS]


@dataclass
class ScanResult:
    blocked: bool
    category: str = ""
    matched: str = ""
    reply: str = ""


REFUSALS = {
    "prompt": "这个我不能提供。不过你的账户问题我可以继续帮你处理，需要办什么？",
    "privilege": "查别人账户或动用别人的卡，我做不了，这属于账户安全红线。你自己的账户我可以随时帮你查。",
    "fabricate": "收益率和金额我不能估一个给你，只能报产品和账户里真实的数据。要看看当前在售的理财产品吗？",
}


def scan(text: str) -> ScanResult:
    """扫用户输入。命中即拒绝，并给出合规替代路径。"""
    for rx in _INJ:
        m = rx.search(text)
        if not m:
            continue
        frag = m.group(0)
        if re.search(r"(老婆|老公|爸爸|妈妈|父母|朋友|别人|他人|他的|她的|用.{0,2}的卡)", frag):
            return ScanResult(True, "privilege", frag, REFUSALS["privilege"])
        if re.search(r"(收益率|利息|数字|按.{0,3}算)", frag):
            return ScanResult(True, "fabricate", frag, REFUSALS["fabricate"])
        return ScanResult(True, "prompt", frag, REFUSALS["prompt"])
    return ScanResult(False)


# 外部内容定界符：账单备注 / 商户名里藏指令时，只当文本
def wrap_external(text: str) -> str:
    if not text:
        return text
    return f"【外部内容】{text}【/外部内容】"


def has_hidden_instruction(text: str) -> bool:
    return bool(text) and bool(re.search(r"(system\s*[:：]|忽略风控|跳过验证|ignore)", text, re.I))


# --------------------------------------------------------------------------
# 确认令牌（v2 状态机）
# --------------------------------------------------------------------------
CONFIRM_TTL = 120  # 秒


@dataclass
class ConfirmTicket:
    token: str
    fingerprint: str
    expires_at: float
    action: str
    summary: str
    payload: dict = field(default_factory=dict)

    def valid(self, fp: str) -> tuple[bool, str]:
        if time.time() > self.expires_at:
            return False, "expired"
        if fp != self.fingerprint:
            return False, "changed"
        return True, ""


def fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def new_token() -> str:
    import uuid
    return "ct_" + uuid.uuid4().hex[:8]


def make_ticket(action: str, summary: str, payload: dict) -> ConfirmTicket:
    return ConfirmTicket(
        token=new_token(),
        fingerprint=fingerprint(payload),
        expires_at=time.time() + CONFIRM_TTL,
        action=action,
        summary=summary,
        payload=payload,
    )


# --------------------------------------------------------------------------
# 数值溯源：每个报给用户的数字都要能指回来源
# --------------------------------------------------------------------------
class Ledger:
    """记录本轮所有工具返回值，供前端/答辩展示"数字从哪来"。"""

    def __init__(self) -> None:
        self.sources: list[dict[str, Any]] = []

    def record(self, tool: str, trace_id: str, data: Any) -> None:
        self.sources.append({"tool": tool, "trace_id": trace_id, "data": data})

    def clear(self) -> None:
        self.sources.clear()
