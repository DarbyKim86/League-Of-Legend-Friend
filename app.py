"""
롤 '최근 함께한 사람' 웹앱 (단일 파일: 프론트엔드 + 백엔드 프록시)

왜 백엔드가 필요한가:
  - Riot API 키를 브라우저에 넣으면 누구나 훔쳐볼 수 있고,
  - Riot API는 브라우저 직접 호출(CORS)을 막아둠.
  => 그래서 이 서버가 키를 숨겨 들고 대신 Riot을 호출한 뒤 결과만 페이지로 넘겨줌.

실행 방법:
  1) pip install flask requests
  2) 환경변수로 키 지정 후 실행
        (윈도우 PowerShell)  $env:RIOT_API_KEY="RGAPI-..."; python app.py
        (맥/리눅스)          RIOT_API_KEY="RGAPI-..." python app.py
     또는 아래 DEFAULT_KEY 에 직접 넣어도 됨(권장 X).
  3) 브라우저에서 http://localhost:5000 접속

공개 배포 시 주의:
  - 개인 키는 24시간마다 만료됨 → 만료되면 새 키로 교체.
  - 요청 제한(2분당 100회)이 전체 사용자 공유 → 지인용으로는 OK,
    불특정 다수 공개 서비스는 프로덕트 키 승인 필요.
"""

import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template_string, request

# ===================== 설정 =====================
DEFAULT_KEY = ""        # 여기에 직접 넣지 말고 환경변수 RIOT_API_KEY 로 지정할 것
ROUTING = "asia"        # 한국 계정: asia
DAYS = 30               # 며칠 이내 매치까지 볼지
SLEEP = 1.2             # Riot 요청 사이 대기(초). 레이트 리밋 여유용
# ===============================================

API_KEY = os.environ.get("RIOT_API_KEY", DEFAULT_KEY)
BASE = f"https://{ROUTING}.api.riotgames.com"

app = Flask(__name__)


def riot_get(url, params=None):
    """429면 Retry-After 만큼 대기 후 재시도. 그 외 HTTP 에러는 그대로 올림."""
    while True:
        r = requests.get(url, headers={"X-Riot-Token": API_KEY}, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 5)))
            continue
        r.raise_for_status()
        return r.json()


def riot_id_of(p):
    name = p.get("riotIdGameName") or p.get("riotIdName") or "(이름없음)"
    tag = p.get("riotIdTagline") or p.get("riotIdTagLine") or ""
    return f"{name}#{tag}" if tag else name


def lookup(game_name, tag_line, count):
    """이름/태그로 최근 count개 매치를 훑어 함께한 사람 집계."""
    # 1) PUUID
    acc = riot_get(
        f"{BASE}/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
    )
    my_puuid = acc["puuid"]

    # 2) 매치 ID (날짜 제한 없이 최근 count개)
    ids, start = [], 0
    while len(ids) < count:
        batch = riot_get(
            f"{BASE}/lol/match/v5/matches/by-puuid/{my_puuid}/ids",
            {"start": start, "count": min(100, count - len(ids))},
        )
        if not batch:
            break
        ids.extend(batch)
        if len(batch) < 100:
            break
        start += len(batch)
        time.sleep(SLEEP)

    # 3) 각 매치 참가자 집계 (적팀 포함)
    stats = defaultdict(lambda: {"total": 0, "ally": 0, "enemy": 0})
    names = {}
    for mid in ids:
        try:
            match = riot_get(f"{BASE}/lol/match/v5/matches/{mid}")
        except requests.HTTPError:
            continue
        parts = match["info"]["participants"]
        my_team = next((p["teamId"] for p in parts if p["puuid"] == my_puuid), None)
        for p in parts:
            if p["puuid"] == my_puuid:
                continue
            s = stats[p["puuid"]]
            s["total"] += 1
            if my_team is not None and p["teamId"] == my_team:
                s["ally"] += 1
            else:
                s["enemy"] += 1
            names[p["puuid"]] = riot_id_of(p)
        time.sleep(SLEEP)

    people = [
        {"name": names[pu], "total": s["total"], "ally": s["ally"], "enemy": s["enemy"]}
        for pu, s in stats.items()
    ]
    people.sort(key=lambda x: x["total"], reverse=True)
    return {"me": riot_id_of(acc) if acc.get("riotIdGameName") else f"{game_name}#{tag_line}",
            "scanned": len(ids), "people": people}


@app.route("/api/lookup")
def api_lookup():
    if not API_KEY.strip():
        return jsonify({"error": "서버에 RIOT_API_KEY가 설정되지 않았습니다. 호스팅 환경변수를 확인해 주세요."}), 500

    raw = (request.args.get("riotId") or "").strip()
    if not raw:
        return jsonify({"error": "라이엇 ID를 입력해 주세요."}), 400
    if "#" in raw:
        game_name, tag_line = raw.rsplit("#", 1)
    else:
        game_name, tag_line = raw, "KR1"
    game_name, tag_line = game_name.strip(), tag_line.strip()

    try:
        count = max(1, min(200, int(request.args.get("count", 20))))
    except ValueError:
        count = 20

    try:
        return jsonify(lookup(game_name, tag_line, count))
    except requests.HTTPError as e:
        code = e.response.status_code
        if code == 404:
            return jsonify({"error": "라이엇 ID를 찾을 수 없습니다. 이름과 태그를 확인해 주세요."}), 404
        if code in (401, 403):
            return jsonify({"error": "API 키가 만료되었거나 잘못되었습니다. 키를 새로 발급해 주세요."}), 401
        return jsonify({"error": f"Riot API 오류 (HTTP {code})"}), 502
    except requests.RequestException:
        return jsonify({"error": "네트워크 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."}), 502


@app.route("/api/debug")
def api_debug():
    """각 단계가 실제로 뭘 반환하는지 그대로 보여주는 진단용 엔드포인트."""
    raw = (request.args.get("riotId") or "").strip()
    if "#" in raw:
        game_name, tag_line = raw.rsplit("#", 1)
    else:
        game_name, tag_line = raw, "KR1"
    game_name, tag_line = game_name.strip(), tag_line.strip()

    out = {
        "key_set": bool(API_KEY.strip()),
        "routing": ROUTING,
        "input": {"gameName": game_name, "tagLine": tag_line},
    }
    try:
        acc = riot_get(
            f"{BASE}/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
        )
        out["account_ok"] = True
        out["account"] = {
            "gameName": acc.get("gameName"),
            "tagLine": acc.get("tagLine"),
            "puuid_len": len(acc.get("puuid", "")),
        }
        mids = riot_get(
            f"{BASE}/lol/match/v5/matches/by-puuid/{acc['puuid']}/ids",
            {"start": 0, "count": 5},
        )
        out["match_ids_count"] = len(mids)
        out["match_ids_sample"] = mids
    except requests.HTTPError as e:
        out["error"] = {
            "status": e.response.status_code,
            "url": e.response.url,
            "body": e.response.text[:300],
        }
    except requests.RequestException as e:
        out["error"] = {"exception": str(e)}
    return jsonify(out)


PAGE = r"""
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>함께한 소환사</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Marcellus&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@600&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#0A1220; --surface:#111C2E; --surface2:#16233A; --line:#23344F;
    --gold:#C8AA6E; --gold-bright:#E4D5A8; --ally:#4B9CD3; --enemy:#C0475A;
    --text:#E6EAF0; --muted:#8FA1BB;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:
      radial-gradient(1200px 500px at 50% -200px, #16283f 0%, transparent 70%),
      var(--ink);
    color:var(--text); font-family:'Inter',sans-serif; line-height:1.5;
    min-height:100vh;
  }
  .wrap{max-width:760px; margin:0 auto; padding:48px 20px 80px}
  header{text-align:center; margin-bottom:36px}
  .eyebrow{
    font-size:12px; letter-spacing:.28em; text-transform:uppercase;
    color:var(--gold); margin-bottom:12px;
  }
  h1{
    font-family:'Marcellus',serif; font-weight:400; font-size:40px;
    margin:0 0 8px; letter-spacing:.01em;
  }
  .sub{color:var(--muted); font-size:15px; margin:0}

  .panel{
    background:linear-gradient(180deg,var(--surface) 0%,var(--surface2) 100%);
    border:1px solid var(--line); border-radius:14px; padding:20px;
    display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end;
  }
  .field{flex:1 1 220px; min-width:0}
  label{display:block; font-size:12px; color:var(--muted); margin-bottom:6px; letter-spacing:.02em}
  input{
    width:100%; background:var(--ink); border:1px solid var(--line); border-radius:9px;
    color:var(--text); padding:12px 14px; font:inherit; outline:none;
  }
  input:focus{border-color:var(--gold)}
  .count-field{flex:0 0 130px}
  button{
    flex:0 0 auto; background:var(--gold); color:#1a1204; border:none; border-radius:9px;
    padding:12px 22px; font:600 15px 'Inter',sans-serif; cursor:pointer;
    transition:background .15s;
  }
  button:hover{background:var(--gold-bright)}
  button:disabled{opacity:.55; cursor:default}
  .hint{color:var(--muted); font-size:12.5px; margin:10px 2px 0}

  #status{margin-top:28px; text-align:center; color:var(--muted)}
  .spinner{
    width:26px; height:26px; margin:0 auto 12px; border-radius:50%;
    border:2.5px solid var(--line); border-top-color:var(--gold);
    animation:spin .8s linear infinite;
  }
  @keyframes spin{to{transform:rotate(360deg)}}
  .err{color:#E9A2AD}

  .meta{margin:30px 2px 14px; display:flex; justify-content:space-between; align-items:baseline; color:var(--muted); font-size:13px}
  .meta b{color:var(--text); font-family:'Marcellus',serif; font-weight:400; font-size:16px}

  .row{
    display:grid; grid-template-columns:1fr auto; gap:6px 16px;
    padding:14px 16px; border:1px solid var(--line); border-radius:11px;
    background:var(--surface); margin-bottom:8px;
  }
  .name{font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .total{font-family:'JetBrains Mono',monospace; color:var(--gold-bright); font-size:15px; text-align:right}
  .total small{color:var(--muted); font-weight:400; font-size:11px; margin-left:3px}
  .bar{grid-column:1 / -1; height:7px; border-radius:99px; overflow:hidden; display:flex; background:var(--ink)}
  .bar .a{background:var(--ally)} .bar .e{background:var(--enemy)}
  .legend{grid-column:1 / -1; display:flex; gap:16px; font-size:11.5px; color:var(--muted); margin-top:1px}
  .dot{display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:5px; vertical-align:middle}
  .dot.a{background:var(--ally)} .dot.e{background:var(--enemy)}

  footer{margin-top:40px; text-align:center; color:var(--muted); font-size:12px; line-height:1.7}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">League of Legends</div>
    <h1>함께한 소환사</h1>
    <p class="sub">라이엇 ID를 입력하면 최근 같은 게임에 있던 사람들을 자주 만난 순으로 보여줍니다.</p>
  </header>

  <div class="panel">
    <div class="field">
      <label for="riotId">라이엇 ID</label>
      <input id="riotId" placeholder="소환사명#KR1" autocomplete="off">
    </div>
    <div class="field count-field">
      <label for="count">최근 게임 수</label>
      <input id="count" type="number" value="20" min="1" max="200">
    </div>
    <button id="go">조회</button>
    <p class="hint">태그(#뒤)를 빼면 KR1로 처리합니다. 게임 수를 늘릴수록 정확하지만 더 느려집니다.</p>
  </div>

  <div id="status"></div>
  <div id="results"></div>

  <footer>
    키는 서버에만 저장되며 브라우저로 전달되지 않습니다.<br>
    적팀까지 포함해 집계합니다 · 게임당 여러 번 Riot API를 호출하므로 조회에 수십 초~몇 분이 걸릴 수 있습니다.
  </footer>
</div>

<script>
const $ = s => document.querySelector(s);
const statusEl = $('#status'), resultsEl = $('#results'), btn = $('#go');

function esc(s){ return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function run(){
  const riotId = $('#riotId').value.trim();
  const count = $('#count').value || 20;
  if(!riotId){ statusEl.innerHTML = '<p class="err">라이엇 ID를 입력해 주세요.</p>'; return; }

  btn.disabled = true;
  resultsEl.innerHTML = '';
  statusEl.innerHTML = '<div class="spinner"></div><p>최근 게임을 훑는 중입니다… 잠시만 기다려 주세요.</p>';

  try{
    const res = await fetch(`/api/lookup?riotId=${encodeURIComponent(riotId)}&count=${count}`);
    const data = await res.json();
    if(!res.ok){ statusEl.innerHTML = `<p class="err">${esc(data.error || '오류가 발생했습니다.')}</p>`; return; }
    render(data);
  }catch(e){
    statusEl.innerHTML = '<p class="err">요청에 실패했습니다. 서버가 켜져 있는지 확인해 주세요.</p>';
  }finally{
    btn.disabled = false;
  }
}

function render(data){
  statusEl.innerHTML = '';
  if(!data.people.length){
    resultsEl.innerHTML = `<p style="text-align:center;color:var(--muted);margin-top:24px">${esc(data.me)} 기준 최근 ${data.scanned}게임에서 함께한 사람이 없습니다.</p>`;
    return;
  }
  let html = `<div class="meta"><b>${esc(data.me)}</b><span>최근 ${data.scanned}게임 기준 · ${data.people.length}명</span></div>`;
  html += `<div class="legend" style="margin:0 2px 12px"><span><i class="dot a"></i>같은 편</span><span><i class="dot e"></i>적팀</span></div>`;
  for(const p of data.people){
    const allyPct = p.total ? (p.ally / p.total * 100) : 0;
    const enemyPct = 100 - allyPct;
    html += `
      <div class="row">
        <div class="name">${esc(p.name)}</div>
        <div class="total">${p.total}<small>회</small></div>
        <div class="bar">
          <span class="a" style="width:${allyPct}%"></span>
          <span class="e" style="width:${enemyPct}%"></span>
        </div>
        <div class="legend">
          <span><i class="dot a"></i>같은 편 ${p.ally}</span>
          <span><i class="dot e"></i>적팀 ${p.enemy}</span>
        </div>
      </div>`;
  }
  resultsEl.innerHTML = html;
}

btn.addEventListener('click', run);
$('#riotId').addEventListener('keydown', e => { if(e.key === 'Enter') run(); });
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
