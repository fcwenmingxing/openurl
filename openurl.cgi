#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
openurl.cgi — 手机向车机发送网址的网页服务（房间号机制）

设计
----
* 纯 CGI + 文件信箱（无长连接 / 无 WebSocket，兼容受限代理）
* 房间数据目录：/tmp/openurl_rooms/<room>.json（CGI 自建，属主 www-data）
* 服务器不长期存储：车机取走消息后立即删除；房间 30 分钟无活动惰性清理
* 车机端：短轮询 2 秒拉取新消息，取到即写入 localStorage 并跳转
* 手机端：扫码进入房间，输入网址发送
* 二维码：服务端生成 PNG（依赖 python3-qrcode + Pillow）

接口
----
GET  openurl.cgi                        → 车机入口（自动生成/读取房间号）
GET  openurl.cgi                        → 车机模式（自动建房 / 读缓存房间号）
GET  openurl.cgi?room=1234               → 车机模式（进入指定房间）
GET  openurl.cgi?room=1234&m=1           → 手机模式（扫码链接）
GET  openurl.cgi?action=qr&room=1234     → 二维码 PNG
POST openurl.cgi?action=send&room=..     → 手机发送网址（body: url=..）
GET  openurl.cgi?action=poll&room=..&since=N → 车机短轮询（取走即删）
POST openurl.cgi?action=join&room=..     → 进房上报

作者：讷言  日期：2026-09-28
"""

import os
import sys
import json
import time
import random
import re
import fcntl
import io
import urllib.parse

ROOM_DIR = "/tmp/openurl_rooms"
ROOM_TTL = 30 * 60
ROOM_MAX_MSGS = 50
POLL_INTERVAL_MS = 2000

# 二维码链接的兜底配置（用环境变量覆盖，见 README「配置」一节）
#   OPENURL_DEFAULT_HOST  取不到 HTTP_HOST 时使用的兜底主机名，如 example.com:8080
#   OPENURL_HTTPS_HOSTS   逗号分隔的主机名片段；host 命中任一即强制 https，如 example.com,.myds.me
# 每次调用时读取，避免在长驻进程里沿用启动时的环境。
def default_public_host():
    return os.environ.get("OPENURL_DEFAULT_HOST", "").strip()


def https_host_patterns():
    return [
        s.strip().lower()
        for s in os.environ.get("OPENURL_HTTPS_HOSTS", "").split(",")
        if s.strip()
    ]


def want_https(host):
    """host 命中 OPENURL_HTTPS_HOSTS 中任一子串时返回 True。"""
    h = (host or "").lower()
    return any(p in h for p in https_host_patterns())


# --------------------------- 房间存储 --------------------------------------
def now_ts():
    return int(time.time())


def ensure_room_dir():
    try:
        os.makedirs(ROOM_DIR, mode=0o755, exist_ok=True)
        return True
    except Exception:
        return False


def valid_room(r):
    return bool(re.fullmatch(r"\d{4}", r or ""))


def room_path(r):
    return os.path.join(ROOM_DIR, r + ".json")


def gen_room():
    for _ in range(80):
        r = "%04d" % random.randint(0, 9999)
        if not os.path.exists(room_path(r)):
            return r
    return "%04d" % random.randint(0, 9999)


def read_room(r):
    p = room_path(r)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_room(r, d):
    p = room_path(r)
    lock_p = p + ".lock"
    try:
        with open(lock_p, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, p)
            fcntl.flock(lf, fcntl.LOCK_UN)
        return True
    except Exception:
        return False


def get_or_create_room(r):
    d = read_room(r)
    if d is None:
        d = {"room": r, "created": now_ts(), "last_active": now_ts(),
             "seq": 0, "msgs": [], "phones": {}}
        write_room(r, d)
    d.setdefault("msgs", [])
    d.setdefault("phones", {})
    return d


def save_room(r, d):
    d["last_active"] = now_ts()
    write_room(r, d)


def cleanup_rooms():
    if not os.path.isdir(ROOM_DIR):
        return
    now = now_ts()
    try:
        for fn in os.listdir(ROOM_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(ROOM_DIR, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if now - int(d.get("last_active", 0)) > ROOM_TTL:
                    os.remove(p)
            except Exception:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


def append_msg(r, msg):
    d = get_or_create_room(r)
    d["seq"] = int(d.get("seq", 0)) + 1
    msg["seq"] = d["seq"]
    msg["time"] = now_ts()
    d["msgs"].append(msg)
    if len(d["msgs"]) > ROOM_MAX_MSGS:
        d["msgs"] = d["msgs"][-ROOM_MAX_MSGS:]
    save_room(r, d)
    return msg


def active_phone_count(d, window=10):
    """10 秒内有心跳的手机数量"""
    now = now_ts()
    ph = d.get("phones", {})
    return sum(1 for t in ph.values() if now - int(t) <= window)


# --------------------------- HTTP 输入/输出 --------------------------------
def get_query():
    return {k: v[0] for k, v in urllib.parse.parse_qs(os.environ.get("QUERY_STRING", "")).items()}


def read_body():
    try:
        n = int(os.environ.get("CONTENT_LENGTH", 0) or 0)
    except Exception:
        n = 0
    return sys.stdin.read(n) if n > 0 else ""


def body_params():
    raw = read_body()
    ct = os.environ.get("CONTENT_TYPE", "")
    if "application/json" in ct:
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}


def out_bytes(data, ctype, status=200):
    sys.stdout.buffer.write(("Status: %d\r\n" % status).encode())
    sys.stdout.buffer.write(("Content-Type: %s\r\n" % ctype).encode())
    sys.stdout.buffer.write(b"Cache-Control: no-store\r\n")
    sys.stdout.buffer.write(b"Access-Control-Allow-Origin: *\r\n")
    sys.stdout.buffer.write(b"\r\n")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def out_json(obj, status=200):
    out_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
              "application/json; charset=utf-8", status)
    sys.exit(0)


def out_html(body, status=200):
    out_bytes(body.encode("utf-8"), "text/html; charset=utf-8", status)
    sys.exit(0)


def make_qr_png(text):
    """服务端生成二维码 PNG"""
    import qrcode
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------- 页面 CSS/基础 ---------------------------------
CSS = """*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1115;color:#e8eaed}
.wrap{max-width:720px;margin:0 auto;padding:16px}
.card{background:#181b22;border-radius:14px;padding:16px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,.3)}
h1{font-size:20px;margin:0 0 6px}
.room{font-size:34px;font-weight:700;letter-spacing:6px;color:#4c8dff;text-align:center;margin:4px 0 10px}
.dim{color:#9aa0a6;font-size:13px}
input[type=text],input[type=url]{width:100%;padding:14px;border-radius:10px;border:1px solid #2b3038;background:#0d0f13;color:#e8eaed;font-size:16px;outline:none}
button{border:0;border-radius:10px;padding:13px 18px;font-size:16px;font-weight:600;color:#fff;background:#4c8dff;cursor:pointer}
button:active{opacity:.8}
.big{width:100%;margin-top:10px;padding:16px;font-size:18px}
.list{list-style:none;padding:0;margin:0}
.item{display:flex;align-items:flex-start;gap:10px;padding:12px;border-bottom:1px solid #242833}
.item:last-child{border-bottom:0}
.item .u{flex:1;word-break:break-all;font-size:15px;line-height:1.4}
.item .t{font-size:12px;color:#9aa0a6;margin-top:4px}
.item.pin{background:rgba(76,141,255,.08)}
#log .item{display:block}
.ib{background:#242833;color:#e8eaed;padding:8px 10px;font-size:13px;border-radius:8px;font-weight:500}
.ib.d{background:#3a1f1f;color:#ff8a80}
.ib.s{background:#1f5f2f;color:#8fffa8}
/* 手机端历史条目布局 */
.rowtop{display:flex;align-items:flex-start;gap:8px}
.rowtop a{flex:1;word-break:break-all}
.pinicon{background:transparent;border:0;color:#6b7280;font-size:22px;line-height:1;padding:2px 4px;cursor:pointer}
.pinicon.on{color:#ffd54a}
.acts{display:flex;gap:10px;margin-top:10px}
.acts .ib{flex:1;padding:12px 14px;font-size:15px;font-weight:600}
.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;background:#242833;color:#9aa0a6}
.qr{text-align:center;padding:10px}
.qr img{background:#fff;padding:10px;border-radius:12px;width:200px;height:200px}
.center{text-align:center}
.foot{margin-top:20px;text-align:center;font-size:12px;color:#9aa0a6}
a{color:#4c8dff}
.toast{display:none;position:fixed;left:50%;bottom:30px;transform:translateX(-50%);padding:10px 16px;border-radius:20px;font-size:14px;background:#333;z-index:99}
"""

HEAD = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>__TITLE__</title>
<link rel="icon" type="image/svg+xml" href="/openclaw/pic/satellite-dish.svg">
<link rel="apple-touch-icon" href="/openclaw/pic/satellite-dish.svg">
<style>__CSS__</style></head><body><div class="wrap">
"""

FOOT = """<div class="foot">openurl · 房间 __ROOM__</div></div>__SCRIPT__</body></html>"""


def page(title, room, inner, script=""):
    return (HEAD.replace("__TITLE__", title).replace("__CSS__", CSS)
            + inner
            + FOOT.replace("__ROOM__", room).replace("__SCRIPT__", script))


# --------------------------- 车机端页面 ------------------------------------
def build_car_page(room):
    inner = """
<div class="card">
  <h1>🚗 车机接收端</h1>
  <div class="dim">房间号</div>
  <div class="room">{room}</div>
  <div class="center"><span class="badge" id="status">⏳ 等待连接</span> &nbsp; <span class="badge">📱 手机 <b id="phones">0</b></span> <span class="badge" id="conn" style="display:none">连接中…</span></div>
</div>

<div class="card qr">
  <div class="dim">手机扫码进入本房间</div>
  <img id="qrimg" alt="二维码" width="200" height="200">
  <div class="dim" style="margin-top:8px">房间号 <b>{room}</b></div>
</div>

<div class="card">
  <h1 style="font-size:16px">📥 接收记录</h1>
  <ul class="list" id="list"></ul>
</div>

<div id="toast" class="toast"></div>
""".format(room=room)

    script = """
<script>
const ROOM="__ROOM__", POLL_MS=__POLL__, LS_KEY="room_"+ROOM+"_history", LS_ROOM="openurl_room";
function loadHist(){try{return JSON.parse(localStorage.getItem(LS_KEY)||"[]")}catch(e){return []}}
function saveHist(h){localStorage.setItem(LS_KEY,JSON.stringify(capHist(h)))}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c]})}
function fmt(ts){const d=new Date(ts*1000),p=n=>String(n).padStart(2,"0");return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds())}
function newId(){return Math.random().toString(36).slice(2)+Date.now().toString(36)}
function withIds(h){h.forEach(function(it){if(!it.id)it.id=newId()});return h}
// 超长 URL 截断显示（不影响真实链接）
function shortUrl(u,n){
  u=String(u||""); n=n||48;
  if(u.length<=n) return u;
  const head=Math.ceil((n-3)*0.6), tail=n-3-head;
  return u.slice(0,head)+"…"+u.slice(u.length-tail);
}
// 去重：同一 URL 只保留一条；pin 取或运算，time 取最新
function dedup(h){
  const map={},res=[];
  h.forEach(function(it){
    const k=String(it.url||"");
    if(!k)return;
    if(map[k]===undefined){map[k]=res.length;res.push({id:it.id||newId(),url:it.url,time:it.time||0,pin:!!it.pin})}
    else{const t=res[map[k]];if((it.time||0)>(t.time||0)){t.time=it.time;t.id=it.id||t.id}if(it.pin)t.pin=true}
  });
  return res;
}
const MAX_HIST=500;
// 超上限时淘汰最旧的，置顶优先保留
function capHist(h){
  h=dedup(withIds(h));
  if(h.length<=MAX_HIST) return h;
  h.sort(function(a,b){return (b.pin?1:0)-(a.pin?1:0)||b.time-a.time});
  return h.slice(0,MAX_HIST);
}
function sorted(h){h=dedup(withIds(h));h.sort(function(a,b){return (b.pin?1:0)-(a.pin?1:0)||b.time-a.time});return h}
function render(){
  const h=sorted(loadHist());
  const ul=document.getElementById("list");
  if(!h.length){ul.innerHTML='<li class="dim center" style="padding:16px">暂无记录，用手机扫码发送网址</li>';return}
  ul.innerHTML=h.map(function(it,i){
    return '<li class="item'+(it.pin?' pin':'')+'">'
      +'<div class="u"><a href="'+esc(it.url)+'" target="_blank" rel="noreferrer" title="'+esc(it.url)+'">'+esc(shortUrl(it.url,52))+'</a>'
      +'<div class="t">'+fmt(it.time)+(it.pin?' · 已置顶':'')+'</div></div>'
      +'<button class="ib" onclick="pin('+i+')">'+(it.pin?'取消置顶':'置顶')+'</button>'
      +'<button class="ib d" onclick="del('+i+')">删除</button></li>';
  }).join("");
}
function pin(idx){const s=sorted(loadHist()),it=s[idx];if(!it)return;const all=dedup(withIds(loadHist()));const t=all.find(x=>x.url===it.url)||it;t.pin=!t.pin;saveHist(all);render()}
function del(idx){const s=sorted(loadHist()),it=s[idx];if(!it)return;saveHist(dedup(loadHist()).filter(x=>x.url!==it.url));render()}
function setConn(bad){const e=document.getElementById("conn");e.textContent=bad?"连接中断，重试中…":"已连接";e.style.color=bad?"#ff453a":"#34c759"}
function setPhones(n){
  document.getElementById("phones").textContent=n;
  const s=document.getElementById("status");
  if(n>0){ s.textContent="✅ 已连接"; s.style.background="#1f3d24"; s.style.color="#34c759"; }
  else   { s.textContent="⏳ 等待连接"; s.style.background="#3d1f1f"; s.style.color="#ff6b6b"; }
}
function flash(t){const e=document.getElementById("toast");e.textContent=t;e.style.display="block";setTimeout(function(){e.style.display="none"},1800)}
let since=0,failN=0;
function poll(){
  fetch("openurl.cgi?action=poll&room="+ROOM+"&since="+since)
   .then(function(r){return r.json()})
   .then(function(d){
     failN=0;setConn(false);
     if(!d||d.status!=="ok")return;
     setPhones((d.data.room_status&&d.data.room_status.phone_count)||0);
     const msgs=(d.data.msgs)||[];
     if(msgs.length){
       since=Math.max.apply(null,msgs.map(function(m){return m.seq}));
       let h=withIds(loadHist()),jump=null;
       msgs.forEach(function(m){
         if(m.type==="url"&&m.url){h.push({id:newId(),url:m.url,time:m.time,pin:false});jump=m.url}
         if(m.type==="join"){flash("有手机加入房间")}
       });
       saveHist(dedup(h));render();
       if(jump){setTimeout(function(){location.href=jump},300)}
     }
   })
   .catch(function(){failN++;setConn(failN>2)})
   .finally(function(){setTimeout(poll,POLL_MS)});
}
localStorage.setItem(LS_ROOM,ROOM);
document.getElementById("qrimg").src="openurl.cgi?action=qr&room="+ROOM+"&t="+Date.now();
render();setConn(false);setPhones(0);poll();
</script>
""".replace("__ROOM__", room).replace("__POLL__", str(POLL_INTERVAL_MS))

    return page("车机 · 房间 " + room, room, inner, script)


# --------------------------- 手机端页面 ------------------------------------
def build_phone_page(room):
    inner = """
<div class="card">
  <h1>📱 发送网址到车机</h1>
  <div class="dim">当前房间</div>
  <div class="room">{room}</div>
</div>

<div class="card">
  <input type="url" id="url" placeholder="粘贴或输入网址，如 https://..." autocomplete="off" inputmode="url">
  <button class="big" id="btn" onclick="send()">发送到车机</button>
  <div class="dim center" style="margin-top:8px">车机会自动打开这个网址</div>
</div>

<div class="card">
  <h1 style="font-size:16px">📚 历史发送记录</h1>
  <div class="dim" style="margin-bottom:6px">本机保存，不依赖房间号；重复网址自动合并</div>
  <ul class="list" id="log"><li class="dim center" style="padding:12px">还没有发送记录</li></ul>
</div>

<div id="toast" class="toast"></div>
""".format(room=room)

    script = """
<script>
const ROOM="__ROOM__";
const LS_HIST="openurl_send_history";
function fmt(ts){const d=new Date((ts||0)*1000),p=n=>String(n).padStart(2,"0");return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds())}
function newId(){return Math.random().toString(36).slice(2)+Date.now().toString(36)}
function loadHist(){try{return JSON.parse(localStorage.getItem(LS_HIST)||"[]")}catch(e){return []}}
function saveHist(h){localStorage.setItem(LS_HIST,JSON.stringify(capHist(h)))}
// 去重：同一 URL 只留一条；pin 取或运算，time 取最新
function dedup(h){
  const map={},res=[];
  h.forEach(function(it){
    const k=String(it.url||"");
    if(!k)return;
    if(map[k]===undefined){map[k]=res.length;res.push({id:it.id||newId(),url:it.url,time:it.time||0,pin:!!it.pin})}
    else{const t=res[map[k]];if((it.time||0)>(t.time||0)){t.time=it.time;t.id=it.id||t.id}if(it.pin)t.pin=true}
  });
  return res;
}
const MAX_HIST=500;
// 超上限时淘汰最旧的，置顶优先保留
function capHist(h){
  h=dedup(h);
  if(h.length<=MAX_HIST) return h;
  h.sort(function(a,b){return (b.pin?1:0)-(a.pin?1:0)||b.time-a.time});
  return h.slice(0,MAX_HIST);
}
function sorted(h){h=dedup(h);h.sort(function(a,b){return (b.pin?1:0)-(a.pin?1:0)||b.time-a.time});return h}
function render(){
  const h=sorted(loadHist());
  const ul=document.getElementById("log");
  if(!h.length){ul.innerHTML='<li class="dim center" style="padding:12px">还没有发送记录</li>';return}
  ul.innerHTML=h.map(function(it,i){
    return '<li class="item'+(it.pin?' pin':'')+'">'
      +'<div class="u"><div class="rowtop"><a href="'+esc(it.url)+'" target="_blank" rel="noreferrer" title="'+esc(it.url)+'">'+esc(shortUrl(it.url,34))+'</a>'
      +'<button class="pinicon'+(it.pin?' on':'')+'" onclick="pin('+i+')" title="'+(it.pin?'取消置顶':'置顶')+'">'+(it.pin?'\u2605':'\u2606')+'</button></div>'
      +'<div class="t">'+fmt(it.time)+'</div>'
      +'<div class="acts">'
      +'<button class="ib s" onclick="resend('+i+')">发送</button>'
      +'<button class="ib d" onclick="del('+i+')">删除</button>'
      +'</div></div></li>';
  }).join("");
}
function resend(idx){const s=sorted(loadHist()),it=s[idx];if(!it)return;doSend(it.url)}
function pin(idx){const s=sorted(loadHist()),it=s[idx];if(!it)return;const all=dedup(loadHist());const t=all.find(x=>x.url===it.url)||it;t.pin=!t.pin;saveHist(all);render()}
function del(idx){const s=sorted(loadHist()),it=s[idx];if(!it)return;saveHist(dedup(loadHist()).filter(x=>x.url!==it.url));render()}
function addHist(u){
  const h=loadHist();
  h.push({id:newId(),url:u,time:Math.floor(Date.now()/1000),pin:false});
  saveHist(dedup(h));render();
}
function tip(t,ok){const e=document.getElementById("toast");e.textContent=t;e.style.background=ok?"#1f5f2f":"#5f1f1f";e.style.display="block";setTimeout(function(){e.style.display="none"},1800)}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c]})}
function shortUrl(u,n){
  u=String(u||""); n=n||32;
  if(u.length<=n) return u;
  const head=Math.ceil((n-3)*0.55), tail=n-3-head;
  return u.slice(0,head)+"…"+u.slice(u.length-tail);
}
function send(){
  doSend(document.getElementById("url").value.trim());
}
function doSend(u){
  if(!u)return;
  if(!/^https?:\\/\\//i.test(u))u="https://"+u;
  const btn=document.getElementById("btn");if(btn){btn.disabled=true;btn.textContent="发送中…"}
  fetch("openurl.cgi?action=send&room="+ROOM,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"url="+encodeURIComponent(u)})
   .then(function(r){return r.json()})
   .then(function(d){
      if(btn){btn.disabled=false;btn.textContent="发送到车机"}
      if(d&&d.status==="ok"){tip("已发送 ✅",true);const inp=document.getElementById("url");if(inp)inp.value="";addHist(u)}
      else tip("发送失败："+((d&&d.message)||""),false);
   })
   .catch(function(){if(btn){btn.disabled=false;btn.textContent="发送到车机"}tip("网络错误",false)});
}
// 心跳：让车机端显示手机数量（用稳定的 pid 标识本手机）
const PID=(function(){try{var k="openurl_pid";var v=localStorage.getItem(k);if(!v){v=Math.random().toString(36).slice(2)+Date.now().toString(36);localStorage.setItem(k,v)}return v}catch(e){return "anon"}})();
function beat(){fetch("openurl.cgi?action=join&room="+ROOM+"&pid="+encodeURIComponent(PID),{method:"POST"}).catch(function(){})}
beat();setInterval(beat,5000);
// 页面隐藏/关闭时主动通知离开（即时归零）
function leave(){try{navigator.sendBeacon("openurl.cgi?action=leave&room="+ROOM+"&pid="+encodeURIComponent(PID))}catch(e){fetch("openurl.cgi?action=leave&room="+ROOM+"&pid="+encodeURIComponent(PID),{method:"POST",keepalive:true}).catch(function(){})}}
window.addEventListener("pagehide",leave);
render();
</script>
""".replace("__ROOM__", room)

    return page("手机发送 · 房间 " + room, room, inner, script)


# --------------------------- 主流程 ----------------------------------------
def main():
    ensure_room_dir()
    cleanup_rooms()

    q = get_query()
    action = q.get("action", "")
    room = q.get("room", "").strip()
    qs = os.environ.get("QUERY_STRING", "")
    # 不再依赖 UA：m=1 为手机模式，其余（含无参数）均为车机模式
    is_phone = (q.get("m") == "1")

    # ---------- API ----------
    if action == "qr":
        if not valid_room(room):
            out_json({"status": "error", "message": "房间号无效"}, 400)
        # 扫码后进入手机端。用请求自身的 host/scheme，保证内外网都可用。
        host = (os.environ.get("HTTP_X_FORWARDED_HOST", "").split(",")[0].strip()
                or os.environ.get("HTTP_HOST", "").strip()
                or default_public_host()
                or "localhost")
        # 代理一般用 X-Forwarded-Proto，部分老代理用 X-Forwarded-SSL: on
        proto = os.environ.get("HTTP_X_FORWARDED_PROTO", "").split(",")[0].strip()
        if not proto and os.environ.get("HTTP_X_FORWARDED_SSL", "").strip().lower() in ("on", "1", "https"):
            proto = "https"
        if not proto:
            if os.environ.get("HTTPS", "").strip().lower() == "on":
                proto = "https"
            elif want_https(host):
                # 命中 OPENURL_HTTPS_HOSTS 的主机始终走 https
                proto = "https"
            else:
                proto = "http"
        # 动态获取 CGI 自身路径（如 /api/openurl.cgi），兼容反向代理路径。
        self_path = os.environ.get("SCRIPT_NAME", "") or "/api/openurl.cgi"
        target = "%s://%s%s?room=%s&m=1" % (proto, host, self_path, room)
        try:
            out_bytes(make_qr_png(target), "image/png")
        except Exception as e:
            out_json({"status": "error", "message": "QR 生成失败: " + str(e)}, 500)
        sys.exit(0)

    if action == "send":
        if not valid_room(room):
            out_json({"status": "error", "message": "房间号无效"}, 400)
        p = body_params()
        url = (p.get("url") or "").strip()
        if not url:
            out_json({"status": "error", "message": "缺少 url"}, 400)
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        msg = append_msg(room, {"type": "url", "url": url})
        out_json({"status": "ok", "data": {"seq": msg["seq"], "time": msg["time"]}})

    if action == "join":
        if not valid_room(room):
            out_json({"status": "error", "message": "房间号无效"}, 400)
        d = get_or_create_room(room)
        pid = q.get("pid") or (body_params().get("pid") or "")
        if not pid:
            pid = os.environ.get("REMOTE_ADDR", "?") + "|" + os.environ.get("HTTP_USER_AGENT", "")[:40]
        d.setdefault("phones", {})
        d["phones"][pid] = now_ts()
        # 过期手机清理（30 秒内无心跳则移除）
        d["phones"] = {k: v for k, v in d["phones"].items() if now_ts() - int(v) <= 30}
        save_room(room, d)
        out_json({"status": "ok", "data": {"phone_count": active_phone_count(d)}})

    if action == "leave":
        if not valid_room(room):
            out_json({"status": "error", "message": "房间号无效"}, 400)
        d = get_or_create_room(room)
        pid = q.get("pid") or (body_params().get("pid") or "")
        if pid:
            d.get("phones", {}).pop(pid, None)
            save_room(room, d)
        out_json({"status": "ok", "data": {"phone_count": active_phone_count(d)}})

    if action == "poll":
        if not valid_room(room):
            out_json({"status": "error", "message": "房间号无效"}, 400)
        try:
            since = int(q.get("since", 0))
        except Exception:
            since = 0
        d = get_or_create_room(room)
        new_msgs = [m for m in d.get("msgs", []) if int(m.get("seq", 0)) > since]
        if new_msgs:
            d["msgs"] = []  # 取走即删：服务器不存储
        save_room(room, d)
        out_json({"status": "ok", "data": {
            "msgs": new_msgs,
            "seq": d.get("seq", 0),
            "room_status": {"phone_count": active_phone_count(d)},
        }})

    # ---------- 页面 ----------
    if q.get("new") == "1":
        room = gen_room()
        get_or_create_room(room)
        out_html('<meta http-equiv="refresh" content="0;url=openurl.cgi?room=%s">' % room)
        return

    if not room:
        # 车机入口：先看浏览器缓存房间号，有则跳入；否则新建房间
        out_html("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/openclaw/pic/satellite-dish.svg">
<title>openurl</title></head><body>
<script>
(function(){
  var cached=null;
  try{ cached=localStorage.getItem("openurl_room"); }catch(e){}
  if(cached && /^\\d{4}$/.test(cached)){
    location.replace("openurl.cgi?room="+cached);
  }else{
    location.replace("openurl.cgi?new=1");
  }
})();
</script>
<noscript><a href="openurl.cgi?new=1">进入</a></noscript>
</body></html>""")
        return

    if not valid_room(room):
        out_html("<div class='wrap'><h3>房间号无效（需 4 位数字）</h3><a href='openurl.cgi'>返回</a></div>", 400)
        return

    get_or_create_room(room)
    if is_phone:
        out_html(build_phone_page(room))
    else:
        out_html(build_car_page(room))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        out_json({"status": "error", "message": str(e)}, 500)
