"""从单文件版的 style.css / body.html / ui.js 生成 Python 版的 web/index.html。

差异只在数据来源：
  单文件版  session.handle(text)               同步返回
  Python 版 POST /api/chat → /api/bootstrap    异步
渲染层（卡片、时间线、英雄区）完全复用，保证两个入口视觉一致。
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
SF = HERE.parents[2] / "src" / "single_file"
OUT = HERE / "index.html"

style = (SF / "style.css").read_text(encoding="utf-8")
body = (SF / "body.html").read_text(encoding="utf-8")
ui = (SF / "ui.js").read_text(encoding="utf-8")

# ---------------------------------------------------------------- body 调整
# Python 版不暴露待执行队列（数据在后端），去掉该页签与面板
body = body.replace('''      <button class="side-tab" data-tab="pending" type="button">
        <svg class="icon" aria-hidden="true"><use href="#i-calendar"></use></svg>
        待执行
      </button>
''', "")
body = body.replace('''    <div class="side-pane" id="panePending" hidden>
      <p class="pane-intro">定时转账和 AA 收款会排在这里，不会立即动账。</p>
      <div id="pendingList"></div>
    </div>
''', "")
# 页面内自检改为接口自检（Python 版引擎在后端）
body = body.replace('''          自检 17 项''', '''          接口自检''')

# ---------------------------------------------------------------- JS 改造
js = ui

js = js.replace('''  var C = window.AIBankCore;
  var A = window.AIBank;''',
'''  var API = location.origin;
  var sid = null;

  function fmtMoney(v) {
    var n = Number(v || 0);
    var s = n.toFixed(2).replace(/\\B(?=(\\d{3})+(?!\\d))/g, ",");
    return "¥" + s;
  }
  var C = { fmtMoney: fmtMoney };

  function getJSON(path) {
    return fetch(API + path, { headers: { "Accept": "application/json" } }).then(function (r) { return r.json(); });
  }
  function postJSON(path, data) {
    return fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data || {})
    }).then(function (r) { return r.json(); });
  }''')

js = js.replace('''  var session = null;''',
'''  var session = {
    db: { contacts: [], accounts: [], subscriptions: [], wealth: [], scheduled: [] },
    audit: [],
    waiting: null
  };''')

# 待执行面板已移除，做空值保护
js = js.replace('''  function renderPending() {
    if (!session) return;''',
'''  function renderPending() {
    if (!pendingBox) return;''')

js = js.replace('''    } else {
      pendingBox.innerHTML = '<div class="empty-note">' + ICON("calendar") + "还没有" + "操作记录</div>";
    }''', "")

# ---- submit 改为异步
start = js.index("  function submit(fixed) {")
end = js.index("  /* ------------------------------------------------------------ 场景面板 */")
new_submit = '''  function submit(fixed) {
    var text = fixed != null ? fixed : input.value;
    text = String(text || "").trim();
    if (!text || busy) return;
    if (fixed == null) {
      input.value = "";
      input.style.height = "auto";
    }
    busy = true;
    sendBtn.disabled = true;

    turn("user", text);

    postJSON("/api/chat", { session_id: sid, text: text }).then(function (out) {
      sid = out.session_id || sid;
      var host = turn("bot", out.text);
      renderCards(out.cards, host);
      stampCountdowns();
      startTicker();
      syncState(out.state);
      return getJSON("/api/timeline?session_id=" + encodeURIComponent(sid || "")).then(function (tj) {
        session.audit = (tj && tj.audit) || [];
        renderTimeline();
        return getJSON("/api/bootstrap?session_id=" + encodeURIComponent(sid || ""));
      }).then(function (bs) {
        sid = bs.session_id || sid;
        applyBootstrap(bs);
      });
    }).catch(function (err) {
      turn("bot", "服务连接失败，请确认 server.py 还在运行。" +
        (err && err.message ? "（" + err.message + "）" : ""));
    }).then(function () {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
      scrollDown();
    });
  }

  function applyBootstrap(bs) {
    session.db.contacts = bs.contacts || [];
    session.db.subscriptions = bs.subscriptions || [];
    session.db.wealth = bs.wealth || [];
    session.db.accounts = [{ type: "借记卡", no: (bs.debit || {}).no || "", balance: (bs.debit || {}).balance || 0 }];
    syncBalance();
    renderHero();
  }

'''
js = js[:start] + new_submit + js[end:]

# ---- reset 改为异步
start = js.index("  function reset() {")
end = js.index("  function boot() {")
new_reset = '''  function reset() {
    chat.innerHTML = "";
    timelineBox.innerHTML = "";
    syncState({ status: "idle" });
    balanceHidden = false;
    $("#balanceToggle").setAttribute("aria-pressed", "false");
    $("#balanceToggle").innerHTML = ICON("eye", "icon-sm");
    showHero();
    renderHero();
    session.audit = [];
    renderTimeline();
    postJSON("/api/reset", { session_id: sid }).then(function (out) {
      sid = (out && out.session_id) || sid;
      return getJSON("/api/bootstrap?session_id=" + encodeURIComponent(sid || ""));
    }).then(function (bs) {
      sid = bs.session_id || sid;
      applyBootstrap(bs);
    }).catch(function () { /* 离线时保留静态框架 */ });
  }

'''
js = js[:start] + new_reset + js[end:]

# ---- 自检改为接口自检
start = js.index("  var CHECKS = [")
end = js.index("  /* ------------------------------------------------------------ 启动 */")
new_self = '''  function runSelfTest() {
    var results = [];
    var host = turn("bot", "正在检查前后端链路，请稍候…");
    var box = el("div", "card");
    host.appendChild(box);

    function step(name, fn) {
      return Promise.resolve().then(fn).then(function (ok) {
        results.push({ name: name, ok: true, got: ok || "✓" });
      }).catch(function (e) {
        results.push({ name: name, ok: false, got: String(e && e.message ? e.message : e) });
      });
    }

    getJSON("/api/health")
      .then(function (h) {
        return step("① 服务健康检查", function () {
          if (!h || !h.ok) throw new Error("health 返回异常");
          return "/api/health ok，会话数 " + (h.sessions || 0);
        });
      })
      .then(function () {
        return step("② 一轮问答往返", function () {
          return postJSON("/api/chat", { session_id: sid, text: "余额还有多少" }).then(function (o) {
            if (!o || !o.text) throw new Error("无回复");
            sid = o.session_id || sid;
            return o.text.slice(0, 22);
          });
        });
      })
      .then(function () {
        return step("③ 审计记录可读", function () {
          return getJSON("/api/timeline?session_id=" + encodeURIComponent(sid || "")).then(function (t) {
            if (!t || !t.audit || !t.audit.length) throw new Error("审计为空");
            return "共 " + t.audit.length + " 条记录";
          });
        });
      })
      .then(function () {
        box.innerHTML = "";
        var pass = results.filter(function (r) { return r.ok; }).length;
        var head = el("div", "card-head");
        head.innerHTML = '<div class="card-title">' + ICON("shield") + "<span>接口自检 " +
          pass + "/" + results.length + "</span></div>" +
          badge(pass === results.length ? "全部通过" : "有失败项", pass === results.length ? "ok" : "bad");
        box.appendChild(head);
        var rows = results.map(function (r) {
          return "<tr><td>" + esc(r.name) + "</td><td>" +
            badge(r.ok ? "PASS" : "FAIL", r.ok ? "ok" : "bad") + "</td><td>" + esc(r.got) + "</td></tr>";
        }).join("");
        box.insertAdjacentHTML("beforeend",
          "<table><thead><tr><th>检查项</th><th>结果</th><th>说明</th></tr></thead><tbody>" + rows + "</tbody></table>");
        turn("bot", "自检完成：" + pass + "/" + results.length + " 通过。" +
          (pass === results.length ? "前后端链路正常。" : "请检查 server.py 状态。"));
        scrollDown();
        return getJSON("/api/timeline?session_id=" + encodeURIComponent(sid || ""));
      })
      .then(function (tj) {
        session.audit = (tj && tj.audit) || [];
        renderTimeline();
      })
      .catch(function (e) {
        turn("bot", "自检未完成：" + (e && e.message ? e.message : e));
      });
  }

'''
js = js[:start] + new_self + js[end:]

# 场景列表里不再依赖引擎；保持不变

# ---------------------------------------------------------------- 组装
html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#123b34">
<meta name="application-name" content="智银研创 · AI 银行助理">
<title>智银研创 · AI 银行助理</title>
<style>
%s
</style>
</head>
<body>

%s

<script>
%s
</script>
</body>
</html>
""" % (style.rstrip(), body.rstrip(), js.rstrip())

OUT.write_text(html, encoding="utf-8", newline="\n")
print("已生成:", OUT, len(html), "chars")
