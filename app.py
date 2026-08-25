"""
롤 명단 대시보드 (티어 · 챔피언 숙련도 랭킹)

구조:
  - 관리자만 사용자(라이엇 ID)를 명단에 등록/삭제 (Postgres DB에 저장)
  - 공개 페이지: 명단의 티어 랭킹 + 챔피언별 숙련도 랭킹
  - 티어/숙련도/아이콘은 Riot API에서 실시간 조회 후 메모리 캐싱

Riot API 라우팅 주의:
  - ACCOUNT-V1  → 지역 호스트(asia)      : 라이엇 ID → puuid
  - SUMMONER/LEAGUE/CHAMPION-MASTERY-V4 → 플랫폼 호스트(kr)

필요 환경변수 (Render 대시보드에서 설정):
  RIOT_API_KEY   : 라이엇 API 키
  DATABASE_URL   : Supabase Postgres 연결 문자열 (필요시 끝에 ?sslmode=require)
  ADMIN_PASSWORD : 관리자 비밀번호
  SECRET_KEY     : 세션 서명용 아무 랜덤 문자열

실행:
  pip install flask requests psycopg2-binary
  gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120
"""

import os
import time
import threading
from functools import wraps
from urllib.parse import quote

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, render_template_string, session, redirect

# ===================== 설정 =====================
RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
PLATFORM = "kr"      # summoner/league/mastery 호스트 (한국 서버)
REGION = "asia"      # account 호스트 (한국 계정)
CACHE_TTL = 600      # 선수 실시간 데이터 캐시 유지 시간(초)
SLEEP = 0.3          # Riot 요청 사이 대기(초)
# ===============================================

PLATFORM_HOST = f"https://{PLATFORM}.api.riotgames.com"
REGION_HOST = f"https://{REGION}.api.riotgames.com"

app = Flask(__name__)
app.secret_key = SECRET_KEY

_cache = {}
_lock = threading.Lock()
_ddragon = {"ts": 0, "version": None, "champions": {}}

TIER_ORDER = {"IRON": 0, "BRONZE": 1, "SILVER": 2, "GOLD": 3, "PLATINUM": 4,
              "EMERALD": 5, "DIAMOND": 6, "MASTER": 7, "GRANDMASTER": 8, "CHALLENGER": 9}
DIV_ORDER = {"IV": 0, "III": 1, "II": 2, "I": 3}


# ---------- DB ----------
def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    id         SERIAL PRIMARY KEY,
                    game_name  TEXT NOT NULL,
                    tag_line   TEXT NOT NULL,
                    puuid      TEXT UNIQUE NOT NULL,
                    added_at   TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (game_name, tag_line)
                );
            """)
    finally:
        conn.close()


try:
    if DATABASE_URL:
        init_db()
except Exception as e:
    print("init_db 실패(부팅은 계속):", e)


# ---------- Riot ----------
def riot_get(host, path, params=None):
    while True:
        r = requests.get(host + path, headers={"X-Riot-Token": RIOT_API_KEY},
                         params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 5)))
            continue
        r.raise_for_status()
        return r.json()


def _simplify_entry(e):
    if not e:
        return None
    return {"tier": e.get("tier"), "rank": e.get("rank"),
            "lp": e.get("leaguePoints", 0),
            "wins": e.get("wins", 0), "losses": e.get("losses", 0)}


def fetch_player_live(puuid):
    summ = riot_get(PLATFORM_HOST, f"/lol/summoner/v4/summoners/by-puuid/{puuid}")
    time.sleep(SLEEP)
    summoner_id = summ.get("id")
    solo = flex = None
    if summoner_id:
        entries = riot_get(PLATFORM_HOST, f"/lol/league/v4/entries/by-summoner/{summoner_id}")
        time.sleep(SLEEP)
        for e in entries:
            if e.get("queueType") == "RANKED_SOLO_5x5":
                solo = e
            elif e.get("queueType") == "RANKED_FLEX_SR":
                flex = e
    mastery = riot_get(PLATFORM_HOST, f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}")
    time.sleep(SLEEP)
    return {
        "level": summ.get("summonerLevel"),
        "iconId": summ.get("profileIconId"),
        "solo": _simplify_entry(solo),
        "flex": _simplify_entry(flex),
        "mastery": [{"championId": m["championId"], "points": m["championPoints"],
                     "level": m["championLevel"]} for m in mastery],
    }


def get_player_live(puuid):
    now = time.time()
    with _lock:
        c = _cache.get(puuid)
        if c and now - c[0] < CACHE_TTL:
            return c[1]
    data = fetch_player_live(puuid)
    with _lock:
        _cache[puuid] = (now, data)
    return data


def rank_score(entry):
    if not entry or not entry.get("tier"):
        return -1
    return (TIER_ORDER.get(entry["tier"], 0) * 100000
            + DIV_ORDER.get(entry.get("rank") or "I", 0) * 1000
            + entry.get("lp", 0))


# ---------- Data Dragon (정적 데이터: 챔피언 이름/이미지) ----------
def get_ddragon():
    now = time.time()
    if _ddragon["version"] and now - _ddragon["ts"] < 86400:
        return _ddragon
    ver = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10).json()[0]
    data = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/ko_KR/champion.json",
                        timeout=10).json()["data"]
    id_map = {}
    for c in data.values():
        id_map[int(c["key"])] = {"id": c["id"], "name": c["name"]}
    _ddragon.update(ts=now, version=ver, champions=id_map)
    return _ddragon


def enrich_top_mastery(mastery, dd, n=5):
    out = []
    for m in mastery[:n]:
        info = dd["champions"].get(m["championId"], {})
        out.append({**m, "name": info.get("name", str(m["championId"])),
                    "img": info.get("id")})
    return out


# ---------- 관리자 인증 ----------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("admin"):
            return jsonify({"error": "관리자 로그인이 필요합니다."}), 401
        return fn(*a, **k)
    return wrapper


# ---------- 공개 API ----------
@app.route("/api/roster")
def api_roster():
    if not RIOT_API_KEY:
        return jsonify({"error": "서버에 RIOT_API_KEY가 없습니다."}), 500
    dd = get_ddragon()
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, game_name, tag_line, puuid FROM players ORDER BY added_at")
            players = cur.fetchall()
    finally:
        conn.close()

    out = []
    for p in players:
        row = {"id": p["id"], "name": p["game_name"], "tag": p["tag_line"],
               "level": None, "iconId": None, "solo": None, "flex": None, "topMastery": []}
        try:
            live = get_player_live(p["puuid"])
            row.update(level=live["level"], iconId=live["iconId"],
                       solo=live["solo"], flex=live["flex"],
                       topMastery=enrich_top_mastery(live["mastery"], dd))
        except requests.HTTPError:
            pass
        out.append(row)

    out.sort(key=lambda x: rank_score(x.get("solo")), reverse=True)
    return jsonify({"version": dd["version"], "players": out})


@app.route("/api/champions")
def api_champions():
    dd = get_ddragon()
    champs = [{"id": cid, "name": v["name"], "img": v["id"]}
              for cid, v in sorted(dd["champions"].items(), key=lambda kv: kv[1]["name"])]
    return jsonify({"version": dd["version"], "champions": champs})


@app.route("/api/champion-ranking")
def api_champion_ranking():
    cid = request.args.get("championId", type=int)
    if not cid:
        return jsonify({"error": "championId가 필요합니다."}), 400
    dd = get_ddragon()
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT game_name, tag_line, puuid FROM players")
            players = cur.fetchall()
    finally:
        conn.close()

    ranked = []
    for p in players:
        try:
            live = get_player_live(p["puuid"])
        except requests.HTTPError:
            continue
        m = next((x for x in live["mastery"] if x["championId"] == cid), None)
        if m:
            ranked.append({"name": p["game_name"], "tag": p["tag_line"],
                           "points": m["points"], "level": m["level"]})
    ranked.sort(key=lambda x: x["points"], reverse=True)
    champ = dd["champions"].get(cid, {})
    return jsonify({"champion": champ.get("name"), "img": champ.get("id"),
                    "version": dd["version"], "players": ranked})


# ---------- 관리자 API ----------
@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({"error": "서버에 ADMIN_PASSWORD가 설정되지 않았습니다."}), 500
    pw = (request.form.get("password") or "")
    if pw == ADMIN_PASSWORD:
        session["admin"] = True
        return redirect("/admin")
    return redirect("/admin?err=1")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin")


@app.route("/api/admin/list")
@admin_required
def admin_list():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, game_name, tag_line FROM players ORDER BY added_at")
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({"players": rows})


@app.route("/api/admin/add", methods=["POST"])
@admin_required
def admin_add():
    raw = ((request.json or {}).get("riotId") or "").strip()
    if not raw:
        return jsonify({"error": "라이엇 ID를 입력하세요."}), 400
    if "#" in raw:
        name, tag = raw.rsplit("#", 1)
    else:
        name, tag = raw, "KR1"
    name, tag = name.strip(), tag.strip()
    try:
        acc = riot_get(REGION_HOST, f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}")
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return jsonify({"error": "라이엇 ID를 찾을 수 없습니다."}), 404
        if e.response.status_code in (401, 403):
            return jsonify({"error": "API 키가 만료/무효입니다."}), 401
        return jsonify({"error": f"조회 오류 (HTTP {e.response.status_code})"}), 502

    puuid = acc["puuid"]
    real_name = acc.get("gameName", name)
    real_tag = acc.get("tagLine", tag)
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO players (game_name, tag_line, puuid)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (puuid) DO NOTHING RETURNING id""",
                        (real_name, real_tag, puuid))
            row = cur.fetchone()
    finally:
        conn.close()
    return jsonify({"ok": True, "added": bool(row),
                    "name": f"{real_name}#{real_tag}"})


@app.route("/api/admin/remove", methods=["POST"])
@admin_required
def admin_remove():
    pid = (request.json or {}).get("id")
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM players WHERE id = %s", (pid,))
    finally:
        conn.close()
    return jsonify({"ok": True})


# ---------- 페이지 ----------
@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/admin")
def admin_page():
    return render_template_string(ADMIN_PAGE, logged_in=bool(session.get("admin")))


THEME = r"""
  :root{
    --ink:#0A1220; --surface:#111C2E; --surface2:#16233A; --line:#23344F;
    --gold:#C8AA6E; --gold-bright:#E4D5A8; --blue:#4B9CD3; --red:#C0475A;
    --text:#E6EAF0; --muted:#8FA1BB;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 500px at 50% -200px,#16283f 0%,transparent 70%),var(--ink);
       color:var(--text);font-family:'Inter',system-ui,sans-serif;min-height:100vh}
  .wrap{max-width:820px;margin:0 auto;padding:40px 18px 80px}
  h1{font-family:'Marcellus',serif;font-weight:400;font-size:34px;margin:0 0 6px}
  .eyebrow{font-size:12px;letter-spacing:.28em;text-transform:uppercase;color:var(--gold);margin-bottom:10px}
  .sub{color:var(--muted);font-size:14px;margin:0 0 26px}
  a{color:var(--gold)}
  input,select{background:var(--ink);border:1px solid var(--line);border-radius:9px;color:var(--text);
       padding:11px 13px;font:inherit;outline:none}
  input:focus,select:focus{border-color:var(--gold)}
  button{background:var(--gold);color:#1a1204;border:none;border-radius:9px;padding:11px 18px;
       font:600 14px 'Inter',sans-serif;cursor:pointer}
  button:hover{background:var(--gold-bright)}
"""

PAGE = r"""
<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>소환사 명단 대시보드</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Marcellus&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<style>__THEME__
  .tabs{display:flex;gap:8px;margin-bottom:20px}
  .tab{padding:9px 16px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--muted);cursor:pointer}
  .tab.on{background:var(--gold);color:#1a1204;border-color:var(--gold);font-weight:600}
  .row{display:flex;align-items:center;gap:14px;padding:12px 14px;border:1px solid var(--line);border-radius:11px;background:var(--surface);margin-bottom:8px}
  .rank-no{font-family:'JetBrains Mono',monospace;color:var(--gold-bright);width:26px;text-align:center;font-size:15px}
  .icon{width:44px;height:44px;border-radius:10px;border:1px solid var(--line)}
  .who{flex:1;min-width:0}
  .who .nm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .who .lv{color:var(--muted);font-size:12px}
  .tier{text-align:right;min-width:120px}
  .tier .t{font-weight:600}
  .tier .wl{color:var(--muted);font-size:12px}
  .champs{display:flex;gap:5px;margin-top:5px}
  .champs img{width:26px;height:26px;border-radius:6px;border:1px solid var(--line)}
  .muted{color:var(--muted)}
  .pts{font-family:'JetBrains Mono',monospace;color:var(--gold-bright)}
  .bar-champ{display:flex;align-items:center;gap:10px;margin:0 2px 16px}
  .bar-champ img{width:40px;height:40px;border-radius:8px}
  #status{color:var(--muted);text-align:center;padding:24px}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">League of Legends</div>
  <h1>소환사 명단 대시보드</h1>
  <p class="sub">등록된 소환사들의 티어와 챔피언 숙련도 랭킹입니다. · <a href="/admin">관리자</a></p>

  <div class="tabs">
    <div class="tab on" data-tab="tier" id="tab-tier">티어 랭킹</div>
    <div class="tab" data-tab="mastery" id="tab-mastery">챔피언 숙련도</div>
  </div>

  <div id="view-tier"><div id="status">불러오는 중…</div><div id="tier-list"></div></div>

  <div id="view-mastery" style="display:none">
    <div style="margin-bottom:16px">
      <select id="champ-select"><option value="">챔피언 선택…</option></select>
    </div>
    <div id="champ-result" class="muted" style="text-align:center;padding:16px">챔피언을 선택하면 명단의 숙련도 순위가 표시됩니다.</div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
let VERSION=null;
const dd=(sub)=>`https://ddragon.leagueoflegends.com/cdn/${VERSION}/img/${sub}`;
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tierText=e=>{ if(!e||!e.tier) return '<span class="muted">언랭크</span>';
  const div=['MASTER','GRANDMASTER','CHALLENGER'].includes(e.tier)?'':' '+e.rank;
  return `${e.tier}${div} · ${e.lp} LP`; };

async function loadRoster(){
  try{
    const r=await fetch('/api/roster'); const d=await r.json();
    if(!r.ok){ $('#status').textContent=d.error||'오류'; return; }
    VERSION=d.version; $('#status').style.display='none';
    if(!d.players.length){ $('#tier-list').innerHTML='<div class="muted" style="text-align:center;padding:24px">아직 등록된 소환사가 없습니다. 관리자 페이지에서 추가하세요.</div>'; return; }
    $('#tier-list').innerHTML=d.players.map((p,i)=>{
      const icon=(VERSION&&p.iconId!=null)?`<img class="icon" src="${dd('profileicon/'+p.iconId+'.png')}">`:`<div class="icon"></div>`;
      const champs=(p.topMastery||[]).map(m=>`<img title="${esc(m.name)} · ${m.points.toLocaleString()}점" src="${dd('champion/'+m.img+'.png')}">`).join('');
      const wl=p.solo?`<div class="wl">${p.solo.wins}승 ${p.solo.losses}패</div>`:'';
      return `<div class="row">
        <div class="rank-no">${i+1}</div>${icon}
        <div class="who"><div class="nm">${esc(p.name)} <span class="muted">#${esc(p.tag)}</span></div>
          <div class="lv">Lv.${p.level??'-'}</div><div class="champs">${champs}</div></div>
        <div class="tier"><div class="t">${tierText(p.solo)}</div>${wl}</div>
      </div>`;
    }).join('');
  }catch(e){ $('#status').textContent='불러오기 실패'; }
}

async function loadChampions(){
  const r=await fetch('/api/champions'); const d=await r.json(); VERSION=d.version;
  const sel=$('#champ-select');
  d.champions.forEach(c=>{ const o=document.createElement('option'); o.value=c.id; o.textContent=c.name; sel.appendChild(o); });
}

$('#champ-select').addEventListener('change', async e=>{
  const cid=e.target.value; const box=$('#champ-result');
  if(!cid){ box.innerHTML='챔피언을 선택하면 명단의 숙련도 순위가 표시됩니다.'; return; }
  box.innerHTML='불러오는 중…';
  const r=await fetch('/api/champion-ranking?championId='+cid); const d=await r.json();
  if(!r.ok){ box.textContent=d.error||'오류'; return; }
  let html=`<div class="bar-champ"><img src="${dd('champion/'+d.img+'.png')}"><div><b>${esc(d.champion)}</b> 숙련도 랭킹</div></div>`;
  if(!d.players.length){ box.innerHTML=html+'<div class="muted" style="text-align:center">이 챔피언 숙련도 기록이 있는 소환사가 없습니다.</div>'; return; }
  html+=d.players.map((p,i)=>`<div class="row"><div class="rank-no">${i+1}</div>
    <div class="who"><div class="nm">${esc(p.name)} <span class="muted">#${esc(p.tag)}</span></div>
      <div class="lv muted">숙련도 ${p.level}레벨</div></div>
    <div class="tier"><span class="pts">${p.points.toLocaleString()}</span> <span class="muted">점</span></div></div>`).join('');
  box.innerHTML=html;
});

document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on')); t.classList.add('on');
  const m=t.dataset.tab==='mastery';
  $('#view-tier').style.display=m?'none':''; $('#view-mastery').style.display=m?'':'none';
}));

loadRoster(); loadChampions();
</script></body></html>
"""
PAGE = PAGE.replace("__THEME__", THEME)

ADMIN_PAGE = r"""
<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>관리자 · 명단 관리</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Marcellus&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>__THEME__
  .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px;max-width:460px}
  .prow{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface2);margin-bottom:7px}
  .prow .nm{flex:1}
  .del{background:transparent;border:1px solid var(--red);color:var(--red);padding:6px 12px}
  .del:hover{background:var(--red);color:#fff}
  .err{color:#E9A2AD;font-size:13px;margin-top:8px}
  .ok{color:var(--gold-bright);font-size:13px;margin-top:8px}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">Admin</div><h1>명단 관리</h1>
  <p class="sub"><a href="/">← 대시보드로</a></p>
  {% if not logged_in %}
    <div class="card">
      <form method="post" action="/admin/login">
        <div style="margin-bottom:12px"><input type="password" name="password" placeholder="관리자 비밀번호" style="width:100%"></div>
        <button type="submit">로그인</button>
      </form>
      <div class="err" id="loginerr" style="display:none">비밀번호가 틀립니다.</div>
    </div>
  {% else %}
    <div class="card">
      <div style="display:flex;gap:8px;margin-bottom:6px">
        <input id="riotId" placeholder="소환사명#KR1" style="flex:1" autocomplete="off">
        <button id="addBtn">추가</button>
      </div>
      <div id="msg"></div>
      <div id="list" style="margin-top:16px"></div>
      <div style="margin-top:16px"><a href="/admin/logout">로그아웃</a></div>
    </div>
  {% endif %}
</div>
<script>
if(location.search.includes('err=1')){ const e=document.getElementById('loginerr'); if(e) e.style.display='block'; }
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadList(){
  const r=await fetch('/api/admin/list'); if(!r.ok) return;
  const d=await r.json();
  $('#list').innerHTML=d.players.map(p=>`<div class="prow"><span class="nm">${esc(p.game_name)} <span class="muted">#${esc(p.tag_line)}</span></span>
    <button class="del" onclick="removePlayer(${p.id})">삭제</button></div>`).join('') || '<div class="muted">등록된 소환사가 없습니다.</div>';
}
async function addPlayer(){
  const v=$('#riotId').value.trim(); if(!v) return;
  $('#msg').innerHTML='<div class="muted">추가 중…</div>';
  const r=await fetch('/api/admin/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({riotId:v})});
  const d=await r.json();
  if(!r.ok){ $('#msg').innerHTML=`<div class="err">${esc(d.error||'오류')}</div>`; return; }
  $('#msg').innerHTML=`<div class="ok">${d.added?'추가됨':'이미 등록된 소환사'}: ${esc(d.name)}</div>`;
  $('#riotId').value=''; loadList();
}
async function removePlayer(id){
  await fetch('/api/admin/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadList();
}
if($('#addBtn')){ $('#addBtn').addEventListener('click',addPlayer);
  $('#riotId').addEventListener('keydown',e=>{if(e.key==='Enter')addPlayer();}); loadList(); }
</script></body></html>
"""
ADMIN_PAGE = ADMIN_PAGE.replace("__THEME__", THEME)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
