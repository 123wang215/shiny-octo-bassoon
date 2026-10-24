"""自然语言解析（1 号负责）：金额归一化、时间归一化、收款人消歧、意图分类。

刻意做成确定性规则 + 可选 LLM 增强：模型挂了也能演示，答辩时不会翻车。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}

AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|千|块|元|块钱|rmb|RMB)?")


def cn2num(s: str) -> float | None:
    """中文数字转阿拉伯数字，支持"五十""五百""五千""一万二"。"""
    if not s:
        return None
    total = 0.0
    section = 0.0
    num = 0.0
    saw_wan = False
    for ch in s:
        if ch in CN_DIGIT:
            num = CN_DIGIT[ch]
        elif ch in CN_UNIT:
            u = CN_UNIT[ch]
            if u == 10000:
                section = (section + num) * 10000
                total += section
                section = 0
                num = 0
                saw_wan = True
            else:
                if num == 0:
                    num = 1
                section += num * u
                num = 0
        else:
            return None
    if saw_wan and num and num < 10:
        num *= 1000
    return total + section + num


def parse_amount(text: str) -> tuple[float | None, str, str | None]:
    """返回 (金额, 币种, 原文片段)。识别不了返回 None。"""
    # 阿拉伯数字优先
    for m in AMOUNT_RE.finditer(text):
        raw, unit = m.group(1), m.group(2)
        try:
            val = float(raw)
        except ValueError:
            continue
        if unit == "万" or unit in ("w", "W"):
            val *= 10000
        elif unit == "千" or unit in ("k", "K"):
            val *= 1000
        # 过滤掉明显不是金额的数字（如日期里的 10、月份 9）
        if val <= 0:
            continue
        # 日期形式 10月12日 里的数字跳过
        tail = text[m.end():m.end() + 1]
        if tail in ("月", "日", "号", "年", "点"):
            continue
        currency = "CNY"
        if re.search(r"(美元|美金|USD|\$)", text):
            currency = "USD"
        return round(val, 2), currency, m.group(0)

    # 中文数字
    for m in re.finditer(r"([零〇一二两三四五六七八九十百千万]{1,6})\s*(块钱|块|元)?", text):
        seg = m.group(1)
        if not m.group(2) and not re.search(r"(转|给|汇|充|付|买|存|取)", text):
            continue
        val = cn2num(seg)
        if val and val > 0:
            return round(val, 2), "CNY", m.group(0)
    return None, "CNY", None


def detect_month(text: str, now: datetime | None = None) -> tuple[str, str]:
    """返回 (YYYY-MM, 中文标签)。默认最近一个有完整数据的月份。"""
    now = now or datetime.now()

    def shift(base: datetime, months: int) -> datetime:
        y, m = base.year, base.month + months
        while m <= 0:
            y, m = y - 1, m + 12
        while m > 12:
            y, m = y + 1, m - 12
        return datetime(y, m, 1)

    if re.search(r"(上个?月|上月)", text):
        d = shift(now, -1)
    elif re.search(r"(上上个?月|前个月)", text):
        d = shift(now, -2)
    elif re.search(r"(这个月|本月|当月)", text):
        d = now
    elif re.search(r"(今年|年度|全年)", text):
        d = now
    else:
        m = re.search(r"(\d{1,2})\s*月", text)
        if m and 1 <= int(m.group(1)) <= 12:
            d = datetime(now.year, int(m.group(1)), 1)
        else:
            d = shift(now, -1)
    return d.strftime("%Y-%m"), f"{d.year} 年 {d.month} 月"


def find_contacts(text: str, contacts: list[dict]) -> list[dict]:
    """收款人消歧：支持本名、备注、手机尾号。"""
    hits: list[dict] = []
    # 手机尾号
    tail = re.search(r"尾号\s*(\d{3,4})", text)
    if tail:
        code = tail.group(1)
        return [c for c in contacts if c["phone"].endswith(code)]

    for c in contacts:
        if c["name"] in text:
            hits.append(c)
            continue
        note = c.get("note", "")
        if note:
            for token in re.split(r"[\s,，、]", note):
                if len(token) >= 2 and token in text:
                    hits.append(c)
                    break
    if hits:
        # 去重
        uniq = {c["id"]: c for c in hits}
        return list(uniq.values())

    # "给王" 这类只给了姓氏 / 称谓片段
    given = re.search(r"给\s*([\u4e00-\u9fa5]{1,4})", text)
    if given:
        q = given.group(1)
        hit = [c for c in contacts if q in c["name"] or c["name"].endswith(q[-1])
               or q in c.get("note", "")]
        if hit:
            return hit

    # "老王""小王"这类称谓
    m = re.search(r"([老小])([\u4e00-\u9fa5])", text)
    if m:
        prefix, ch = m.group(1), m.group(2)
        return [c for c in contacts if c["name"].endswith(ch) or ch in c["name"]]
    return []


INTENT_RULES = [
    ("transfer.create", [r"转\s*\d", r"转账", r"转给", r"打钱", r"汇给", r"AA", r"收一下"]),
    ("subscription.list", [r"订阅", r"自动续费", r"续费", r"为什么扣", r"扣钱", r"会员扣"]),
    ("bill.summary", [r"花哪", r"花了多少", r"支出", r"消费", r"账单", r"月报", r"年报", r"统计", r"异常", r"可疑", r"奇怪"]),
    ("wealth.list", [r"理财", r"收益", r"产品", r"申购", r"赎回", r"基?金", r"年化"]),
    ("card.list", [r"信用卡", r"额度", r"挂失", r"锁卡", r"解锁", r"卡片", r"卡号"]),
    ("account.balance", [r"余额", r"还有多少钱", r"账户有多少"]),
]

SPLIT_RE = re.compile(r"(?:另外|顺便|然后|还有|再加|同时|；|;)")


def split_multi(text: str) -> list[str]:
    parts = [p.strip("，,。 ") for p in SPLIT_RE.split(text) if p and p.strip("，,。 ")]
    return parts or [text]


def classify(seg: str) -> str:
    for intent, pats in INTENT_RULES:
        for p in pats:
            if re.search(p, seg):
                return intent
    return "unknown"


def is_confirm(text: str) -> bool:
    t = text.strip()
    return bool(re.fullmatch(r"(确认|确认执行|确认转账|是|是的|对|可以|好|好的|行|没问题|嗯|ok|OK|确定)[。！!]?", t))


def is_cancel(text: str) -> bool:
    t = text.strip()
    return bool(re.fullmatch(r"(取消|不要|不用|算了|不了|先不|别|停止)[。！!]?", t))


def detect_schedule(text: str, now: datetime | None = None) -> str | None:
    """识别定时转账：每周一 / 每月 5 号 / 明天。"""
    now = now or datetime.now()
    if re.search(r"每(周|星期)[一二三四五六日天]", text):
        m = re.search(r"每(?:周|星期)([一二三四五六日天])", text)
        return f"每周{m.group(1)}"
    if re.search(r"每月\s*\d{1,2}\s*[号日]", text):
        m = re.search(r"每月\s*(\d{1,2})\s*[号日]", text)
        return f"每月 {m.group(1)} 日"
    if "明天" in text:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def fmt_money(v: float) -> str:
    return f"¥{v:,.2f}"
