"""
협곡의 배신자 — 실시간 웹게임 서버 (Flask-SocketIO)
- 방 생성/입장 → 방장이 시작 → 실시간 턴 진행.
- 히든롤: 매 갱신마다 각 소켓에 '그 사람이 볼 수 있는 뷰'만 전송.
- 화면(HTML/JS)은 이 파일에 내장(A안).
실행: python game_server.py  (로컬)  /  배포: gunicorn -k eventlet -w 1 game_server:app
"""
import os
import random
import string
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, join_room, emit

from game import Game, ROLE_RATIO

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# 방 저장 (메모리)
ROOMS = {}   # code -> {'host':sid, 'players':[{sid,id,name}], 'game':Game|None}


def new_code():
    while True:
        c = "".join(random.choices(string.ascii_uppercase, k=4))
        if c not in ROOMS:
            return c


def room_of(sid):
    for code, r in ROOMS.items():
        for p in r["players"]:
            if p["sid"] == sid:
                return code, r, p
    return None, None, None


def push(code):
    """방 전원에게 각자의 뷰를 개별 전송."""
    r = ROOMS.get(code)
    if not r:
        return
    if r["game"]:
        for p in r["players"]:
            socketio.emit("state", r["game"].view_for(p["id"]), to=p["sid"])
    else:
        lobby = {"phase": "대기", "code": code, "host": r["host_id"],
                 "players": [{"id": p["id"], "name": p["name"]} for p in r["players"]]}
        socketio.emit("lobby", lobby, to=code)


# ---------- Socket 이벤트 ----------
@socketio.on("create_room")
def on_create(data):
    name = (data.get("name") or "익명").strip()[:12]
    code = new_code()
    pid = request.sid
    ROOMS[code] = {"host": request.sid, "host_id": pid,
                   "players": [{"sid": request.sid, "id": pid, "name": name}], "game": None}
    join_room(code)
    emit("joined", {"code": code, "id": pid})
    push(code)


@socketio.on("join_room")
def on_join(data):
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "익명").strip()[:12]
    r = ROOMS.get(code)
    if not r:
        emit("err", {"msg": "방을 찾을 수 없습니다."}); return
    if r["game"]:
        emit("err", {"msg": "이미 시작된 방입니다."}); return
    if len(r["players"]) >= 7:
        emit("err", {"msg": "정원(7명)이 찼습니다."}); return
    pid = request.sid
    r["players"].append({"sid": request.sid, "id": pid, "name": name})
    join_room(code)
    emit("joined", {"code": code, "id": pid})
    push(code)


@socketio.on("start_game")
def on_start(data):
    code, r, p = room_of(request.sid)
    if not r or r["host"] != request.sid:
        emit("err", {"msg": "방장만 시작할 수 있습니다."}); return
    n = len(r["players"])
    if n not in ROLE_RATIO:
        emit("err", {"msg": "4~7명이어야 시작할 수 있습니다."}); return
    r["game"] = Game([{"id": p["id"], "name": p["name"]} for p in r["players"]])
    push(code)


@socketio.on("act")
def on_act(data):
    code, r, p = room_of(request.sid)
    if not r or not r["game"]:
        return
    ok, msg = r["game"].action(p["id"], data.get("kind"), data.get("payload", {}))
    if not ok:
        emit("err", {"msg": msg})
    push(code)


@socketio.on("disconnect")
def on_disc():
    code, r, p = room_of(request.sid)
    if not r:
        return
    if r["game"]:
        # 진행 중 이탈: 표시만(간단 MVP — 자리 유지)
        for pp in r["game"].players:
            if pp["id"] == p["id"]:
                pp["connected"] = False
        push(code)
    else:
        r["players"] = [x for x in r["players"] if x["sid"] != request.sid]
        if not r["players"]:
            ROOMS.pop(code, None)
        else:
            if r["host"] == request.sid:
                r["host"] = r["players"][0]["sid"]
                r["host_id"] = r["players"][0]["id"]
            push(code)


@app.route("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>협곡의 배신자</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
  :root{--ink:#0A1220;--surface:#111C2E;--surface2:#16233A;--line:#23344F;--gold:#C8AA6E;
    --gold2:#E4D5A8;--blue:#4B9CD3;--red:#C0475A;--green:#3FB27F;--text:#E6EAF0;--muted:#8FA1BB;}
  *{box-sizing:border-box} body{margin:0;background:var(--ink);color:var(--text);
    font-family:Inter,system-ui,sans-serif;padding:14px;max-width:760px;margin:0 auto}
  h1{font-size:20px;margin:6px 0 12px;color:var(--gold2)}
  button{background:var(--gold);color:#1a1204;border:0;border-radius:8px;padding:9px 14px;font-weight:600;cursor:pointer}
  button.sec{background:transparent;border:1px solid var(--line);color:var(--text)}
  button:disabled{opacity:.4;cursor:default}
  input{background:var(--surface2);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:9px 10px}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .muted{color:var(--muted)} .err{color:var(--red);min-height:18px;font-size:13px}
  .role{font-weight:700} .role.s{color:var(--red)} .role.m{color:var(--blue)}
  #board{display:grid;gap:3px;justify-content:center;margin:10px 0;overflow:auto}
  .cell{width:46px;height:46px;background:var(--surface2);border:1px solid var(--line);border-radius:6px;
    position:relative;cursor:pointer}
  .cell.empty:hover{outline:2px solid var(--gold)}
  .cell.start{background:#20406a}.cell.nexus{background:#3a2a4a}
  .cell.nexus.real{background:#2a5a3a}.cell.nexus.fake{background:#5a2a2a}
  .pip{position:absolute;background:var(--gold2);border-radius:2px}
  .n{top:0;left:50%;width:8px;height:22px;transform:translateX(-50%)}
  .s{bottom:0;left:50%;width:8px;height:22px;transform:translateX(-50%)}
  .e{right:0;top:50%;width:22px;height:8px;transform:translateY(-50%)}
  .w{left:0;top:50%;width:22px;height:8px;transform:translateY(-50%)}
  .noconn{background:var(--red)!important;opacity:.6}
  .hand{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
  .hc{width:52px;height:52px;border:1px solid var(--line);border-radius:8px;background:var(--surface2);
    position:relative;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:11px;text-align:center}
  .hc.sel{outline:2px solid var(--gold)}
  .hc .act{color:var(--gold2);font-weight:700}
  .pl{display:flex;justify-content:space-between;padding:6px 8px;border:1px solid var(--line);border-radius:8px;margin-bottom:5px}
  .pl.turn{border-color:var(--gold);background:var(--surface2)}
  .badge{font-size:11px;padding:1px 6px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
  .badge.stun{color:var(--red);border-color:var(--red)}
  .log{font-size:12px;color:var(--muted);line-height:1.6;max-height:120px;overflow:auto}
  .win{text-align:center;font-family:serif;font-size:22px;color:var(--gold2);padding:14px}
</style></head><body>
<h1>⚔️ 협곡의 배신자</h1>

<div id="home" class="card">
  <div class="row"><input id="name" placeholder="닉네임" maxlength="12"></div>
  <div class="row" style="margin-top:8px">
    <button onclick="createRoom()">방 만들기</button>
    <input id="code" placeholder="방코드" maxlength="4" style="width:90px;text-transform:uppercase">
    <button class="sec" onclick="joinRoom()">입장</button>
  </div>
  <div class="err" id="homeErr"></div>
</div>

<div id="lobby" class="card" style="display:none">
  <div class="row" style="justify-content:space-between">
    <div>방코드 <b id="lcode" style="color:var(--gold2);letter-spacing:2px"></b></div>
    <button id="startBtn" onclick="startGame()" style="display:none">게임 시작</button>
  </div>
  <div id="lplayers" style="margin-top:10px"></div>
  <div class="muted" style="font-size:12px;margin-top:8px">4~7명이 모이면 방장이 시작할 수 있어요.</div>
</div>

<div id="game" style="display:none">
  <div class="card" id="topbar"></div>
  <div id="board"></div>
  <div class="err" id="gErr"></div>
  <div class="card" id="handbox">
    <div class="row" style="justify-content:space-between">
      <b>내 손패</b>
      <div class="row">
        <button class="sec" id="rotBtn" onclick="rotate()">회전 ↻</button>
        <button class="sec" onclick="doPass()">패스</button>
      </div>
    </div>
    <div class="hand" id="hand"></div>
    <div class="muted" id="hint" style="font-size:12px;margin-top:6px"></div>
  </div>
  <div class="card"><b>플레이어</b><div id="players" style="margin-top:8px"></div></div>
  <div class="card"><b>기록</b><div class="log" id="log"></div></div>
</div>

<script>
const s = io();
let MYID=null, CODE=null, ST=null, sel=null, rot=0, pendingAction=null;
const $=q=>document.querySelector(q);
const esc=t=>(t==null?'':(''+t)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function createRoom(){ s.emit('create_room',{name:$('#name').value}); }
function joinRoom(){ s.emit('join_room',{name:$('#name').value, code:$('#code').value}); }
function startGame(){ s.emit('start_game',{}); }

s.on('err', d=>{ $('#homeErr').textContent=d.msg; $('#gErr').textContent=d.msg; setTimeout(()=>{$('#gErr').textContent='';},2500); });
s.on('joined', d=>{ MYID=d.id; CODE=d.code; });
s.on('lobby', d=>{ renderLobby(d); });
s.on('state', d=>{ ST=d; renderGame(d); });

function show(id){ ['home','lobby','game'].forEach(x=>$('#'+x).style.display = x===id?'':'none'); }

function renderLobby(d){
  show('lobby'); $('#lcode').textContent=d.code;
  $('#lplayers').innerHTML = d.players.map(p=>`<div class="pl"><span>${esc(p.name)}${p.id===d.host?' 👑':''}</span></div>`).join('');
  const isHost = d.host===MYID, ok = d.players.length>=4 && d.players.length<=7;
  const b=$('#startBtn'); b.style.display = isHost?'':'none'; b.disabled=!ok;
}

// ---- 게임 렌더 ----
function renderGame(d){
  show('game');
  const me=d.me;
  const roleTxt = me ? `<span class="role ${me.role==='스파이'?'s':'m'}">${me.role}</span>` : '';
  const turnTxt = d.phase==='진행' ? `지금: <b>${esc(d.turnName)}</b> 차례${me&&me.myTurn?' (당신!)':''}` : '';
  $('#topbar').innerHTML = `<div class="row" style="justify-content:space-between">
    <div>내 역할: ${roleTxt}</div><div>${turnTxt}</div></div>`;

  renderBoard(d);
  // 손패
  if(me){
    $('#hand').innerHTML = me.hand.map((c,i)=>{
      if(c.type==='path'){
        return `<div class="hc ${sel===i?'sel':''}" onclick="pickHand(${i})">${miniTile(c.edges)}</div>`;
      }
      const kor={stun:'스턴',heal:'정화',gank:'갱킹',ward:'와드'}[c.action];
      return `<div class="hc ${sel===i?'sel':''}" onclick="pickHand(${i})"><span class="act">${kor}</span></div>`;
    }).join('');
    updateHint();
  }
  // 플레이어
  $('#players').innerHTML = d.players.map((p,i)=>`<div class="pl ${i===d.turn?'turn':''}">
    <span>${esc(p.name)}${p.id===MYID?' (나)':''} ${p.connected?'':'<span class="badge">이탈</span>'}</span>
    <span>${p.blocked?'<span class="badge stun">스턴</span> ':''}<span class="badge">손 ${p.hand}</span></span></div>`).join('');
  $('#log').innerHTML = d.log.map(l=>`<div>· ${esc(l)}</div>`).reverse().join('');

  if(d.phase==='종료'){
    $('#gErr').innerHTML='';
    $('#topbar').innerHTML = `<div class="win">${d.winner} 승리! 🏆</div>
      <div class="muted" style="text-align:center">${me?('당신은 '+me.role+'):'):''} ${me&&me.role===d.winner?'승리 🎉':'패배'}</div>`;
  }
}

function miniTile(e){
  let h='';
  if(e[0])h+='<span class="pip n"></span>'; if(e[1])h+='<span class="pip e"></span>';
  if(e[2])h+='<span class="pip s"></span>'; if(e[3])h+='<span class="pip w"></span>';
  return h;
}

function renderBoard(d){
  const {rows,cols}=d.meta;
  const bd=$('#board'); bd.style.gridTemplateColumns=`repeat(${cols},46px)`;
  let html='';
  for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){
    const t=d.board[r+','+c];
    let cls='cell', inner='';
    if(!t){ cls+=' empty'; }
    else if(t.kind==='start'){ cls+=' start'; inner=miniTile(t.edges); }
    else if(t.kind==='nexus'){
      cls+=' nexus'; 
      if(t.revealed) cls+= t.real?' real':' fake';
      inner = t.revealed ? (t.real?'★':'✕') : '?';
      // 와드로 본 정보(본인만)
      const w = d.me && d.me.wardSeen && d.me.wardSeen[nexusIndex(d,r,c)];
      if(!t.revealed && d.me && (nexusIndex(d,r,c) in (d.me.wardSeen||{}))) inner = w?'(★)':'(✕)';
    }
    else { inner=miniTile(t.edges); if(!t.conn) cls+=''; }
    const noconn = (t&&t.kind==='path'&&!t.conn)?'<span class="pip" style="width:10px;height:10px;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--red);border-radius:50%"></span>':'';
    html+=`<div class="${cls}" onclick="clickCell(${r},${c})">${inner}${noconn}</div>`;
  }
  bd.innerHTML=html;
}
function nexusIndex(d,r,c){ return d.meta.nexus.findIndex(n=>n[0]===r&&n[1]===c); }

function pickHand(i){
  if(!ST.me||!ST.me.myTurn) return;
  sel=(sel===i?null:i); rot=0; pendingAction=null;
  renderGame(ST);
}
function rotate(){ rot^=1; renderGame(ST); updateHint(); }

function updateHint(){
  const me=ST&&ST.me; if(!me){return;}
  if(!me.myTurn){ $('#hint').textContent='다른 사람 차례입니다.'; return; }
  if(sel===null){ $('#hint').textContent='손패에서 카드를 고르세요.'; return; }
  const c=me.hand[sel];
  if(c.type==='path') $('#hint').textContent=me.blocked?'스턴 상태 — 길을 놓을 수 없습니다(정화 필요).':'빈 칸을 클릭해 길을 놓으세요. (회전 가능)';
  else if(c.action==='gank') $('#hint').textContent='부술 길 카드를 클릭하세요.';
  else if(c.action==='ward') $('#hint').textContent='정찰할 넥서스(?)를 클릭하세요.';
  else $('#hint').textContent='대상 플레이어를 아래 목록에서 클릭하세요.';
}

function clickCell(r,c){
  const me=ST&&ST.me; if(!me||!me.myTurn||sel===null) return;
  const card=me.hand[sel];
  const t=ST.board[r+','+c];
  if(card.type==='path'){
    if(t) return;
    s.emit('act',{kind:'place',payload:{hand:sel,pos:[r,c],rot:rot}}); sel=null;
  } else if(card.action==='gank'){
    if(!t||t.kind!=='path') return;
    s.emit('act',{kind:'action',payload:{hand:sel,pos:[r,c]}}); sel=null;
  } else if(card.action==='ward'){
    const ni=nexusIndex(ST,r,c); if(ni<0) return;
    s.emit('act',{kind:'action',payload:{hand:sel,nexus:ni}}); sel=null;
  }
}

// 플레이어 클릭 = 스턴/정화 대상
$('#players').addEventListener('click',e=>{});
function targetPlayer(pid){
  const me=ST&&ST.me; if(!me||!me.myTurn||sel===null) return;
  const card=me.hand[sel];
  if(card.type==='action'&&(card.action==='stun'||card.action==='heal')){
    s.emit('act',{kind:'action',payload:{hand:sel,target:pid}}); sel=null;
  }
}
// 플레이어 목록에 클릭 핸들러 위임
document.addEventListener('click',e=>{
  const pl=e.target.closest('#players .pl'); if(!pl) return;
  const idx=[...$('#players').children].indexOf(pl);
  if(idx>=0&&ST&&ST.players[idx]) targetPlayer(ST.players[idx].id);
});

function doPass(){
  const me=ST&&ST.me; if(!me||!me.myTurn) return;
  s.emit('act',{kind:'pass',payload:{hand: sel!==null?sel:undefined}}); sel=null;
}
</script>
</body></html>
"""

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
