"""
롤 회원 대시보드 (회원 리스트 · 챔피언별 최고 숙련자)

설계:
  - 회원 명단 + 각 회원의 레벨/아이콘/숙련도를 DB(Postgres)에 저장
  - 공개 페이지는 DB만 읽어서 즉시 렌더 (API 호출 없음 → 빠르고 타임아웃 없음)
  - 관리자가 '정보 갱신' 버튼을 누를 때만 Riot API로 최신 데이터를 받아 DB에 저장

Riot API 라우팅:
  - ACCOUNT-V1  → asia (라이엇 ID → puuid)
  - SUMMONER-V4 / CHAMPION-MASTERY-V4 → kr

환경변수: RIOT_API_KEY, DATABASE_URL, ADMIN_PASSWORD, SECRET_KEY
실행: gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120
"""

import os
import time
import threading
from functools import wraps
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

import requests
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from flask import Flask, request, jsonify, render_template_string, session, redirect

# ===================== 설정 =====================
RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
PLATFORM = "kr"      # summoner/mastery 호스트
REGION = "asia"      # account 호스트
WORKERS = 6          # 갱신 시 동시에 처리할 회원 수
# ===============================================

PLATFORM_HOST = f"https://{PLATFORM}.api.riotgames.com"
REGION_HOST = f"https://{REGION}.api.riotgames.com"

app = Flask(__name__)
app.secret_key = SECRET_KEY

_ddragon = {"ts": 0, "version": None, "champions": {}}


# ---------- DB ----------
def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    id             SERIAL PRIMARY KEY,
                    game_name      TEXT NOT NULL,
                    tag_line       TEXT NOT NULL,
                    puuid          TEXT UNIQUE NOT NULL,
                    summoner_level INT,
                    profile_icon_id INT,
                    updated_at     TIMESTAMPTZ,
                    added_at       TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (game_name, tag_line)
                );
            """)
            # 기존 DB(구버전 테이블) 대비 컬럼 보강
            cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS summoner_level INT;")
            cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS profile_icon_id INT;")
            cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mastery (
                    player_id   INT REFERENCES players(id) ON DELETE CASCADE,
                    champion_id INT NOT NULL,
                    points      INT NOT NULL,
                    level       INT NOT NULL,
                    PRIMARY KEY (player_id, champion_id)
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
    for _ in range(3):
        r = requests.get(host + path, headers={"X-Riot-Token": RIOT_API_KEY},
                         params=params, timeout=(5, 10))
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 3)), 5))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def fetch_player_store(puuid):
    """저장용 데이터: 레벨, 아이콘, 전체 숙련도 (티어/리그는 사용 안 함)."""
    summ = riot_get(PLATFORM_HOST, f"/lol/summoner/v4/summoners/by-puuid/{puuid}")
    mastery = riot_get(PLATFORM_HOST, f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}")
    return {
        "level": summ.get("summonerLevel"),
        "iconId": summ.get("profileIconId"),
        "mastery": [{"championId": m["championId"], "points": m["championPoints"],
                     "level": m["championLevel"]} for m in mastery],
    }


def store_player(conn, player_id, puuid):
    data = fetch_player_store(puuid)
    with conn.cursor() as cur:
        cur.execute("UPDATE players SET summoner_level=%s, profile_icon_id=%s, updated_at=now() WHERE id=%s",
                    (data["level"], data["iconId"], player_id))
        cur.execute("DELETE FROM mastery WHERE player_id=%s", (player_id,))
        if data["mastery"]:
            execute_values(cur,
                "INSERT INTO mastery (player_id, champion_id, points, level) VALUES %s",
                [(player_id, m["championId"], m["points"], m["level"]) for m in data["mastery"]])
    conn.commit()


# ---------- Data Dragon ----------
def get_ddragon():
    now = time.time()
    if _ddragon["version"] and now - _ddragon["ts"] < 86400:
        return _ddragon
    ver = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=10).json()[0]
    data = requests.get(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/ko_KR/champion.json",
                        timeout=10).json()["data"]
    id_map = {int(c["key"]): {"id": c["id"], "name": c["name"]} for c in data.values()}
    _ddragon.update(ts=now, version=ver, champions=id_map)
    return _ddragon


def safe_ddragon():
    try:
        return get_ddragon()
    except Exception:
        return {"version": None, "champions": {}}


# ---------- 관리자 인증 ----------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        if not session.get("admin"):
            return jsonify({"error": "관리자 로그인이 필요합니다."}), 401
        return fn(*a, **k)
    return wrapper


# ---------- 공개 API (DB만 읽음) ----------
@app.route("/api/members")
def api_members():
    ver = safe_ddragon()["version"]
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT game_name, tag_line, summoner_level, profile_icon_id, updated_at
                           FROM players
                           ORDER BY summoner_level DESC NULLS LAST, added_at""")
            rows = cur.fetchall()
    finally:
        conn.close()
    members = [{"name": r["game_name"], "tag": r["tag_line"],
                "level": r["summoner_level"], "iconId": r["profile_icon_id"]} for r in rows]
    last = max([r["updated_at"] for r in rows if r["updated_at"]], default=None)
    return jsonify({"version": ver, "members": members,
                    "updatedAt": last.isoformat() if last else None})


@app.route("/api/champion-top")
def api_champion_top():
    dd = safe_ddragon()
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (m.champion_id)
                       m.champion_id, m.points, m.level, p.game_name, p.tag_line
                FROM mastery m JOIN players p ON p.id = m.player_id
                ORDER BY m.champion_id, m.points DESC
            """)
            tops = {r["champion_id"]: r for r in cur.fetchall()}
    finally:
        conn.close()

    out = []
    for cid, info in dd["champions"].items():
        t = tops.get(cid)
        out.append({
            "championId": cid, "name": info["name"], "img": info["id"],
            "top": ({"name": t["game_name"], "tag": t["tag_line"],
                     "points": t["points"], "level": t["level"]} if t else None),
        })
    # 기록 있는 챔피언 먼저(포인트 내림차순), 그다음 기록 없는 챔피언(이름순)
    out.sort(key=lambda x: (0 if x["top"] else 1,
                            -(x["top"]["points"] if x["top"] else 0), x["name"]))
    return jsonify({"version": dd["version"], "champions": out})


# ---------- 관리자 API ----------
@app.route("/admin/login", methods=["POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({"error": "서버에 ADMIN_PASSWORD가 없습니다."}), 500
    if (request.form.get("password") or "") == ADMIN_PASSWORD:
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
            cur.execute("""SELECT id, game_name, tag_line, summoner_level, updated_at
                           FROM players ORDER BY added_at""")
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat() if r["updated_at"] else None
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
        new_id = row[0] if row else None
    finally:
        conn.close()

    # 새로 추가된 회원은 바로 정보 저장(실패해도 명단엔 남고 나중에 갱신 가능)
    if new_id:
        try:
            c = get_db()
            try:
                store_player(c, new_id, puuid)
            finally:
                c.close()
        except Exception:
            pass
    return jsonify({"ok": True, "added": bool(new_id), "name": f"{real_name}#{real_tag}"})


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


@app.route("/api/admin/refresh", methods=["POST"])
@admin_required
def admin_refresh():
    if not RIOT_API_KEY:
        return jsonify({"error": "서버에 RIOT_API_KEY가 없습니다."}), 500
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, puuid FROM players")
            players = cur.fetchall()
    finally:
        conn.close()

    result = {"updated": 0, "failed": 0}
    errors = []
    lock = threading.Lock()

    def work(p):
        try:
            c = get_db()
        except Exception as e:
            with lock:
                result["failed"] += 1
                if len(errors) < 3:
                    errors.append(f"DB연결: {type(e).__name__}: {e}")
            return
        try:
            store_player(c, p["id"], p["puuid"])
            with lock:
                result["updated"] += 1
        except Exception as e:
            print("refresh error:", repr(e))
            with lock:
                result["failed"] += 1
                if len(errors) < 3:
                    errors.append(f"{type(e).__name__}: {e}")
        finally:
            try:
                c.close()
            except Exception:
                pass

    if players:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(work, players))
    return jsonify({"ok": True, **result, "errors": errors})


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
  button:disabled{opacity:.55;cursor:default}
"""

PAGE = r"""
<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>회원 대시보드</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Marcellus&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<style>__THEME__
  .tabs{display:flex;gap:8px;margin-bottom:14px}
  .tab{padding:9px 16px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--muted);cursor:pointer}
  .tab.on{background:var(--gold);color:#1a1204;border-color:var(--gold);font-weight:600}
  .updated{color:var(--muted);font-size:12px;margin:0 2px 16px}
  .row{display:flex;align-items:center;gap:14px;padding:11px 14px;border:1px solid var(--line);border-radius:11px;background:var(--surface);margin-bottom:7px}
  .no{font-family:'JetBrains Mono',monospace;color:var(--gold-bright);width:30px;text-align:center;font-size:15px}
  .icon{width:42px;height:42px;border-radius:10px;border:1px solid var(--line)}
  .who{flex:1;min-width:0}
  .who .nm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .who .sm{color:var(--muted);font-size:12px}
  .lvl{font-family:'JetBrains Mono',monospace;color:var(--gold-bright);text-align:right}
  .lvl small{color:var(--muted);font-weight:400;font-size:11px}
  .pts{font-family:'JetBrains Mono',monospace;color:var(--gold-bright)}
  .champ-nm{font-weight:600}
  .top-who{color:var(--text)}
  .norec{color:var(--muted);font-size:13px}
  #search{width:100%;margin-bottom:12px}
  .muted{color:var(--muted)}
  #status{color:var(--muted);text-align:center;padding:24px}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">League of Legends</div>
  <h1>회원 대시보드</h1>
  <p class="sub">등록된 회원과 챔피언별 최고 숙련자입니다. · <a href="/admin">관리자</a></p>

  <div class="tabs">
    <div class="tab on" data-tab="members">회원 리스트</div>
    <div class="tab" data-tab="mastery">챔피언 숙련도</div>
  </div>
  <div class="updated" id="updated"></div>

  <div id="view-members"><div id="status">불러오는 중…</div><div id="member-list"></div></div>

  <div id="view-mastery" style="display:none">
    <input id="search" placeholder="챔피언 이름 검색…" autocomplete="off">
    <div id="champ-list"><div class="muted" style="text-align:center;padding:16px">불러오는 중…</div></div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
let VERSION=null, CHAMPS=[];
const dd=sub=>`https://ddragon.leagueoflegends.com/cdn/${VERSION}/img/${sub}`;
const esc=s=>(s==null?'':(''+s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadMembers(){
  try{
    const r=await fetch('/api/members'); const d=await r.json();
    VERSION=d.version; $('#status').style.display='none';
    $('#updated').textContent = d.updatedAt ? ('마지막 갱신: '+d.updatedAt.slice(0,16).replace('T',' ')) : '아직 갱신 안 됨 — 관리자 페이지에서 정보 갱신을 눌러주세요.';
    if(!d.members.length){ $('#member-list').innerHTML='<div class="muted" style="text-align:center;padding:24px">등록된 회원이 없습니다.</div>'; return; }
    $('#member-list').innerHTML=d.members.map((m,i)=>{
      const icon=(VERSION&&m.iconId!=null)?`<img class="icon" src="${dd('profileicon/'+m.iconId+'.png')}">`:`<div class="icon"></div>`;
      return `<div class="row"><div class="no">${i+1}</div>${icon}
        <div class="who"><div class="nm">${esc(m.name)} <span class="sm">#${esc(m.tag)}</span></div></div>
        <div class="lvl">${m.level??'-'} <small>레벨</small></div></div>`;
    }).join('');
  }catch(e){ $('#status').textContent='불러오기 실패'; }
}

function renderChamps(filter){
  const f=(filter||'').trim();
  const list=f?CHAMPS.filter(c=>c.name.includes(f)):CHAMPS;
  if(!list.length){ $('#champ-list').innerHTML='<div class="muted" style="text-align:center;padding:16px">해당 챔피언이 없습니다.</div>'; return; }
  $('#champ-list').innerHTML=list.map(c=>{
    const icon=VERSION?`<img class="icon" src="${dd('champion/'+c.img+'.png')}">`:`<div class="icon"></div>`;
    const top=c.top
      ? `<div class="who"><div class="top-who">${esc(c.top.name)} <span class="muted">#${esc(c.top.tag)}</span></div></div><div class="lvl"><span class="pts">${c.top.points.toLocaleString()}</span> <small>점</small></div>`
      : `<div class="who"><span class="norec">기록 없음</span></div>`;
    return `<div class="row">${icon}<div style="min-width:90px"><span class="champ-nm">${esc(c.name)}</span></div>${top}</div>`;
  }).join('');
}

async function loadChamps(){
  try{
    const r=await fetch('/api/champion-top'); const d=await r.json();
    VERSION=d.version||VERSION; CHAMPS=d.champions||[];
    renderChamps('');
  }catch(e){ $('#champ-list').innerHTML='<div class="muted" style="text-align:center">불러오기 실패</div>'; }
}

$('#search').addEventListener('input', e=>renderChamps(e.target.value));
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on')); t.classList.add('on');
  const m=t.dataset.tab==='mastery';
  $('#view-members').style.display=m?'none':''; $('#view-mastery').style.display=m?'':'none';
}));

loadMembers(); loadChamps();
</script></body></html>
"""
PAGE = PAGE.replace("__THEME__", THEME)

ADMIN_PAGE = r"""
<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>관리자</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Marcellus&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>__THEME__
  .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:20px;max-width:480px}
  .prow{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface2);margin-bottom:7px}
  .prow .nm{flex:1}
  .prow .lv{color:var(--muted);font-size:12px}
  .del{background:transparent;border:1px solid var(--red);color:var(--red);padding:6px 12px}
  .del:hover{background:var(--red);color:#fff}
  .refresh{background:transparent;border:1px solid var(--blue);color:var(--blue)}
  .refresh:hover{background:var(--blue);color:#fff}
  .err{color:#E9A2AD;font-size:13px;margin-top:8px}
  .ok{color:var(--gold-bright);font-size:13px;margin-top:8px}
  .bar{display:flex;gap:8px;margin-bottom:6px}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">Admin</div><h1>회원 관리</h1>
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
      <div class="bar">
        <input id="riotId" placeholder="소환사명#KR1" style="flex:1" autocomplete="off">
        <button id="addBtn">추가</button>
      </div>
      <div class="bar"><button id="refreshBtn" class="refresh" style="width:100%">전체 정보 갱신 (레벨·숙련도)</button></div>
      <div id="msg"></div>
      <div id="list" style="margin-top:16px"></div>
      <div style="margin-top:16px"><a href="/admin/logout">로그아웃</a></div>
    </div>
  {% endif %}
</div>
<script>
if(location.search.includes('err=1')){const e=document.getElementById('loginerr'); if(e)e.style.display='block';}
const $=s=>document.querySelector(s);
const esc=s=>(s==null?'':(''+s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadList(){
  const r=await fetch('/api/admin/list'); if(!r.ok) return;
  const d=await r.json();
  $('#list').innerHTML=d.players.map(p=>{
    const lv=p.summoner_level!=null?`Lv.${p.summoner_level}`:'미갱신';
    return `<div class="prow"><span class="nm">${esc(p.game_name)} <span class="muted">#${esc(p.tag_line)}</span></span>
      <span class="lv">${lv}</span><button class="del" onclick="removePlayer(${p.id})">삭제</button></div>`;
  }).join('') || '<div class="muted">등록된 회원이 없습니다.</div>';
}
async function addPlayer(){
  const v=$('#riotId').value.trim(); if(!v) return;
  $('#msg').innerHTML='<div class="muted">추가 중…</div>';
  const r=await fetch('/api/admin/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({riotId:v})});
  const d=await r.json();
  if(!r.ok){ $('#msg').innerHTML=`<div class="err">${esc(d.error||'오류')}</div>`; return; }
  $('#msg').innerHTML=`<div class="ok">${d.added?'추가됨':'이미 등록됨'}: ${esc(d.name)}</div>`;
  $('#riotId').value=''; loadList();
}
async function removePlayer(id){
  await fetch('/api/admin/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadList();
}
async function refreshAll(){
  const btn=$('#refreshBtn'); btn.disabled=true;
  $('#msg').innerHTML='<div class="muted">갱신 중… 인원에 따라 시간이 걸립니다.</div>';
  try{
    const r=await fetch('/api/admin/refresh',{method:'POST'}); const d=await r.json();
    if(!r.ok){ $('#msg').innerHTML=`<div class="err">${esc(d.error||'오류')}</div>`; }
    else{
      let m=`<div class="ok">${d.updated}명 갱신 완료${d.failed?`, ${d.failed}명 실패`:''}</div>`;
      if(d.errors&&d.errors.length){ m+=`<div class="err">사유: ${esc(d.errors.join(' | '))}</div>`; }
      $('#msg').innerHTML=m; loadList();
    }
  }catch(e){ $('#msg').innerHTML='<div class="err">갱신 요청 실패</div>'; }
  finally{ btn.disabled=false; }
}
if($('#addBtn')){
  $('#addBtn').addEventListener('click',addPlayer);
  $('#riotId').addEventListener('keydown',e=>{if(e.key==='Enter')addPlayer();});
  $('#refreshBtn').addEventListener('click',refreshAll);
  loadList();
}
</script></body></html>
"""
ADMIN_PAGE = ADMIN_PAGE.replace("__THEME__", THEME)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
