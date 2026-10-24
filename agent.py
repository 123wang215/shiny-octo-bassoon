"""智能体核心（1 号负责）：解析 → 补全 → 规划 → 确认 → 执行 → 回执。

关键设计：模型只做"理解与表达"，所有执行与风控走确定性代码。
这样即使模型胡说，也转不走一分钱 —— 这是我们的幻觉防护底座。
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from . import nlu, security
from .nlu import fmt_money
from .store import build
from .tools import Bank, _trace

MAX_CLARIFY = 2
CALL_BUDGET = 6


class Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.db = build()
        self.bank = Bank(self.db)
        self.pending: security.ConfirmTicket | None = None
        self.queue: list[str] = []
        self.pending_clarify: dict | None = None
        self.clarify_count = 0
        self.audit: list[dict] = []
        self.history: list[dict] = []
        self.created_at = datetime.now()

    # ---------------- 审计 ----------------
    def log(self, action: str, tool: str = "-", args: Any = None, ok: bool = True,
            trace: str = "-", detail: str = "", actor: str = "agent", level: str = "") -> None:
        self.audit.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "actor": actor, "action": action, "tool": tool, "args": args,
            "ok": ok, "trace_id": trace, "detail": detail, "level": level,
        })

    # ---------------- 主入口 ----------------
    def handle(self, text: str) -> dict:
        text = (text or "").strip()
        self.history.append({"role": "user", "text": text, "ts": datetime.now().strftime("%H:%M:%S")})
        self.log("收到消息", actor="user", detail=text[:40])

        # 1. 注入 / 越权检测（最优先，先于任何意图识别）
        scan = security.scan(text)
        if scan.blocked:
            self.log("安全拦截", ok=False, detail=f"{scan.category}:{scan.matched}")
            return self._respond(scan.reply, [{"type": "handoff", "reason": "该请求涉及安全红线",
                                               "entry": "转人工核实"}])

        # 2. 确认态优先
        if self.pending is not None:
            handled = self._handle_confirm_state(text)
            if handled is not None:
                return handled

        # 2.5 上一轮的追问（问收款人 / 问金额）优先接续
        resumed = self._resume_clarify(text)
        if resumed is not None:
            return resumed

        # 3. 意图识别（多意图拆解）
        segs = nlu.split_multi(text)
        intents = [(nlu.classify(s), s) for s in segs]
        primary = next(((i, s) for i, s in intents if i != "unknown"), None)
        if primary is None:
            return self._unknown(text)

        rest = [i for i in intents if i[0] != "unknown"][1:]
        self.queue = list(rest)
        return self._route(primary[0], text, primary[1])

    # ---------------- 确认态 ----------------
    def _handle_confirm_state(self, text: str) -> dict | None:
        t = self.pending
        assert t is not None
        if nlu.is_cancel(text):
            self.pending = None
            self.log("用户取消操作", tool=t.payload.get("tool", "-"), ok=True)
            return self._respond("已取消，没有产生任何扣款。还有别的要办的吗？")
        if nlu.is_confirm(text):
            ok, why = t.valid(t.fingerprint)
            if not ok:
                self.pending = None
                if why == "expired":
                    return self._respond("刚才那笔确认已超时失效（超过 120 秒），需要我重新发起吗？")
                return self._respond("参数已经变了，需要重新确认。我重新出一张确认卡？")
            self.pending = None
            return self._execute(t)
        # 既不是确认也不是取消：先答新问题，再提醒
        return None

    # ---------------- 路由 ----------------
    def _route(self, intent: str, raw: str, seg: str) -> dict:
        handler = {
            "account.balance": self._do_balance,
            "bill.summary": self._do_bill,
            "subscription.list": self._do_subscription,
            "transfer.create": self._do_transfer,
            "wealth.list": self._do_wealth,
            "card.list": self._do_card,
        }.get(intent)
        if handler is None:
            return self._unknown(raw)
        return handler(raw, seg)

    # ---------------- 各场景 ----------------
    def _do_balance(self, raw: str, seg: str) -> dict:
        r = self.bank.balance()
        self.log("查询余额", "account.balance", ok=True, trace=r["trace_id"], level="query")
        d = r["data"]
        text = (f"借记卡可用 {fmt_money(d['debit']['balance'])}，"
                f"信用卡可用 {fmt_money(d['credit']['available'])}，"
                f"本期账单 {d['credit']['bill_due']} 到期。")
        return self._respond(text, [{"type": "table", "columns": ["账户", "可用/余额"], "rows": [
            ["借记卡 " + d["debit"]["no"], fmt_money(d["debit"]["balance"])],
            ["信用卡 " + d["credit"]["no"], fmt_money(d["credit"]["available"])],
        ]}])

    def _do_bill(self, raw: str, seg: str) -> dict:
        month, label = nlu.detect_month(raw)
        # 问"有没有异常"但没说月份时，查最近一个月（异常往往就在眼前）
        explicit_month = re.search(r"(上个?月|本月|这个月|\d{1,2}\s*月|今年)", raw)
        if not explicit_month and any(k in raw for k in ["异常", "可疑", "奇怪"]):
            month, label = datetime.now().strftime("%Y-%m"), "最近一个月"
        if any(k in raw for k in ["异常", "奇怪", "不对", "可疑", "重复"]):
            r = self.bank.bill_anomalies(month)
            self.log("异常交易识别", "bill.anomalies", {"month": month}, r["ok"], r["trace_id"], level="query")
            if not r["ok"]:
                return self._respond(r["error"])
            d = r["data"]
            if d["count"] == 0:
                return self._respond(f"{label}没有发现异常交易，共 {len([t for t in self.db['transactions'] if t['month'] == month])} 笔。")
            top = d["items"][0]
            text = f"{label}发现 {d['count']} 笔可疑交易，最需要看的是 {top['date'][5:]} 在「{top['merchant']}」的 {fmt_money(top['amount'])}，{top['reason']}。"
            cards = [{"type": "table", **d["table"]}]
            if d.get("suggestion"):
                text += "建议先锁卡再核实。"
                cards.append({"type": "options", "items": [
                    {"id": "lock", "label": "立即锁卡"}, {"id": "detail", "label": "看这笔详情"}]})
            return self._respond(text, cards)

        if any(k in raw for k in ["年度", "全年", "今年"]):
            r = self.bank.bill_annual()
            self.log("年度账单", "bill.annual", ok=r["ok"], trace=r["trace_id"], level="query")
            d = r["data"]
            return self._respond(f"共 {len(d['months'])} 个月合计支出 {fmt_money(d['total'])}。",
                                 [{"type": "table", **d["table"]}])

        cat = next((c for c in ["餐饮", "购物", "交通", "娱乐", "通讯", "医疗", "教育", "生活缴费"]
                    if c in raw), None)
        if cat:
            r = self.bank.bill_detail(month, cat)
            self.log("分类明细", "bill.detail", {"month": month, "category": cat}, r["ok"], r["trace_id"], level="query")
            d = r["data"]
            return self._respond(f"{label}「{cat}」金额最高的 {d['count']} 笔：", [{"type": "table", **d["table"]}])

        r = self.bank.bill_summary(month)
        self.log("账单汇总", "bill.summary", {"month": month}, r["ok"], r["trace_id"], level="query")
        if not r["ok"]:
            return self._respond(r["error"])
        d = r["data"]
        mom = f"，比上月{'多' if (d['mom_pct'] or 0) >= 0 else '少'} {abs(d['mom_pct'])}%" if d["mom_pct"] is not None else ""
        top2 = d["by_category"][:2]
        share = sum(c["pct"] for c in top2)
        text = (f"{label}共支出 {fmt_money(d['total'])}，{d['count']} 笔{mom}；"
                f"{top2[0]['category']}和{top2[1]['category']}占了 {share:.0f}%。")
        return self._respond(text, [{"type": "table", **d["table"]},
                                    {"type": "options", "items": [
                                        {"id": "anomaly", "label": "看有没有异常"},
                                        {"id": "top", "label": "看最大那几笔"}]}])

    def _do_subscription(self, raw: str, seg: str) -> dict:
        r = self.bank.subscription_list()
        self.log("订阅清单", "subscription.list", ok=r["ok"], trace=r["trace_id"], level="query")
        d = r["data"]
        want_cancel = any(k in raw for k in ["取消", "退", "停", "关掉"])

        if want_cancel:
            # 定位具体订阅
            target = next((s for s in d["items"] if s["name"][:4] in raw or
                           any(k in raw for k in [s["name"][:2]])), None)
            if target is None:
                items = [{"id": s["id"], "label": f"{s['name']}（{fmt_money(s['amount'])}/{s['cycle']}）"}
                         for s in d["items"][:3]]
                if len(d["items"]) > 1:
                    return self._respond(
                        f"你一共有 {d['count']} 个订阅，每月合计 {fmt_money(d['monthly_total'])}。要停哪一个？",
                        [{"type": "table", **d["table"]},
                         {"type": "options", "items": items}])
                target = d["items"][0]
            payload = {"tool": "subscription.cancel", "args": {"sub_id": target["id"]}}
            ticket = security.make_ticket("取消订阅", f"取消「{target['name']}」，下月起不再扣 {fmt_money(target['amount'])}", payload)
            self.pending = ticket
            self.log("发起确认", "subscription.cancel", payload["args"], level="change")
            return self._respond(f"确认要取消「{target['name']}」吗？取消后下月起不再扣 {fmt_money(target['amount'])}。",
                                 [{"type": "confirm_card", "action": "取消订阅", "target": target["name"],
                                   "amount": fmt_money(target["amount"]) + "/月", "when": "立即生效",
                                   "impact": "下月起不再扣费，已扣费用不退", "expires_in": 120,
                                   "confirm_token": ticket.token}])

        return self._respond(
            f"你有 {d['count']} 个自动续费，每月固定扣 {fmt_money(d['monthly_total'])}，最近一笔是 {d['items'][0]['next_charge']} 的「{d['items'][0]['name']}」。",
            [{"type": "table", **d["table"]},
             {"type": "options", "items": [{"id": "cancel", "label": "取消某个订阅"},
                                           {"id": "remind", "label": "扣费前提醒我"}]}])

    def _do_transfer(self, raw: str, seg: str) -> dict:
        amount, currency, _ = nlu.parse_amount(raw)
        matches = nlu.find_contacts(raw, self.db["contacts"])

        if not matches:
            opts = [{"id": c["id"], "label": f"{c['name']}（{c['phone'][-4:]}）"} for c in self.db["contacts"][:3]]
            self.pending_clarify = {"kind": "payee", "amount": amount,
                                    "currency": currency, "schedule": nlu.detect_schedule(raw)}
            hint = ("转账要先有收款人，你通讯录里的常用联系人是这些：" if re.search(r"转", raw)
                    else "没在通讯录里找到这个收款人，可以选一位常用联系人：")
            return self._respond(hint, [{"type": "options", "items": opts}])

        if len(matches) > 1:
            opts = [{"id": c["id"], "label": f"{c['name']}（{c['phone'][-4:]}）"} for c in matches]
            names = "、".join(f"{c['name']}（尾号 {c['phone'][-4:]}）" for c in matches)
            self.pending_clarify = {"kind": "payee", "amount": amount,
                                    "currency": currency, "schedule": nlu.detect_schedule(raw)}
            return self._respond(f"通讯录里有 {len(matches)} 位可能的人：{names}，是哪一位？",
                                 [{"type": "options", "items": opts}])

        return self._transfer_to(matches[0], amount, currency,
                                 nlu.detect_schedule(raw))

    def _transfer_to(self, payee: dict, amount: float | None,
                     currency: str = "CNY", schedule: str | None = None) -> dict:
        if amount is None:
            self.clarify_count += 1
            self.pending_clarify = {"kind": "amount", "payee_id": payee["id"],
                                    "currency": currency, "schedule": schedule}
            return self._respond(f"要转给 {payee['name']} 多少钱？", [{"type": "options", "items": [
                {"id": "100", "label": "¥100"}, {"id": "500", "label": "¥500"}, {"id": "1000", "label": "¥1,000"}]}])

        if amount <= 0:
            return self._respond("金额必须大于 0，请重新给一个数。")

        first_time = not any(t["merchant"] == f"转账-{payee['name']}" for t in self.db["transactions"])
        debit = next(a for a in self.db["accounts"] if a["type"] == "借记卡")
        if amount > debit["balance"]:
            return self._respond(
                f"余额不足：这笔要 {fmt_money(amount)}，当前可用 {fmt_money(debit['balance'])}，"
                f"还差 {fmt_money(amount - debit['balance'])}。",
                [{"type": "options", "items": [{"id": "part", "label": f"改转 {fmt_money(debit['balance'])}"},
                                               {"id": "topup", "label": "先充值"}, {"id": "cancel", "label": "算了"}]}])

        payload = {"tool": "transfer.create",
                   "args": {"payee_id": payee["id"], "amount": amount, "currency": currency,
                            "note": "", "schedule": schedule, "payee_first_time": first_time}}
        level = security.level_of("transfer.create", payload["args"])
        when = schedule or "立即到账"
        ticket = security.make_ticket("转账", f"向 {payee['name']} 转 {fmt_money(amount)}", payload)
        self.pending = ticket
        self.log("发起确认", "transfer.create", payload["args"], level=level)
        note = "首次向该收款人转账，需短信验证" if first_time else "转出后不可撤销"
        return self._respond(
            f"请确认这笔转账（{when}），{note}。",
            [{"type": "confirm_card", "action": "转账", "target": f"{payee['name']}（{payee['phone'][-4:]}）",
              "amount": fmt_money(amount), "when": when, "impact": note,
              "expires_in": 120, "confirm_token": ticket.token, "risk": level}])

    def _resume_clarify(self, text: str) -> dict | None:
        """接续上一轮的追问：用户这句是在回答"哪一位"或"多少钱"。"""
        c = self.pending_clarify
        if not c:
            return None
        if nlu.is_cancel(text):
            self.pending_clarify = None
            return self._respond("好的，这笔转账不办了。还有别的要处理吗？")

        if c["kind"] == "payee":
            matches = nlu.find_contacts(text, self.db["contacts"])
            if len(matches) == 1:
                self.pending_clarify = None
                return self._transfer_to(matches[0], c.get("amount"), c.get("currency", "CNY"), c.get("schedule"))
            if len(matches) > 1:
                opts = [{"id": m["id"], "label": f"{m['name']}（{m['phone'][-4:]}）"} for m in matches]
                return self._respond("还是有点不确定，点一个吧：", [{"type": "options", "items": opts}])
            return None

        if c["kind"] == "amount":
            amt, cur, _ = nlu.parse_amount(text)
            if amt:
                payee = next((x for x in self.db["contacts"] if x["id"] == c["payee_id"]), None)
                self.pending_clarify = None
                if payee:
                    return self._transfer_to(payee, amt, cur, c.get("schedule"))
            return None
        return None

    def _do_wealth(self, raw: str, seg: str) -> dict:
        r = self.bank.wealth_list()
        self.log("理财查询", "wealth.list", ok=r["ok"], trace=r["trace_id"], level="query")
        d = r["data"]

        if any(k in raw for k in ["申购", "买", "投"]) and re.search(r"(买|申购)", raw):
            amount, _, _ = nlu.parse_amount(raw)
            pick = next((p for p in d["items"] if p["name"][:2] in raw), None)
            if pick is None:
                items = [{"id": p["id"], "label": f"{p['name']}（年化 {p['annual_rate']}%，{p['risk']}）"}
                         for p in d["items"][:3]]
                return self._respond("想买哪一只？", [{"type": "options", "items": items}])
            if amount is None:
                return self._respond(f"「{pick['name']}」起购 {fmt_money(pick['min_amount'])}，你打算投多少？")
            payload = {"tool": "wealth.purchase", "args": {"product_id": pick["id"], "amount": amount}}
            ticket = security.make_ticket("申购", f"申购「{pick['name']}」{fmt_money(amount)}", payload)
            self.pending = ticket
            expect = amount * pick["annual_rate"] / 100 * (pick["term_days"] or 365) / 365
            self.log("发起确认", "wealth.purchase", payload["args"], level="change")
            return self._respond(
                f"确认申购「{pick['name']}」？按年化 {pick['annual_rate']}% 估算，到期收益约 {fmt_money(expect)}（实际以产品为准）。",
                [{"type": "confirm_card", "action": "申购", "target": pick["name"],
                  "amount": fmt_money(amount), "when": f"{pick['term_days']} 天" if pick["term_days"] else "活期",
                  "impact": f"{pick['risk']}，非保本浮动收益", "expires_in": 120,
                  "confirm_token": ticket.token}])

        hold = d["holdings"][0] if d["holdings"] else None
        text = f"在售 {d['count']} 只产品，年化从 {min(p['annual_rate'] for p in d['items'])}% 到 {max(p['annual_rate'] for p in d['items'])}%，风险等级 R1 到 R4。"
        if hold:
            text += f"你当前持有「{hold['name']}」{fmt_money(hold['amount'])}，累计收益 {fmt_money(hold['profit'])}。"
        return self._respond(text, [{"type": "table", **d["table"]},
                                    {"type": "options", "items": [
                                        {"id": "compare", "label": "对比前三只"},
                                        {"id": "buy", "label": "我要申购"}]}])

    def _do_card(self, raw: str, seg: str) -> dict:
        r = self.bank.card_list()
        self.log("卡片查询", "card.list", ok=r["ok"], trace=r["trace_id"], level="query")
        d = r["data"]
        credit = next(c for c in d["items"] if c["type"] == "信用卡")

        if any(k in raw for k in ["挂失", "丢了", "找不到卡"]):
            payload = {"tool": "card.report_loss", "args": {"card_id": credit["id"]}}
            ticket = security.make_ticket("挂失", f"挂失信用卡 {credit['no']}", payload)
            self.pending = ticket
            self.log("发起确认", "card.report_loss", payload["args"], level="high")
            return self._respond("挂失后该卡立即失效，补卡 7 个工作日寄达。确认挂失？",
                                 [{"type": "confirm_card", "action": "挂失", "target": f"信用卡 {credit['no']}",
                                   "amount": "—", "when": "立即生效", "impact": "卡片永久失效，需补卡",
                                   "expires_in": 120, "confirm_token": ticket.token, "risk": "high"}])

        if any(k in raw for k in ["锁卡", "解锁", "冻结"]):
            unlock = "解锁" in raw
            payload = {"tool": "card.lock", "args": {"card_id": credit["id"], "unlock": unlock}}
            ticket = security.make_ticket("锁卡", f"{'解锁' if unlock else '锁定'}信用卡 {credit['no']}", payload)
            self.pending = ticket
            self.log("发起确认", "card.lock", payload["args"], level="change")
            return self._respond(f"确认{'解锁' if unlock else '锁定'}这张信用卡？",
                                 [{"type": "confirm_card", "action": "解锁" if unlock else "锁卡",
                                   "target": f"信用卡 {credit['no']}", "amount": "—", "when": "立即生效",
                                   "impact": "锁定期间无法交易" if not unlock else "恢复后可正常交易",
                                   "expires_in": 120, "confirm_token": ticket.token}])

        if "额度" in raw and not re.search(r"(多少|几|查询|看看|还有|是什么)", raw):
            amount, _, _ = nlu.parse_amount(raw)
            if amount is None:
                return self._respond(f"当前额度 {fmt_money(credit['limit'])}，可用 {fmt_money(credit['available'])}。想提到多少？")
            payload = {"tool": "card.limit_adjust", "args": {"new_limit": amount}}
            ticket = security.make_ticket("提额", f"申请将额度从 {fmt_money(credit['limit'])} 提到 {fmt_money(amount)}", payload)
            self.pending = ticket
            self.log("发起确认", "card.limit_adjust", payload["args"], level="high")
            return self._respond("提额需银行审核，确认提交申请？",
                                 [{"type": "confirm_card", "action": "额度调整",
                                   "target": f"信用卡 {credit['no']}",
                                   "amount": f"{fmt_money(credit['limit'])} → {fmt_money(amount)}",
                                   "when": "1 至 3 个工作日", "impact": "以审核结果为准",
                                   "expires_in": 120, "confirm_token": ticket.token, "risk": "high"}])

        return self._respond(f"你有 {len(d['items'])} 张卡，信用卡可用 {fmt_money(credit['available'])}。",
                             [{"type": "table", **d["table"]},
                              {"type": "options", "items": [{"id": "limit", "label": "调整额度"},
                                                            {"id": "lock", "label": "锁卡"},
                                                            {"id": "loss", "label": "挂失"}]}])

    # ---------------- 执行 ----------------
    def _execute(self, ticket: security.ConfirmTicket) -> dict:
        tool = ticket.payload["tool"]
        args = dict(ticket.payload["args"])
        level = security.level_of(tool, args)

        # 强验证（模拟）：高风险操作追加一道
        auth_note = ""
        if level == "high":
            self.log("强验证", "auth.verify", {"方式": "短信验证码"}, ok=True, level="high")
            auth_note = "已通过短信验证，"

        start = time.time()
        if tool == "transfer.create":
            args.pop("payee_first_time", None)
            r = self.bank.transfer(**args)
        elif tool == "subscription.cancel":
            r = self.bank.subscription_cancel(**args)
        elif tool == "wealth.purchase":
            r = self.bank.wealth_purchase(**args)
        elif tool == "card.lock":
            r = self.bank.card_lock(**args)
        elif tool == "card.report_loss":
            r = self.bank.card_report_loss(**args)
        elif tool == "card.limit_adjust":
            r = self.bank.card_limit_adjust(**args)
        else:
            r = {"ok": False, "error": "未实现的工具", "trace_id": _trace(), "data": None}

        cost = int((time.time() - start) * 1000)
        self.log("执行", tool, args, r["ok"], r["trace_id"], f"{cost}ms", level=level)

        if not r["ok"]:
            # 失败：停后续步骤 + 报三件事
            self.queue.clear()
            return self._respond(f"这一步没成功：{r['error']}。没有产生任何扣款，你可以改一下金额或稍后再试。",
                                 [{"type": "result_card", "action": ticket.action, "status": "failed",
                                   "amount": "—", "trace_id": r["trace_id"], "eta": "—"}])

        d = r["data"]
        if tool == "transfer.create":
            text = (f"{auth_note}已转出 {fmt_money(d['amount'])} 给 {d['payee']}，"
                    f"余额剩 {fmt_money(d['balance_after'])}，可在 3 分钟内撤销。")
        elif tool == "subscription.cancel":
            text = f"已取消「{d['name']}」，下月起不再扣 {fmt_money(d['amount'])}。"
        elif tool == "wealth.purchase":
            text = (f"已申购「{d['product']}」{fmt_money(d['amount'])}，"
                    f"期限 {d['term_days']} 天，按年化估算收益约 {fmt_money(d['expected_profit'])}。")
        elif tool == "card.lock":
            text = f"卡片已{'解锁' if d['status'] == 'normal' else '锁定'}，状态：{d['status']}。"
        elif tool == "card.report_loss":
            text = f"信用卡 {d['card']} 已挂失，{d['note']}。"
        elif tool == "card.limit_adjust":
            text = f"提额申请已提交：{fmt_money(d['current'])} → {fmt_money(d['requested'])}，{d['eta']}出结果。"
        else:
            text = "已完成。"

        card = {"type": "result_card", "action": ticket.action.strip(), "status": "success",
                "amount": fmt_money(d.get("amount", 0)) if isinstance(d.get("amount"), (int, float)) else "—",
                "trace_id": r["trace_id"], "eta": d.get("arrive") or d.get("eta") or "已生效"}

        # 多意图：这件事办成后，把后面几件接着办完（失败时队列已被清空）
        if self.queue:
            return self._drain_queue(text, [card])
        return self._respond(text, [card])

    def _drain_queue(self, prev_text: str, prev_cards: list[dict]) -> dict:
        texts = [prev_text + "\n✅ 这件事办完了，接着处理下一件：" + _intent_name(self.queue[0][0])]
        cards = list(prev_cards)
        mark = len(self.history)
        last_state = {"status": "idle", "pending": None}
        while self.queue:
            intent, seg = self.queue.pop(0)
            out = self._route(intent, seg, seg)
            texts.append(out["text"])
            cards.extend(out.get("cards", []))
            last_state = out.get("state", last_state)
        # 丢掉内层 _respond 产生的重复历史，只保留合并后的一条
        del self.history[mark:]
        text = "\n".join(texts)
        self.history.append({"role": "agent", "text": text, "cards": cards,
                             "ts": datetime.now().strftime("%H:%M:%S")})
        return {"text": text, "cards": cards, "state": last_state, "audit": self.audit[-6:]}

    # ---------------- 兜底 ----------------
    def _unknown(self, text: str) -> dict:
        self.clarify_count += 1
        if self.clarify_count > MAX_CLARIFY:
            self.log("转人工", "handoff", ok=True, detail="连续无法识别")
            return self._respond("我可能没理解对，帮你转人工客服会更快。",
                                 [{"type": "handoff", "reason": "连续 3 次未识别意图", "entry": "人工客服"}])
        return self._respond("我没太听懂这句。你可以这样说：", [{"type": "options", "items": [
            {"id": "bill", "label": "我上个月钱都花哪了"},
            {"id": "sub", "label": "我都在为什么扣钱"},
            {"id": "transfer", "label": "转 500 给老王"},
        ]}])

    def _respond(self, text: str, cards: list[dict] | None = None) -> dict:
        cards = cards or []
        self.history.append({"role": "agent", "text": text, "cards": cards,
                             "ts": datetime.now().strftime("%H:%M:%S")})
        return {
            "text": text,
            "cards": cards,
            "state": {"status": "confirming" if self.pending else "idle",
                      "pending": ({"action": self.pending.action, "expires_in": max(
                          0, int(self.pending.expires_at - time.time()))} if self.pending else None)},
            "audit": self.audit[-6:],
        }


def _intent_name(intent: str) -> str:
    return {
        "bill.summary": "账单分析", "subscription.list": "订阅管理",
        "transfer.create": "转账", "wealth.list": "理财", "card.list": "卡片管理",
        "account.balance": "余额查询",
    }.get(intent, intent)
