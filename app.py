# -*- coding: utf-8 -*-
"""
Ferpoks WhatsApp AI — لوحة تحكم متكاملة للتاجر

• FastAPI + SQLite (MVP) — تعمل على Render
• تتضمن: /dashboard واجهة كاملة + API لحفظ الإعدادات والقوالب وإرسال اختبار
• تدعم تعدد المتاجر (Multi-tenant) عبر store_id (sid)
• جاهزة للوضع الإنتاجي الخفيف — يفضّل الترقية لاحقًا إلى Postgres

طريقة التشغيل على Render:
uvicorn app:app --host 0.0.0.0 --port 10000

متغيرات البيئة:
APP_URL, DB_PATH, SALLA_* ، WABA_TOKEN (اختياري لكل متجر)، WABA_PHONE_ID (اختياري لكل متجر)
"""
import os, json, time, hmac, hashlib, sqlite3, secrets
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse

# ================= إعدادات عامة =================
APP_URL           = os.getenv("APP_URL", "https://your-app.onrender.com").rstrip("/")
DB_PATH           = os.getenv("DB_PATH", "/var/data/salla_bot.db")

SALLA_CLIENT_ID   = os.getenv("SALLA_CLIENT_ID", "")
SALLA_CLIENT_SEC  = os.getenv("SALLA_CLIENT_SECRET", "")
SALLA_AUTH_URL    = os.getenv("SALLA_AUTH_URL", "https://accounts.salla.sa/oauth2/authorize")
SALLA_TOKEN_URL   = os.getenv("SALLA_TOKEN_URL", "https://accounts.salla.sa/oauth2/token")
SALLA_API_BASE    = os.getenv("SALLA_API_BASE", "https://api.salla.dev/admin")  # عدّل حسب بيئتك
SALLA_WEBHOOK_SEC = os.getenv("SALLA_WEBHOOK_SECRET", "change-me")

# WhatsApp Cloud API — يمكن لكل متجر تعيين مفاتيحه داخل اللوحة
GLOBAL_WABA_TOKEN    = os.getenv("WABA_TOKEN", "")
GLOBAL_WABA_PHONE_ID = os.getenv("WABA_PHONE_ID", "")
WABA_API_BASE        = os.getenv("WABA_API_BASE", "https://graph.facebook.com/v20.0")

# ================= قاعدة البيانات =================
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur  = conn.cursor()

cur.execute("""CREATE TABLE IF NOT EXISTS merchants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT UNIQUE,
  store_domain TEXT,
  access_token TEXT,
  refresh_token TEXT,
  token_expires_at INTEGER,
  waba_token TEXT,
  waba_phone_id TEXT,
  plan TEXT DEFAULT 'basic',
  plan_until INTEGER,
  created_at INTEGER
)""")

cur.execute("""CREATE TABLE IF NOT EXISTS store_settings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT,
  settings_json TEXT,
  updated_at INTEGER,
  UNIQUE(store_id)
)""")

cur.execute("""CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT,
  tkey TEXT,
  display_name TEXT,
  body TEXT,
  UNIQUE(store_id, tkey)
)""")

cur.execute("""CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT,
  event_type TEXT,
  payload TEXT,
  created_at INTEGER
)""")

cur.execute("""CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT,
  to_msisdn TEXT,
  template TEXT,
  status TEXT,
  error TEXT,
  created_at INTEGER
)""")

conn.commit()

def now() -> int:
    return int(time.time())

# =============== أدوات مساعدة ===================
DEFAULT_SETTINGS = {
    "enabled": {
        "order_created": True,
        "order_paid": True,
        "order_fulfilled": True,
        "out_for_delivery": True,
        "delivered": True,
        "order_canceled": True,
        "refund_created": True,
    },
    "rate_limit_mps": 60,  # الحد الأقصى للرسائل بالثانية لكل متجر (سيُحترم منطقيًا)
}

DEFAULT_TEMPLATES = [
    {"tkey": "order_created", "display_name": "تم إنشاء الطلب", "body": "يا {name} 🎉 تم استلام طلبك #{order_no}. رابط الطلب: {order_url}"},
    {"tkey": "order_paid", "display_name": "تم الدفع", "body": "يا {name} ✨ تم تأكيد دفع طلبك #{order_no} ✅ سنطلعك على حالة الشحن أولاً بأول."},
    {"tkey": "order_fulfilled", "display_name": "تم الشحن", "body": "يا {name} 📦 تم شحن طلبك #{order_no}. رقم التتبع: {tracking_no}"},
    {"tkey": "out_for_delivery", "display_name": "خارج للتسليم", "body": "يا {name} 🚚 طلبك #{order_no} في الطريق إليك."},
    {"tkey": "delivered", "display_name": "تم التسليم", "body": "يا {name} ✅ تم تسليم طلبك #{order_no}. نتمنى لك تجربة ممتعة."},
    {"tkey": "order_canceled", "display_name": "تم الإلغاء", "body": "يؤسفنا إبلاغك بإلغاء طلبك #{order_no}. لأي استفسار نحن هنا دائمًا."},
    {"tkey": "refund_created", "display_name": "استرجاع", "body": "تم فتح طلب استرجاع للطلب #{order_no}. سيتواصل فريقنا معك بالتفاصيل."},
]

PLACEHOLDERS = ["{name}", "{order_no}", "{order_url}", "{tracking_no}"]

async def http_post(url: str, headers: dict = None, data=None, json_=None):
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(url, headers=headers, data=data, json=json_)
        r.raise_for_status()
        return r

async def http_get(url: str, headers: dict = None):
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r

# =============== OAuth مع سلة (مختصر) =================
app = FastAPI(title="Ferpoks WhatsApp AI – Salla App")

@app.get("/install")
def install():
    scopes = "read_orders read_customers webhooks"  # ابدأ بالأدنى
    redirect_uri = f"{APP_URL}/callback"
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": SALLA_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": scopes,
        "state": state,
    }
    return RedirectResponse(f"{SALLA_AUTH_URL}?{urlencode(params)}")

@app.get("/callback")
async def callback(code: Optional[str] = None, state: Optional[str] = None):
    if not code:
        raise HTTPException(400, "Missing code")
    data = {
        "grant_type": "authorization_code",
        "client_id": SALLA_CLIENT_ID,
        "client_secret": SALLA_CLIENT_SEC,
        "redirect_uri": f"{APP_URL}/callback",
        "code": code,
    }
    r = await http_post(SALLA_TOKEN_URL, data=data)
    tok = r.json()
    access_tok  = tok.get("access_token")
    refresh_tok = tok.get("refresh_token")
    exp         = now() + int(tok.get("expires_in", 3600))

    # TODO: اجلب بيانات المتجر من Salla API وضع store_id الحقيقي
    store_id = f"store-{hash(access_tok)%999999}"
    store_domain = "example.salla.sa"

    cur.execute("""
        INSERT OR REPLACE INTO merchants (store_id, store_domain, access_token, refresh_token, token_expires_at, created_at)
        VALUES (?,?,?,?,?,?)
    """, (store_id, store_domain, access_tok, refresh_tok, exp, now()))
    conn.commit()

    ensure_defaults(store_id)

    return RedirectResponse(f"/dashboard?sid={store_id}")

# =============== Webhook من سلة =====================

@app.post("/webhook")
async def webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("X-Signature", "")  # قد يختلف اسم الهيدر
    if SALLA_WEBHOOK_SEC:
        digest = hmac.new(SALLA_WEBHOOK_SEC.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, sig or ""):
            raise HTTPException(401, "Invalid signature")

    payload = await request.json()
    event_type = payload.get("event") or payload.get("type", "unknown")
    store_id   = payload.get("store_id", "unknown")

    cur.execute("INSERT INTO events (store_id, event_type, payload, created_at) VALUES (?,?,?,?)",
                (store_id, event_type, json.dumps(payload, ensure_ascii=False), now()))
    conn.commit()

    # يمكنك هنا وضع منطق الإرسال شبه الفوري (مع محدد سرعات لكل متجر)
    # للاختصار سيُدار الإرسال عبر /api/test-send عند التجربة
    return JSONResponse({"ok": True})

# =============== وظائف البيانات =====================

def ensure_defaults(store_id: str):
    # إعدادات
    row = cur.execute("SELECT settings_json FROM store_settings WHERE store_id=?", (store_id,)).fetchone()
    if not row:
        cur.execute("INSERT OR IGNORE INTO store_settings (store_id, settings_json, updated_at) VALUES (?,?,?)",
                    (store_id, json.dumps(DEFAULT_SETTINGS, ensure_ascii=False), now()))
        conn.commit()
    # قوالب
    for t in DEFAULT_TEMPLATES:
        cur.execute("INSERT OR IGNORE INTO templates (store_id, tkey, display_name, body) VALUES (?,?,?,?)",
                    (store_id, t["tkey"], t["display_name"], t["body"]))
    conn.commit()


def get_store(sid: Optional[str]) -> Optional[sqlite3.Row]:
    if sid:
        return cur.execute("SELECT * FROM merchants WHERE store_id=?", (sid,)).fetchone()
    # لو ما فيه sid وجِد متجر واحد فقط
    row = cur.execute("SELECT * FROM merchants ORDER BY id DESC LIMIT 2").fetchall()
    if not row:
        return None
    if len(row) == 1:
        return row[0]
    return None  # أكثر من متجر — نطلب sid

# =============== إرسال واتساب =======================
async def send_whatsapp_text(waba_token: str, waba_phone_id: str, to_msisdn: str, body: str) -> Dict[str, Any]:
    url = f"{WABA_API_BASE}/{waba_phone_id}/messages"
    headers = {"Authorization": f"Bearer {waba_token}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_msisdn,
        "type": "text",
        "text": {"preview_url": False, "body": body}
    }
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(url, headers=headers, json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {"text": await resp.aread()}
        return {"status": resp.status_code, "data": data}

# =============== واجهة HTML (Dashboard) =============
BASE_STYLE = """
<style>
  body{font-family:system-ui,Segoe UI,Arial;background:#0f172a;color:#e2e8f0;margin:0}
  a{color:#67e8f9}
  .wrap{max-width:1100px;margin:0 auto;padding:24px}
  .grid{display:grid;gap:16px}
  .g2{grid-template-columns:1fr 1fr}
  .card{background:#111827;border:1px solid #334155;border-radius:16px;padding:18px}
  input,select,textarea{width:100%;padding:10px;border-radius:12px;border:1px solid #334155;background:#0b1220;color:#e2e8f0}
  label{display:block;margin:8px 0 6px;color:#cbd5e1}
  .btn{display:inline-block;padding:10px 16px;border-radius:12px;background:#22d3ee;color:#0f172a;font-weight:700;text-decoration:none;border:none;cursor:pointer}
  .btn-outline{background:transparent;color:#e2e8f0;border:1px solid #334155}
  .switch{display:flex;align-items:center;gap:8px;margin:6px 0}
  .muted{color:#94a3b8}
  .badge{display:inline-block;padding:4px 10px;border-radius:999px;background:#0ea5e9;color:#0f172a;font-weight:700}
  .row{display:flex;gap:10px;align-items:center}
  .error{color:#fca5a5}
</style>
"""

DASHBOARD_HTML = """
<!doctype html><html lang='ar' dir='rtl'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>لوحة Ferpoks WhatsApp AI</title>
{STYLE}
</head><body>
  <main class='wrap'>
    <h1>لوحة Ferpoks WhatsApp AI</h1>
    <p class='muted'>اضبط مفاتيح واتساب، فعّل/عطّل الإشعارات، وعدّل القوالب. استخدم <span class='badge'>تجربة الإرسال</span> للتحقق فورًا.</p>

    <div id='storeBar' class='card row'>
      <div>المتجر: <b id='storeId'>—</b> • الخطة: <span id='plan' class='badge'>—</span></div>
      <div style='flex:1'></div>
      <button class='btn-outline' onclick='reload()'>تحديث</button>
    </div>

    <div class='grid g2'>
      <section class='card'>
        <h3>إعدادات واتساب (لكل متجر)</h3>
        <label>WABA Token</label>
        <input id='waba_token' type='password' placeholder='EAAG...'>
        <label>WABA Phone ID</label>
        <input id='waba_phone_id' placeholder='123456789012345'>
        <div class='row' style='margin-top:10px'>
          <button class='btn' onclick='saveWaba()'>حفظ</button>
          <span id='waba_msg' class='muted'></span>
        </div>
      </section>

      <section class='card'>
        <h3>تجربة الإرسال</h3>
        <label>رقم المرسل إليه (صيغة دولية، مثال 9665XXXXXXXX)</label>
        <input id='test_msisdn' placeholder='9665XXXXXXXX'>
        <label>اختر القالب</label>
        <select id='test_template'></select>
        <label>معاينة الرسالة (يتم استبدال المتغيرات القابلة)</label>
        <textarea id='preview' rows='5' placeholder='المعاينة ستظهر هنا'></textarea>
        <div class='row' style='margin-top:10px'>
          <button class='btn' onclick='sendTest()'>إرسال اختبار</button>
          <span id='test_msg' class='muted'></span>
        </div>
      </section>
    </div>

    <section class='card'>
      <h3>الإشعارات المفعّلة</h3>
      <div id='switches' class='grid g2'></div>
      <div class='row' style='margin-top:10px'>
        <button class='btn' onclick='saveSettings()'>حفظ الإعدادات</button>
        <span id='settings_msg' class='muted'></span>
      </div>
      <p class='muted'>المتغيرات المدعومة في القوالب: {name} ، {order_no} ، {order_url} ، {tracking_no}</p>
    </section>

    <section class='card'>
      <h3>القوالب</h3>
      <div id='templates'></div>
      <div class='row' style='margin-top:10px'>
        <button class='btn' onclick='saveTemplates()'>حفظ القوالب</button>
        <span id='tpl_msg' class='muted'></span>
      </div>
    </section>

    <footer class='muted' style='margin-top:24px'>© Ferpoks 2025 • <a href='/privacy'>الخصوصية</a> • <a href='/terms'>الشروط</a> • <a href='/support'>الدعم</a></footer>
  </main>

<script>
  const qs = new URLSearchParams(location.search);
  const sid = qs.get('sid') || '';

  const S = {
    settings: null,
    templates: [],
    store: null,
  };

  async function api(path, method='GET', body=null){
    const opt = {method, headers:{'Content-Type':'application/json'}};
    if(body) opt.body = JSON.stringify(body);
    const r = await fetch(path + (path.includes('?')?'&':'?') + new URLSearchParams({sid}), opt);
    if(!r.ok){
      const t = await r.text();
      throw new Error('HTTP '+r.status+': '+t);
    }
    return r.json();
  }

  function el(tag, attrs={}, children=[]) {
    const e = document.createElement(tag);
    for (const k in attrs) {
      if (k === 'class') e.className = attrs[k]; else if(k==='html') e.innerHTML = attrs[k]; else e.setAttribute(k, attrs[k]);
    }
    children.forEach(c => e.appendChild(c));
    return e;
  }

  function renderSwitches(){
    const container = document.getElementById('switches');
    container.innerHTML = '';
    const m = S.settings.enabled;
    const items = [
      ['order_created','تم إنشاء الطلب'],
      ['order_paid','تم الدفع'],
      ['order_fulfilled','تم الشحن'],
      ['out_for_delivery','خارج للتسليم'],
      ['delivered','تم التسليم'],
      ['order_canceled','تم الإلغاء'],
      ['refund_created','استرجاع']
    ];
    items.forEach(([key,label])=>{
      const id = 'sw_'+key;
      const row = el('div',{class:'switch'},[
        el('input',{type:'checkbox', id, ...(m[key]?{checked:true}:{})}),
        el('label',{for:id, html:label})
      ]);
      container.appendChild(row);
    });
  }

  function renderTemplates(){
    const box = document.getElementById('templates');
    box.innerHTML = '';
    S.templates.forEach((t,i)=>{
      const wrap = el('div',{class:'card'},[
        el('div',{class:'row'},[
          el('div',{html:`<b>${t.display_name}</b> <span class='muted'>(key: ${t.tkey})</span>`}),
        ]),
        el('label',{html:'النص'}),
        el('textarea',{rows:'3', id:'tpl_'+i},[]),
      ]);
      box.appendChild(wrap);
      setTimeout(()=>{ document.getElementById('tpl_'+i).value = t.body; }, 0);
    });
  }

  function currentTemplateBody(){
    const sel = document.getElementById('test_template');
    const t = S.templates.find(x=>x.tkey===sel.value);
    return t? t.body: '';
  }

  function updatePreview(){
    let body = currentTemplateBody();
    body = body.replaceAll('{name}','عميلنا').replaceAll('{order_no}','12345').replaceAll('{order_url}','https://salla.sa/orders/12345').replaceAll('{tracking_no}','TRK123');
    document.getElementById('preview').value = body;
  }

  async function loadAll(){
    const info = await api('/api/store');
    S.store = info.store;
    document.getElementById('storeId').textContent = S.store? S.store.store_id : '—';
    document.getElementById('plan').textContent = S.store? (S.store.plan || 'basic') : '—';

    const set = await api('/api/settings');
    S.settings = set.settings;
    renderSwitches();

    const tpls = await api('/api/templates');
    S.templates = tpls.templates;
    renderTemplates();

    // تعبئة حقول واتساب
    document.getElementById('waba_token').value = (S.store && S.store.waba_token) ? S.store.waba_token : '';
    document.getElementById('waba_phone_id').value = (S.store && S.store.waba_phone_id) ? S.store.waba_phone_id : '';

    // قائمة القوالب للتجربة
    const sel = document.getElementById('test_template');
    sel.innerHTML = '';
    S.templates.forEach(t=>{
      const o = document.createElement('option');
      o.value = t.tkey; o.textContent = t.display_name;
      sel.appendChild(o);
    });
    sel.addEventListener('change', updatePreview);
    updatePreview();
  }

  async function saveWaba(){
    const token = (document.getElementById('waba_token').value||'').trim();
    const pid   = (document.getElementById('waba_phone_id').value||'').trim();
    try{
      await api('/api/waba','POST',{waba_token:token, waba_phone_id:pid});
      document.getElementById('waba_msg').textContent = 'تم الحفظ ✅';
    }catch(e){
      document.getElementById('waba_msg').textContent = 'خطأ: '+e.message;
    }
  }

  async function saveSettings(){
    const m = S.settings.enabled;
    const keys = Object.keys(m);
    keys.forEach(k=>{
      const elx = document.getElementById('sw_'+k);
      if(elx) m[k] = !!elx.checked;
    });
    try{
      await api('/api/settings','POST',{enabled:m, rate_limit_mps: S.settings.rate_limit_mps});
      document.getElementById('settings_msg').textContent='تم الحفظ ✅';
    }catch(e){
      document.getElementById('settings_msg').textContent='خطأ: '+e.message;
    }
  }

  async function saveTemplates(){
    const out = S.templates.map((t,i)=> ({tkey:t.tkey, display_name:t.display_name, body: document.getElementById('tpl_'+i).value}));
    try{
      await api('/api/templates','POST',{templates: out});
      document.getElementById('tpl_msg').textContent='تم الحفظ ✅';
    }catch(e){
      document.getElementById('tpl_msg').textContent='خطأ: '+e.message;
    }
  }

  async function sendTest(){
    const msisdn = (document.getElementById('test_msisdn').value||'').trim();
    const body   = (document.getElementById('preview').value||'').trim();
    if(!msisdn || !body){ document.getElementById('test_msg').textContent='يرجى إدخال رقم ومعاينة'; return; }
    try{
      const r = await api('/api/test-send','POST',{to_msisdn: msisdn, body});
      document.getElementById('test_msg').textContent = 'الحالة: '+r.status;
    }catch(e){
      document.getElementById('test_msg').textContent = 'خطأ: '+e.message;
    }
  }

  function reload(){ location.reload(); }

  loadAll().catch(err=>{
    document.getElementById('storeBar').insertAdjacentHTML('beforeend', `<span class='error'>${err.message}</span>`);
  });
</script>
</body></html>
""".replace("{STYLE}", BASE_STYLE)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        # لا يوجد متجر أو يوجد أكثر من متجر بدون sid
        html = f"""
        <!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>
        <meta name='viewport' content='width=device-width, initial-scale=1'>
        <title>اختر المتجر</title>{BASE_STYLE}</head><body>
        <main class='wrap'>
          <h2>لوحة Ferpoks — اختيار متجر</h2>
          <div class='card'>لا يوجد متجر مرتبط بعد، أو لديك عدة متاجر. اربط عبر <code>/install</code> أو مرر ?sid=STORE_ID</div>
        </main></body></html>
        """
        return HTMLResponse(html)
    ensure_defaults(store["store_id"])
    return HTMLResponse(DASHBOARD_HTML)

# =============== API للوحة ==========================
@app.get("/api/store")
async def api_store(sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        raise HTTPException(404, "Store not found. استخدم ?sid=...")
    return {"store": dict(store)}

@app.get("/api/settings")
async def api_get_settings(sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        raise HTTPException(404, "Store not found")
    row = cur.execute("SELECT settings_json FROM store_settings WHERE store_id=?", (store["store_id"],)).fetchone()
    settings = json.loads(row[0]) if row and row[0] else DEFAULT_SETTINGS
    return {"settings": settings}

@app.post("/api/settings")
async def api_save_settings(request: Request, sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        raise HTTPException(404, "Store not found")
    body = await request.json()
    enabled = body.get("enabled") or DEFAULT_SETTINGS["enabled"]
    rate_limit_mps = int(body.get("rate_limit_mps") or 60)
    settings = {"enabled": enabled, "rate_limit_mps": rate_limit_mps}
    cur.execute("INSERT OR REPLACE INTO store_settings (store_id, settings_json, updated_at) VALUES (?,?,?)",
                (store["store_id"], json.dumps(settings, ensure_ascii=False), now()))
    conn.commit()
    return {"ok": True}

@app.get("/api/templates")
async def api_get_templates(sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        raise HTTPException(404, "Store not found")
    rows = cur.execute("SELECT tkey, display_name, body FROM templates WHERE store_id=? ORDER BY id", (store["store_id"],)).fetchall()
    if not rows:
        ensure_defaults(store["store_id"])
        rows = cur.execute("SELECT tkey, display_name, body FROM templates WHERE store_id=? ORDER BY id", (store["store_id"],)).fetchall()
    templates = [dict(r) for r in rows]
    return {"templates": templates}

@app.post("/api/templates")
async def api_save_templates(request: Request, sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        raise HTTPException(404, "Store not found")
    body = await request.json()
    tpls: List[Dict[str, Any]] = body.get("templates") or []
    for t in tpls:
        tkey = t.get("tkey"); disp = t.get("display_name") or tkey; txt = t.get("body") or ""
        cur.execute("INSERT OR IGNORE INTO templates (store_id, tkey, display_name, body) VALUES (?,?,?,?)",
                    (store["store_id"], tkey, disp, txt))
        cur.execute("UPDATE templates SET display_name=?, body=? WHERE store_id=? AND tkey=?",
                    (disp, txt, store["store_id"], tkey))
    conn.commit()
    return {"ok": True}

@app.post("/api/waba")
async def api_save_waba(request: Request, sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        raise HTTPException(404, "Store not found")
    body = await request.json()
    wtok = body.get("waba_token") or ""
    wpid = body.get("waba_phone_id") or ""
    cur.execute("UPDATE merchants SET waba_token=?, waba_phone_id=? WHERE store_id=?",
                (wtok, wpid, store["store_id"]))
    conn.commit()
    return {"ok": True}

@app.post("/api/test-send")
async def api_test_send(request: Request, sid: Optional[str] = None):
    store = get_store(sid)
    if not store:
        raise HTTPException(404, "Store not found")
    body = await request.json()
    to_msisdn = (body.get("to_msisdn") or "").strip()
    msg_body  = (body.get("body") or "").strip()
    if not to_msisdn or not msg_body:
        raise HTTPException(400, "Missing to_msisdn/body")
    wtk = store["waba_token"] or GLOBAL_WABA_TOKEN
    wpid = store["waba_phone_id"] or GLOBAL_WABA_PHONE_ID
    if not wtk or not wpid:
        raise HTTPException(400, "WABA not configured for this store")
    res = await send_whatsapp_text(wtk, wpid, to_msisdn, msg_body)
    cur.execute("INSERT INTO logs (store_id, to_msisdn, template, status, error, created_at) VALUES (?,?,?,?,?,?)",
                (store["store_id"], to_msisdn, "manual_test", str(res.get("status")), json.dumps(res.get("data")), now()))
    conn.commit()
    return {"status": res.get("status"), "data": res.get("data")}

# صفحات عامة بسيطة
@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return HTMLResponse("""
    <!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>سياسة الخصوصية</title>"""+BASE_STYLE+"""
    </head><body><main class='wrap'><h2>سياسة الخصوصية</h2>
    <p>يجمع التطبيق بيانات لازمة لتقديم الإشعارات ولا يبيعها لطرف ثالث. تُخزّن المفاتيح بشكل آمن. للاستفسار: support@ferpoks.com.</p>
    </main></body></html>
    """)

@app.get("/terms", response_class=HTMLResponse)
async def terms():
    return HTMLResponse("""
    <!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>الشروط والأحكام</title>"""+BASE_STYLE+"""
    </head><body><main class='wrap'><h2>الشروط والأحكام</h2>
    <ul><li>الاشتراك شهري/سنوي عبر سلة، قابل للإلغاء.</li><li>تكلفة رسائل واتساب على مزوّد العميل.</li><li>الالتزام بسياسات سلة وواتساب.</li></ul>
    </main></body></html>
    """)

@app.get("/support", response_class=HTMLResponse)
async def support():
    return HTMLResponse("""
    <!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>الدعم</title>"""+BASE_STYLE+"""
    </head><body><main class='wrap'><h2>الدعم الفني</h2>
    <p>راسلنا على support@ferpoks.com أو تيليجرام @Ferpoks</p>
    </main></body></html>
    """)

@app.get("/health")
async def health():
    return {"ok": True, "ts": now()}

