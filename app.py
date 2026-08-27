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

# 갱신 진행 상태(백그라운드) — gunicorn은 --workers 1 로 실행해야 상태가 일관됨
REFRESH = {"running": False, "updated": 0, "failed": 0, "total": 0,
           "errors": [], "finished_at": None}
REFRESH_LOCK = threading.Lock()


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
            cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS view_count INT DEFAULT 0;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS mastery (
                    player_id   INT REFERENCES players(id) ON DELETE CASCADE,
                    champion_id INT NOT NULL,
                    points      INT NOT NULL,
                    level       INT NOT NULL,
                    PRIMARY KEY (player_id, champion_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS join_requests (
                    id           SERIAL PRIMARY KEY,
                    riot_id      TEXT NOT NULL,
                    requested_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            cur.execute("CREATE TABLE IF NOT EXISTS app_meta (k TEXT PRIMARY KEY, v TIMESTAMPTZ);")
    finally:
        conn.close()


try:
    if DATABASE_URL:
        init_db()
except Exception as e:
    print("init_db 실패(부팅은 계속):", e)


# ---------- Riot ----------
def riot_get(host, path, params=None):
    for _ in range(5):
        r = requests.get(host + path, headers={"X-Riot-Token": RIOT_API_KEY},
                         params=params, timeout=(5, 12))
        if r.status_code == 429:
            time.sleep(min(int(r.headers.get("Retry-After", 3)), 60))
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


def store_player(conn, player_id, game_name, tag_line, puuid=None):
    # 저장된 puuid를 먼저 사용. 키가 바뀌어 400이 나면 이름으로 다시 받아온다.
    data = None
    used = puuid
    if puuid:
        try:
            data = fetch_player_store(puuid)
        except requests.HTTPError as e:
            if not (e.response is not None and e.response.status_code == 400):
                raise
            data = None
    if data is None:
        acc = riot_get(REGION_HOST,
                       f"/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}")
        used = acc["puuid"]
        data = fetch_player_store(used)
    with conn.cursor() as cur:
        cur.execute("""UPDATE players
                       SET puuid=%s, summoner_level=%s, profile_icon_id=%s, updated_at=now()
                       WHERE id=%s""",
                    (used, data["level"], data["iconId"], player_id))
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
    dd = safe_ddragon()
    ver = dd["version"]
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT id, game_name, tag_line, summoner_level, profile_icon_id, updated_at, view_count
                           FROM players
                           ORDER BY summoner_level DESC NULLS LAST, added_at""")
            rows = cur.fetchall()
            cur.execute("""
                SELECT player_id, champion_id FROM (
                    SELECT player_id, champion_id,
                           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY points DESC) AS rn
                    FROM mastery
                ) t WHERE rn <= 5 ORDER BY player_id, rn
            """)
            top_rows = cur.fetchall()
            cur.execute("SELECT player_id, SUM(points) AS total FROM mastery GROUP BY player_id")
            total_by_player = {r["player_id"]: int(r["total"]) for r in cur.fetchall()}
            cur.execute("SELECT v FROM app_meta WHERE k = 'last_refresh'")
            mrow = cur.fetchone()
            refreshed_at = mrow["v"] if mrow else None
    finally:
        conn.close()

    top_by_player = {}
    for r in top_rows:
        top_by_player.setdefault(r["player_id"], []).append(r["champion_id"])

    # 인기쟁이: 조회수 상위 3명 (조회수 0은 제외)
    viewed = sorted([r for r in rows if (r["view_count"] or 0) > 0],
                    key=lambda r: r["view_count"], reverse=True)
    popular_ids = {r["id"] for r in viewed[:3]}

    members = []
    for r in rows:
        champs = []
        for cid in top_by_player.get(r["id"], []):
            info = dd["champions"].get(cid)
            champs.append({"name": info["name"] if info else str(cid),
                           "img": info["id"] if info else None})
        members.append({"id": r["id"], "name": r["game_name"], "tag": r["tag_line"],
                        "level": r["summoner_level"], "iconId": r["profile_icon_id"],
                        "views": r["view_count"] or 0, "popular": r["id"] in popular_ids,
                        "total": total_by_player.get(r["id"], 0),
                        "topMastery": champs})
    return jsonify({"version": ver, "members": members,
                    "refreshedAt": refreshed_at.isoformat() if refreshed_at else None})


@app.route("/api/member/<int:pid>")
def api_member(pid):
    dd = safe_ddragon()
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""UPDATE players SET view_count = COALESCE(view_count,0)+1
                           WHERE id = %s
                           RETURNING game_name, tag_line, summoner_level, profile_icon_id,
                                     updated_at, view_count""", (pid,))
            p = cur.fetchone()
            if not p:
                return jsonify({"error": "회원을 찾을 수 없습니다."}), 404
            cur.execute("SELECT COUNT(*) AS c FROM players WHERE COALESCE(view_count,0) > %s",
                        (p["view_count"],))
            pop_rank = cur.fetchone()["c"] + 1
            cur.execute("""SELECT champion_id, points, level FROM mastery
                           WHERE player_id = %s ORDER BY points DESC""", (pid,))
            mrows = cur.fetchall()
        conn.commit()
    finally:
        conn.close()

    champs, total = [], 0
    for m in mrows:
        info = dd["champions"].get(m["champion_id"])
        champs.append({"name": info["name"] if info else str(m["champion_id"]),
                       "img": info["id"] if info else None,
                       "points": m["points"], "level": m["level"]})
        total += m["points"]
    opgg = "https://www.op.gg/summoners/kr/" + quote(f"{p['game_name']}-{p['tag_line']}")
    return jsonify({
        "version": dd["version"], "name": p["game_name"], "tag": p["tag_line"],
        "level": p["summoner_level"], "iconId": p["profile_icon_id"],
        "total": total, "champCount": len(champs),
        "views": p["view_count"], "popRank": pop_rank if p["view_count"] > 0 else None,
        "updatedAt": p["updated_at"].isoformat() if p["updated_at"] else None,
        "opgg": opgg, "champions": champs,
    })


@app.route("/api/compare")
def api_compare():
    a = request.args.get("a", type=int)
    b = request.args.get("b", type=int)
    if not a or not b or a == b:
        return jsonify({"error": "서로 다른 두 회원을 선택하세요."}), 400
    dd = safe_ddragon()
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            side = {}
            for key, pid in (("a", a), ("b", b)):
                cur.execute("""SELECT game_name, tag_line, summoner_level, profile_icon_id, view_count
                               FROM players WHERE id = %s""", (pid,))
                p = cur.fetchone()
                if not p:
                    return jsonify({"error": "회원을 찾을 수 없습니다."}), 404
                cur.execute("SELECT COALESCE(SUM(points),0) AS total FROM mastery WHERE player_id = %s", (pid,))
                total = int(cur.fetchone()["total"])
                cur.execute("""SELECT champion_id FROM mastery WHERE player_id = %s
                               ORDER BY points DESC LIMIT 5""", (pid,))
                top_ids = [r["champion_id"] for r in cur.fetchall()]
                side[key] = {"name": p["game_name"], "tag": p["tag_line"],
                             "level": p["summoner_level"], "iconId": p["profile_icon_id"],
                             "views": p["view_count"] or 0, "total": total, "_top": top_ids}

            union_ids = list(set(side["a"]["_top"]) | set(side["b"]["_top"]))

            def points_on(pid):
                if not union_ids:
                    return {}
                cur.execute("""SELECT champion_id, points FROM mastery
                               WHERE player_id = %s AND champion_id = ANY(%s)""", (pid, union_ids))
                return {r["champion_id"]: r["points"] for r in cur.fetchall()}
            pa, pb = points_on(a), points_on(b)
    finally:
        conn.close()

    champs = []
    for cid in union_ids:
        info = dd["champions"].get(cid, {})
        champs.append({"championId": cid, "name": info.get("name", str(cid)),
                       "img": info.get("id"), "a": pa.get(cid, 0), "b": pb.get(cid, 0)})
    champs.sort(key=lambda c: c["a"] + c["b"], reverse=True)
    champs = champs[:14]
    for s in (side["a"], side["b"]):
        s.pop("_top", None)
    return jsonify({"version": dd["version"], "a": side["a"], "b": side["b"], "champions": champs})


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


@app.route("/api/champion-detail")
def api_champion_detail():
    cid = request.args.get("championId", type=int)
    if not cid:
        return jsonify({"error": "championId가 필요합니다."}), 400
    dd = safe_ddragon()
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT p.game_name, p.tag_line, m.points, m.level
                           FROM mastery m JOIN players p ON p.id = m.player_id
                           WHERE m.champion_id = %s
                           ORDER BY m.points DESC""", (cid,))
            rows = cur.fetchall()
    finally:
        conn.close()
    champ = dd["champions"].get(cid, {})
    players = [{"name": r["game_name"], "tag": r["tag_line"],
                "points": r["points"], "level": r["level"]} for r in rows]
    return jsonify({"champion": champ.get("name"), "img": champ.get("id"),
                    "version": dd["version"], "players": players})


@app.route("/api/ranking")
def api_ranking():
    dd = safe_ddragon()
    teemo_id = next((cid for cid, info in dd["champions"].items() if info["name"] == "티모"), 17)

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT game_name, tag_line, summoner_level AS val, NULL::int AS champion_id
                           FROM players WHERE summoner_level IS NOT NULL
                           ORDER BY summoner_level DESC""")
            level = cur.fetchall()
            # 회원별 최고 숙련 챔피언 1개씩 → 파이썬에서 val 내림차순 정렬
            cur.execute("""SELECT DISTINCT ON (p.id) p.game_name, p.tag_line, m.points AS val, m.champion_id
                           FROM mastery m JOIN players p ON p.id = m.player_id
                           ORDER BY p.id, m.points DESC""")
            single = cur.fetchall()
            single.sort(key=lambda r: r["val"], reverse=True)
            cur.execute("""SELECT p.game_name, p.tag_line, SUM(m.points) AS val, NULL::int AS champion_id
                           FROM mastery m JOIN players p ON p.id = m.player_id
                           GROUP BY p.id, p.game_name, p.tag_line
                           ORDER BY val DESC""")
            total = cur.fetchall()
            cur.execute("""SELECT p.game_name, p.tag_line, COUNT(*) AS val, NULL::int AS champion_id
                           FROM (
                             SELECT DISTINCT ON (champion_id) champion_id, player_id
                             FROM mastery ORDER BY champion_id, points DESC
                           ) t JOIN players p ON p.id = t.player_id
                           GROUP BY p.id, p.game_name, p.tag_line
                           ORDER BY val DESC""")
            most1st = cur.fetchall()
            cur.execute("""SELECT p.game_name, p.tag_line, m.points AS val, m.champion_id
                           FROM mastery m JOIN players p ON p.id = m.player_id
                           WHERE m.champion_id = %s ORDER BY m.points DESC""", (teemo_id,))
            teemo = cur.fetchall()
    finally:
        conn.close()

    def simp(rows, with_champ=False):
        out = []
        for r in rows:
            e = {"name": r["game_name"], "tag": r["tag_line"], "value": int(r["val"])}
            if with_champ and r.get("champion_id") is not None:
                info = dd["champions"].get(r["champion_id"])
                if info:
                    e["champName"] = info["name"]
                    e["champImg"] = info["id"]
            out.append(e)
        return out

    rankings = [
        {"key": "level", "title": "최고 레벨", "unit": "레벨", "players": simp(level)},
        {"key": "single", "title": "단일 챔피언 최고 숙련", "unit": "점", "players": simp(single, True)},
        {"key": "total", "title": "숙련도 총합", "unit": "점", "players": simp(total)},
        {"key": "most1st", "title": "챔피언 1등 최다", "unit": "개", "players": simp(most1st)},
        {"key": "teemo", "title": "귀여운 티모 숙련도 1등", "unit": "점",
         "players": simp(teemo), "img": dd["champions"].get(teemo_id, {}).get("id")},
    ]
    return jsonify({"version": dd["version"], "rankings": rankings})


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


def add_member(raw):
    """라이엇 ID 문자열로 회원을 해석·등록·저장. (status_code, body dict) 반환."""
    raw = (raw or "").strip()
    if not raw:
        return 400, {"error": "라이엇 ID를 입력하세요."}
    if "#" in raw:
        name, tag = raw.rsplit("#", 1)
    else:
        name, tag = raw, "KR1"
    name, tag = name.strip(), tag.strip()
    try:
        acc = riot_get(REGION_HOST, f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}")
    except requests.HTTPError as e:
        code = e.response.status_code
        if code == 404:
            return 404, {"error": "라이엇 ID를 찾을 수 없습니다."}
        if code in (401, 403):
            return 401, {"error": "API 키가 만료/무효입니다."}
        return 502, {"error": f"조회 오류 (HTTP {code})"}

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

    if new_id:
        try:
            c = get_db()
            try:
                store_player(c, new_id, real_name, real_tag, puuid)
            finally:
                c.close()
        except Exception:
            pass
    return 200, {"ok": True, "added": bool(new_id), "name": f"{real_name}#{real_tag}"}


@app.route("/api/admin/add", methods=["POST"])
@admin_required
def admin_add():
    code, body = add_member((request.json or {}).get("riotId"))
    return jsonify(body), code


# ---------- 회원 등록 요청 (공개) ----------
@app.route("/api/request-join", methods=["POST"])
def request_join():
    raw = ((request.json or {}).get("riotId") or "").strip()
    if not raw or len(raw) > 60:
        return jsonify({"error": "라이엇 ID를 올바르게 입력하세요."}), 400
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM players WHERE lower(game_name || '#' || tag_line) = lower(%s)", (raw,))
            if cur.fetchone():
                return jsonify({"ok": True, "already": "member"})
            cur.execute("SELECT 1 FROM join_requests WHERE lower(riot_id) = lower(%s)", (raw,))
            if cur.fetchone():
                return jsonify({"ok": True, "already": "request"})
            cur.execute("INSERT INTO join_requests (riot_id) VALUES (%s)", (raw,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/requests")
@admin_required
def admin_requests():
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, riot_id, requested_at FROM join_requests ORDER BY requested_at")
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["requested_at"] = r["requested_at"].isoformat() if r["requested_at"] else None
    return jsonify({"requests": rows})


@app.route("/api/admin/approve", methods=["POST"])
@admin_required
def admin_approve():
    rid = (request.json or {}).get("id")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT riot_id FROM join_requests WHERE id = %s", (rid,))
            r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return jsonify({"error": "요청을 찾을 수 없습니다."}), 404
    code, body = add_member(r[0])
    if code == 200:
        conn = get_db()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("DELETE FROM join_requests WHERE id = %s", (rid,))
        finally:
            conn.close()
    return jsonify(body), code


@app.route("/api/admin/reject", methods=["POST"])
@admin_required
def admin_reject():
    rid = (request.json or {}).get("id")
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM join_requests WHERE id = %s", (rid,))
    finally:
        conn.close()
    return jsonify({"ok": True})


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


def do_refresh():
    try:
        conn = get_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id, game_name, tag_line, puuid FROM players")
                players = cur.fetchall()
        finally:
            conn.close()
        with REFRESH_LOCK:
            REFRESH["total"] = len(players)

        def work(p):
            try:
                c = get_db()
            except Exception as e:
                with REFRESH_LOCK:
                    REFRESH["failed"] += 1
                    if len(REFRESH["errors"]) < 3:
                        REFRESH["errors"].append(f"DB연결: {e}")
                return
            try:
                store_player(c, p["id"], p["game_name"], p["tag_line"], p["puuid"])
                with REFRESH_LOCK:
                    REFRESH["updated"] += 1
            except Exception as e:
                print("refresh error:", repr(e))
                with REFRESH_LOCK:
                    REFRESH["failed"] += 1
                    if len(REFRESH["errors"]) < 3:
                        REFRESH["errors"].append(f"{type(e).__name__}: {e}")
            finally:
                try:
                    c.close()
                except Exception:
                    pass

        if players:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                list(ex.map(work, players))
        conn2 = get_db()
        try:
            with conn2, conn2.cursor() as cur:
                cur.execute("""INSERT INTO app_meta (k, v) VALUES ('last_refresh', now())
                               ON CONFLICT (k) DO UPDATE SET v = now()""")
        finally:
            conn2.close()
    finally:
        with REFRESH_LOCK:
            REFRESH["running"] = False
            REFRESH["finished_at"] = time.time()


@app.route("/api/admin/refresh", methods=["POST"])
@admin_required
def admin_refresh():
    if not RIOT_API_KEY:
        return jsonify({"error": "서버에 RIOT_API_KEY가 없습니다."}), 500
    with REFRESH_LOCK:
        if REFRESH["running"]:
            return jsonify({"ok": True, "already": True})
        REFRESH.update(running=True, updated=0, failed=0, total=0, errors=[], finished_at=None)
    threading.Thread(target=do_refresh, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/admin/refresh-status")
@admin_required
def admin_refresh_status():
    with REFRESH_LOCK:
        return jsonify(dict(REFRESH))


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
<title>아럽롤 회원 대시보드</title>
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
  .mini{display:flex;gap:4px;margin-top:5px}
  .mini img{width:22px;height:22px;border-radius:5px;border:1px solid var(--line)}
  .pop{display:inline-block;background:rgba(200,170,110,.16);color:var(--gold-bright);border:1px solid var(--gold);
       border-radius:99px;padding:1px 8px;font-size:11px;margin-left:6px;vertical-align:middle;white-space:nowrap}
  .clickable{cursor:pointer}
  .clickable:hover{border-color:var(--gold)}
  .back{background:transparent;border:1px solid var(--line);color:var(--muted);padding:8px 14px}
  .back:hover{background:var(--surface2);color:var(--text)}
  .bar-champ{display:flex;align-items:center;gap:10px;margin:12px 2px 16px}
  .bar-champ img{width:46px;height:46px;border-radius:9px;border:1px solid var(--line)}
  .stats{display:flex;gap:10px;margin:0 0 16px}
  .stat{flex:1;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center}
  .s-val{font-family:'JetBrains Mono',monospace;color:var(--gold-bright);font-size:18px}
  .s-lab{color:var(--muted);font-size:12px;margin-top:2px}
  .opgg{align-self:center;background:transparent;border:1px solid var(--blue);color:var(--blue);
        border-radius:9px;padding:8px 14px;font-size:13px;font-weight:600;text-decoration:none;white-space:nowrap}
  .opgg:hover{background:var(--blue);color:#fff}
  .reqbox{display:flex;gap:8px;margin:18px 0 6px}
  .reqbox input{flex:1}
  .reqmsg{color:var(--muted);font-size:12.5px;margin:0 2px 18px;min-height:16px}
  .mbar{display:flex;gap:8px;margin-bottom:12px}
  .mbar select{flex:0 0 auto}
  .mbar input{flex:1}
  .rk-card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
  .rk-title{font-family:'Marcellus',serif;font-size:17px;margin-bottom:8px;display:flex;align-items:center;gap:8px}
  .rk-title img{width:24px;height:24px;border-radius:6px;border:1px solid var(--line)}
  .rk-row{display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--line)}
  .rk-row:first-of-type{border-top:none}
  .rk-no{font-family:'JetBrains Mono',monospace;color:var(--muted);width:18px;text-align:center}
  .rk-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rk-champ{width:22px;height:22px;border-radius:5px;border:1px solid var(--line)}
  .rk-val{font-family:'JetBrains Mono',monospace;color:var(--gold-bright)}
  .rk-val small{color:var(--muted);font-weight:400;font-size:11px;margin-left:2px}
  .rk-row.first .rk-name{font-weight:600;color:var(--gold-bright)}
  .rk-row.first .rk-no{color:var(--gold)}
  .rk-card.expandable{cursor:pointer}
  .rk-card .rk-row.extra{display:none}
  .rk-card.open .rk-row.extra{display:flex}
  .rk-caret{margin-left:auto;color:var(--muted);font-size:12px;transition:transform .15s}
  .rk-card.open .rk-caret{transform:rotate(180deg)}
  .cmp-pick{display:flex;gap:8px;align-items:center}
  .cmp-pick select{flex:1;min-width:0}
  .cmp-pick .vsm{font-family:'Marcellus',serif;color:var(--gold)}
  .faceoff{display:flex;align-items:center;justify-content:space-between;margin:20px 0 4px}
  .faceoff .fighter{flex:1;text-align:center;min-width:0}
  .faceoff img{width:64px;height:64px;border-radius:14px;border:1px solid var(--line)}
  .faceoff .fn{margin-top:6px;font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .faceoff .vs-big{font-family:'Marcellus',serif;font-size:26px;color:var(--gold);padding:0 8px}
  .round-title{text-align:center;font-family:'Marcellus',serif;font-size:16px;color:var(--gold);margin:20px 0 10px}
  .duel{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;margin-bottom:7px;background:var(--surface)}
  .duel .side{min-width:0}
  .duel .side.a{text-align:right}
  .duel .side.b{text-align:left}
  .duel .val{font-family:'JetBrains Mono',monospace;font-size:15px}
  .duel .side.win .val{color:var(--gold-bright);text-shadow:0 0 12px rgba(200,170,110,.6);font-weight:700}
  .duel .lab{color:var(--muted);font-size:12px;text-align:center;white-space:nowrap}
  .duel .champ-lab{display:flex;align-items:center;gap:6px;justify-content:center;color:var(--text)}
  .duel .champ-lab img{width:24px;height:24px;border-radius:5px}
  .round-sum{text-align:center;color:var(--muted);font-size:13px;margin:2px 0 6px}
  .round-sum b{color:var(--gold-bright)}
  .verdict{text-align:center;margin-top:20px;padding:20px;border-radius:14px;border:1px solid var(--gold);background:rgba(200,170,110,.10)}
  .verdict .who{font-family:'Marcellus',serif;font-size:26px;color:var(--gold-bright);display:block;margin:4px 0}
  .verdict .score{color:var(--muted);font-size:13px;margin-top:4px}
  @keyframes slideL{from{opacity:0;transform:translateX(-24px)}to{opacity:1;transform:none}}
  @keyframes slideR{from{opacity:0;transform:translateX(24px)}to{opacity:1;transform:none}}
  @keyframes pop{0%{opacity:0;transform:scale(.85)}60%{transform:scale(1.05)}100%{opacity:1;transform:scale(1)}}
  .faceoff .left{animation:slideL .5s both}
  .faceoff .right{animation:slideR .5s both}
  .reveal{opacity:0;transform:translateY(8px)}
  .reveal.show{opacity:1;transform:none;transition:opacity .3s, transform .3s}
  .verdict.show{animation:pop .55s both}
</style></head><body>
<div class="wrap">
  <div class="eyebrow">League of Legends</div>
  <h1>아럽롤 회원 대시보드</h1>

  <div class="reqbox">
    <input id="reqId" placeholder="회원 등록 요청 — 소환사명#KR1" autocomplete="off">
    <button id="reqBtn">요청</button>
    <button onclick="location.href='/admin'">관리자</button>
  </div>
  <div id="reqMsg" class="reqmsg"></div>

  <div class="tabs">
    <div class="tab on" data-tab="members">회원 리스트</div>
    <div class="tab" data-tab="mastery">챔피언 숙련도</div>
    <div class="tab" data-tab="ranking">랭킹</div>
    <div class="tab" data-tab="compare">비교</div>
  </div>
  <div class="updated" id="updated"></div>

  <div id="view-members">
    <div id="member-browse">
      <div class="mbar">
        <select id="msort">
          <option value="level">레벨순</option>
          <option value="name">이름순</option>
          <option value="total">총 숙련도순</option>
          <option value="views">조회수순</option>
        </select>
        <input id="msearch" placeholder="회원 이름 검색…" autocomplete="off">
      </div>
      <div id="status">불러오는 중…</div><div id="member-list"></div>
    </div>
    <div id="member-detail" style="display:none"></div>
  </div>

  <div id="view-mastery" style="display:none">
    <div id="champ-browse">
      <input id="search" placeholder="챔피언 이름 검색…" autocomplete="off">
      <div id="champ-list"><div class="muted" style="text-align:center;padding:16px">불러오는 중…</div></div>
    </div>
    <div id="champ-detail" style="display:none"></div>
  </div>

  <div id="view-ranking" style="display:none">
    <div id="ranking-list"><div class="muted" style="text-align:center;padding:16px">불러오는 중…</div></div>
  </div>

  <div id="view-compare" style="display:none">
    <div class="cmp-pick">
      <select id="cmpA"></select>
      <span class="vsm">VS</span>
      <select id="cmpB"></select>
    </div>
    <button id="cmpBtn" style="width:100%;margin-top:8px">⚔️ 대결 시작</button>
    <div id="cmp-result"></div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
let VERSION=null, CHAMPS=[], MEMBERS=[];
const dd=sub=>`https://ddragon.leagueoflegends.com/cdn/${VERSION}/img/${sub}`;
const esc=s=>(s==null?'':(''+s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadMembers(){
  try{
    const r=await fetch('/api/members'); const d=await r.json();
    VERSION=d.version; $('#status').style.display='none';
    MEMBERS=d.members||[];
    $('#updated').textContent = d.refreshedAt
      ? ('마지막 전체 갱신: '+d.refreshedAt.slice(0,16).replace('T',' '))
      : '아직 전체 갱신 안 됨 — 관리자 페이지에서 정보 갱신을 눌러주세요.';
    renderMembers();
  }catch(e){ $('#status').textContent='불러오기 실패'; }
}

function renderMembers(){
  const sort=$('#msort').value, q=$('#msearch').value.trim();
  let list=MEMBERS.slice();
  if(q) list=list.filter(m=>m.name.includes(q));
  const cmp={
    level:(a,b)=>(b.level??-1)-(a.level??-1),
    total:(a,b)=>(b.total||0)-(a.total||0),
    views:(a,b)=>(b.views||0)-(a.views||0),
    name:(a,b)=>a.name.localeCompare(b.name,'ko'),
  }[sort]||((a,b)=>0);
  list.sort(cmp);
  if(!list.length){ $('#member-list').innerHTML='<div class="muted" style="text-align:center;padding:24px">'+(q?'검색 결과가 없습니다.':'등록된 회원이 없습니다.')+'</div>'; return; }
  $('#member-list').innerHTML=list.map((m,i)=>{
    const icon=(VERSION&&m.iconId!=null)?`<img class="icon" src="${dd('profileicon/'+m.iconId+'.png')}">`:`<div class="icon"></div>`;
    const mini=(VERSION&&m.topMastery&&m.topMastery.length)
      ? `<div class="mini">${m.topMastery.map(c=>c.img?`<img title="${esc(c.name)}" src="${dd('champion/'+c.img+'.png')}">`:'').join('')}</div>` : '';
    const pop=m.popular?` <span class="pop" title="조회 ${m.views}회">🔥 인기쟁이</span>`:'';
    return `<div class="row clickable" onclick="showMemberDetail(${m.id})"><div class="no">${i+1}</div>${icon}
      <div class="who"><div class="nm">${esc(m.name)} <span class="sm">#${esc(m.tag)}</span>${pop}</div>${mini}</div>
      <div class="lvl">${m.level??'-'} <small>레벨</small></div></div>`;
  }).join('');
}

async function showMemberDetail(pid){
  $('#member-browse').style.display='none';
  $('#member-detail').style.display='';
  $('#member-detail').innerHTML='<div class="muted" style="text-align:center;padding:16px">불러오는 중…</div>';
  try{
    const r=await fetch('/api/member/'+pid); const d=await r.json();
    if(!r.ok){ $('#member-detail').innerHTML=`<button class="back" onclick="backToMembers()">← 목록으로</button><div class="norec" style="text-align:center;padding:12px">${esc(d.error||'오류')}</div>`; return; }
    VERSION=d.version||VERSION;
    const icon=(VERSION&&d.iconId!=null)?`<img src="${dd('profileicon/'+d.iconId+'.png')}">`:'';
    let h=`<button class="back" onclick="backToMembers()">← 목록으로</button>`;
    h+=`<div class="bar-champ">${icon}<div style="flex:1"><b>${esc(d.name)}</b> <span class="muted">#${esc(d.tag)}</span><div class="muted" style="font-size:13px">Lv.${d.level??'-'} · 조회 ${d.views??0}회${d.popRank?` · 인기 ${d.popRank}위`:''}</div></div>`;
    if(d.opgg){ h+=`<a class="opgg" href="${d.opgg}" target="_blank" rel="noopener">OP.GG ↗</a>`; }
    h+=`</div>`;
    h+=`<div class="stats">
        <div class="stat"><div class="s-val">${d.total.toLocaleString()}</div><div class="s-lab">총 숙련도</div></div>
        <div class="stat"><div class="s-val">${d.champCount}</div><div class="s-lab">보유 챔피언</div></div>
        <div class="stat"><div class="s-val">${d.level??'-'}</div><div class="s-lab">레벨</div></div>
      </div>`;
    if(!d.champions.length){ h+='<div class="muted" style="text-align:center;padding:12px">숙련도 기록이 없습니다. 관리자 갱신이 필요할 수 있어요.</div>'; }
    else{ h+=d.champions.map((c,i)=>{
      const ci=(VERSION&&c.img)?`<img class="icon" src="${dd('champion/'+c.img+'.png')}">`:`<div class="icon"></div>`;
      return `<div class="row"><div class="no">${i+1}</div>${ci}
        <div class="who"><div class="nm">${esc(c.name)}</div><div class="sm">숙련도 ${c.level}레벨</div></div>
        <div class="lvl"><span class="pts">${c.points.toLocaleString()}</span> <small>점</small></div></div>`;
    }).join(''); }
    $('#member-detail').innerHTML=h;
  }catch(e){ $('#member-detail').innerHTML='<button class="back" onclick="backToMembers()">← 목록으로</button><div class="muted" style="text-align:center;padding:16px">불러오기 실패</div>'; }
}
function backToMembers(){ $('#member-detail').style.display='none'; $('#member-browse').style.display=''; }

async function submitRequest(){
  const v=$('#reqId').value.trim(); if(!v) return;
  $('#reqBtn').disabled=true; $('#reqMsg').textContent='요청 중…';
  try{
    const r=await fetch('/api/request-join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({riotId:v})});
    const d=await r.json();
    if(!r.ok){ $('#reqMsg').textContent=d.error||'오류가 발생했습니다.'; }
    else if(d.already==='member'){ $('#reqMsg').textContent='이미 등록된 회원입니다.'; }
    else if(d.already==='request'){ $('#reqMsg').textContent='이미 등록 요청된 아이디입니다.'; }
    else{ $('#reqMsg').textContent='등록 요청이 접수되었습니다. 관리자 승인 후 추가됩니다.'; $('#reqId').value=''; }
  }catch(e){ $('#reqMsg').textContent='요청에 실패했습니다.'; }
  finally{ $('#reqBtn').disabled=false; }
}
$('#msort').addEventListener('change', renderMembers);
$('#msearch').addEventListener('input', renderMembers);
$('#reqBtn').addEventListener('click', submitRequest);
$('#reqId').addEventListener('keydown', e=>{ if(e.key==='Enter') submitRequest(); });

function renderChamps(filter){
  const f=(filter||'').trim();
  const list=f?CHAMPS.filter(c=>c.name.includes(f)):CHAMPS;
  if(!list.length){ $('#champ-list').innerHTML='<div class="muted" style="text-align:center;padding:16px">해당 챔피언이 없습니다.</div>'; return; }
  $('#champ-list').innerHTML=list.map(c=>{
    const icon=VERSION?`<img class="icon" src="${dd('champion/'+c.img+'.png')}">`:`<div class="icon"></div>`;
    const top=c.top
      ? `<div class="who"><div class="top-who">${esc(c.top.name)} <span class="muted">#${esc(c.top.tag)}</span></div></div><div class="lvl"><span class="pts">${c.top.points.toLocaleString()}</span> <small>점</small></div>`
      : `<div class="who"><span class="norec">기록 없음</span></div>`;
    return `<div class="row clickable" onclick="showChampDetail(${c.championId})">${icon}<div style="min-width:90px"><span class="champ-nm">${esc(c.name)}</span></div>${top}</div>`;
  }).join('');
}

async function showChampDetail(cid){
  const c=CHAMPS.find(x=>x.championId===cid);
  $('#champ-browse').style.display='none';
  $('#champ-detail').style.display='';
  $('#champ-detail').innerHTML='<div class="muted" style="text-align:center;padding:16px">불러오는 중…</div>';
  try{
    const r=await fetch('/api/champion-detail?championId='+cid);
    const d=await r.json();
    const img=(c&&c.img)||d.img;
    let h=`<button class="back" onclick="backToList()">← 목록으로</button>`;
    h+=`<div class="bar-champ">${VERSION?`<img src="${dd('champion/'+img+'.png')}">`:''}<div><b>${esc(d.champion||(c&&c.name))}</b> 숙련도 순위</div></div>`;
    if(!d.players.length){ h+='<div class="muted" style="text-align:center;padding:12px">이 챔피언 숙련도 기록이 있는 회원이 없습니다.</div>'; }
    else{ h+=d.players.map((p,i)=>`<div class="row"><div class="no">${i+1}</div>
      <div class="who"><div class="nm">${esc(p.name)} <span class="muted">#${esc(p.tag)}</span></div>
        <div class="sm">숙련도 ${p.level}레벨</div></div>
      <div class="lvl"><span class="pts">${p.points.toLocaleString()}</span> <small>점</small></div></div>`).join(''); }
    $('#champ-detail').innerHTML=h;
  }catch(e){ $('#champ-detail').innerHTML='<button class="back" onclick="backToList()">← 목록으로</button><div class="muted" style="text-align:center;padding:16px">불러오기 실패</div>'; }
}
function backToList(){ $('#champ-detail').style.display='none'; $('#champ-browse').style.display=''; }

async function loadChamps(){
  try{
    const r=await fetch('/api/champion-top'); const d=await r.json();
    VERSION=d.version||VERSION; CHAMPS=d.champions||[];
    renderChamps('');
  }catch(e){ $('#champ-list').innerHTML='<div class="muted" style="text-align:center">불러오기 실패</div>'; }
}

let RANK_LOADED=false;
async function loadRanking(){
  try{
    const r=await fetch('/api/ranking'); const d=await r.json();
    VERSION=d.version||VERSION;
    $('#ranking-list').innerHTML=d.rankings.map(cat=>{
      const head=(cat.img&&VERSION)?`<img src="${dd('champion/'+cat.img+'.png')}">`:'';
      const players=cat.players||[];
      const body=players.length
        ? players.map((p,i)=>{
            const champ=(p.champImg&&VERSION)?`<img class="rk-champ" title="${esc(p.champName)}" src="${dd('champion/'+p.champImg+'.png')}">`:'';
            const cls='rk-row'+(i===0?' first':'')+(i>=3?' extra':'');
            return `<div class="${cls}"><span class="rk-no">${i+1}</span>
              <span class="rk-name">${esc(p.name)} <span class="muted">#${esc(p.tag)}</span></span>
              ${champ}<span class="rk-val">${p.value.toLocaleString()}<small>${esc(cat.unit)}</small></span></div>`;
          }).join('')
        : '<div class="muted" style="padding:6px 2px">기록 없음</div>';
      const expandable=players.length>3;
      const caret=expandable?'<span class="rk-caret">▾</span>':'';
      const cls=expandable?'rk-card expandable':'rk-card';
      const onclick=expandable?" onclick=\"this.classList.toggle('open')\"":'';
      return `<div class="${cls}"${onclick}><div class="rk-title">${head}${esc(cat.title)}${caret}</div>${body}</div>`;
    }).join('');
  }catch(e){ $('#ranking-list').innerHTML='<div class="muted" style="text-align:center">불러오기 실패</div>'; }
}

$('#search').addEventListener('input', e=>renderChamps(e.target.value));

function populateCompare(){
  if(!MEMBERS.length) return;
  const sorted=MEMBERS.slice().sort((a,b)=>a.name.localeCompare(b.name,'ko'));
  const opts='<option value="">회원 선택…</option>'+sorted.map(m=>`<option value="${m.id}">${esc(m.name)} #${esc(m.tag)}</option>`).join('');
  if($('#cmpA').options.length<=1){ $('#cmpA').innerHTML=opts; $('#cmpB').innerHTML=opts; }
}
const nf=n=>(n==null?0:n).toLocaleString();

async function runBattle(){
  const a=$('#cmpA').value, b=$('#cmpB').value;
  const box=$('#cmp-result');
  if(!a||!b){ box.innerHTML='<div class="norec" style="text-align:center;padding:12px">두 회원을 선택하세요.</div>'; return; }
  if(a===b){ box.innerHTML='<div class="norec" style="text-align:center;padding:12px">서로 다른 회원을 선택하세요.</div>'; return; }
  box.innerHTML='<div class="muted" style="text-align:center;padding:16px">대결 준비 중…</div>';
  try{
    const r=await fetch(`/api/compare?a=${a}&b=${b}`); const d=await r.json();
    if(!r.ok){ box.innerHTML=`<div class="norec" style="text-align:center;padding:12px">${esc(d.error||'오류')}</div>`; return; }
    VERSION=d.version||VERSION; renderBattle(d);
  }catch(e){ box.innerHTML='<div class="norec" style="text-align:center;padding:12px">불러오기 실패</div>'; }
}

function fico(P){ return (VERSION&&P.iconId!=null)?`<img src="${dd('profileicon/'+P.iconId+'.png')}">`:'<div class="icon"></div>'; }
function sumText(aw,bw,A,B){
  if(aw>bw) return `<b>${esc(A.name)}</b> 우세 (${aw} : ${bw})`;
  if(bw>aw) return `<b>${esc(B.name)}</b> 우세 (${bw} : ${aw})`;
  return `무승부 (${aw} : ${bw})`;
}

function renderBattle(d){
  const A=d.a, B=d.b;
  const stats=[
    {lab:'레벨', a:A.level||0, b:B.level||0},
    {lab:'총 숙련도', a:A.total||0, b:B.total||0},
    {lab:'조회수', a:A.views||0, b:B.views||0},
  ];
  let aw=0,bw=0;
  stats.forEach(s=>{ s.w=s.a>s.b?'a':(s.b>s.a?'b':''); if(s.w==='a')aw++; else if(s.w==='b')bw++; });
  const champs=d.champions||[];
  let ca=0,cb=0;
  champs.forEach(c=>{ c.w=c.a>c.b?'a':(c.b>c.a?'b':''); if(c.w==='a')ca++; else if(c.w==='b')cb++; });
  const tA=aw+ca, tB=bw+cb;

  let h=`<div class="faceoff">
    <div class="fighter left">${fico(A)}<div class="fn">${esc(A.name)}</div></div>
    <div class="vs-big">VS</div>
    <div class="fighter right">${fico(B)}<div class="fn">${esc(B.name)}</div></div>
  </div>`;
  h+=`<div class="round-title reveal">⚔️ ROUND 1 · 스탯 대결</div>`;
  stats.forEach(s=>{
    h+=`<div class="duel reveal">
      <div class="side a ${s.w==='a'?'win':''}"><span class="val">${nf(s.a)}</span></div>
      <div class="lab">${s.lab}</div>
      <div class="side b ${s.w==='b'?'win':''}"><span class="val">${nf(s.b)}</span></div></div>`;
  });
  h+=`<div class="round-sum reveal">1라운드 — ${sumText(aw,bw,A,B)}</div>`;
  h+=`<div class="round-title reveal">⚔️ ROUND 2 · 챔피언 대결 (Top 5)</div>`;
  if(!champs.length){ h+=`<div class="reveal muted" style="text-align:center;padding:8px">공통으로 비교할 챔피언이 없습니다.</div>`; }
  champs.forEach(c=>{
    const ci=(VERSION&&c.img)?`<img src="${dd('champion/'+c.img+'.png')}">`:'';
    h+=`<div class="duel reveal">
      <div class="side a ${c.w==='a'?'win':''}"><span class="val">${nf(c.a)}</span></div>
      <div class="lab champ-lab">${ci}<span>${esc(c.name)}</span></div>
      <div class="side b ${c.w==='b'?'win':''}"><span class="val">${nf(c.b)}</span></div></div>`;
  });
  h+=`<div class="round-sum reveal">2라운드 — ${sumText(ca,cb,A,B)}</div>`;
  let verdict;
  if(tA>tB) verdict=`🏆<span class="who">${esc(A.name)} 승리!</span>`;
  else if(tB>tA) verdict=`🏆<span class="who">${esc(B.name)} 승리!</span>`;
  else verdict=`🤝<span class="who">무승부!</span>`;
  h+=`<div class="verdict reveal">${verdict}<div class="score">종합 ${tA} : ${tB}</div></div>`;

  $('#cmp-result').innerHTML=h;
  const els=[...document.querySelectorAll('#cmp-result .reveal')];
  els.forEach((el,i)=> setTimeout(()=>el.classList.add('show'), 250+i*220));
}
$('#cmpBtn').addEventListener('click', runBattle);

document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on')); t.classList.add('on');
  backToList(); backToMembers();
  const tab=t.dataset.tab;
  $('#view-members').style.display = tab==='members'?'':'none';
  $('#view-mastery').style.display = tab==='mastery'?'':'none';
  $('#view-ranking').style.display = tab==='ranking'?'':'none';
  $('#view-compare').style.display = tab==='compare'?'':'none';
  if(tab==='ranking' && !RANK_LOADED){ RANK_LOADED=true; loadRanking(); }
  if(tab==='compare') populateCompare();
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
  .appr{background:transparent;border:1px solid var(--gold);color:var(--gold);padding:6px 12px;margin-right:6px}
  .appr:hover{background:var(--gold);color:#1a1204}
  .reqhdr{font-family:'Marcellus',serif;font-size:16px;margin:0 2px 8px}
  .reqhdr .badge{font-family:'Inter';font-size:12px;color:#1a1204;background:var(--gold);border-radius:99px;padding:0 7px;margin-left:6px}
  .reqtime{color:var(--muted);font-size:11px}
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
      <div id="reqList" style="margin-top:16px"></div>
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
  $('#msg').innerHTML='<div class="muted">갱신 시작 중…</div>';
  try{
    const r=await fetch('/api/admin/refresh',{method:'POST'}); const d=await r.json();
    if(!r.ok){ $('#msg').innerHTML=`<div class="err">${esc(d.error||'오류')}</div>`; btn.disabled=false; return; }
    pollStatus();
  }catch(e){ $('#msg').innerHTML='<div class="err">갱신 시작 실패</div>'; btn.disabled=false; }
}
async function pollStatus(){
  try{
    const r=await fetch('/api/admin/refresh-status'); const s=await r.json();
    if(s.running){
      $('#msg').innerHTML=`<div class="muted">갱신 중… ${s.updated+s.failed}/${s.total||'?'}</div>`;
      setTimeout(pollStatus,2000);
    }else{
      let m=`<div class="ok">${s.updated}명 갱신 완료${s.failed?`, ${s.failed}명 실패`:''}</div>`;
      if(s.errors&&s.errors.length){ m+=`<div class="err">사유: ${esc(s.errors.join(' | '))}</div>`; }
      $('#msg').innerHTML=m; $('#refreshBtn').disabled=false; loadList();
    }
  }catch(e){ setTimeout(pollStatus,2500); }
}
async function loadRequests(){
  const r=await fetch('/api/admin/requests'); if(!r.ok) return;
  const d=await r.json();
  if(!d.requests.length){ $('#reqList').innerHTML=''; return; }
  $('#reqList').innerHTML=`<div class="reqhdr">등록 요청<span class="badge">${d.requests.length}</span></div>`+
    d.requests.map(q=>`<div class="prow"><span class="nm">${esc(q.riot_id)}<div class="reqtime">${q.requested_at?esc(q.requested_at.slice(0,16).replace('T',' ')):''}</div></span>
      <button class="appr" onclick="approveReq(${q.id})">승인</button>
      <button class="del" onclick="rejectReq(${q.id})">거절</button></div>`).join('');
}
async function approveReq(id){
  $('#msg').innerHTML='<div class="muted">승인 처리 중…</div>';
  const r=await fetch('/api/admin/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d=await r.json();
  if(!r.ok){ $('#msg').innerHTML=`<div class="err">${esc(d.error||'오류')}</div>`; }
  else{ $('#msg').innerHTML=`<div class="ok">${d.added?'등록됨':'이미 등록됨'}: ${esc(d.name||'')}</div>`; }
  loadRequests(); loadList();
}
async function rejectReq(id){
  await fetch('/api/admin/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadRequests();
}
if($('#addBtn')){
  $('#addBtn').addEventListener('click',addPlayer);
  $('#riotId').addEventListener('keydown',e=>{if(e.key==='Enter')addPlayer();});
  $('#refreshBtn').addEventListener('click',refreshAll);
  loadRequests(); loadList();
}
</script></body></html>
"""
ADMIN_PAGE = ADMIN_PAGE.replace("__THEME__", THEME)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
