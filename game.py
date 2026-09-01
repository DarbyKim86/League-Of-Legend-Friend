"""
협곡의 배신자 — 코어 게임 로직 (MVP)
- 순수 로직만. 서버/네트워크 없음 (game_server.py가 이걸 사용).
- 보드: 격자. 길 카드 = 4방향(N,E,S,W) 통로 개폐 + 중앙 관통(conn) 여부.
- 자동 검증: 놓기 가능 판정 + 본진→넥서스 실제 연결 BFS.
- 히든롤: view_for(pid)가 그 플레이어가 볼 수 있는 것만 조립.
"""
import random

# 방향: N,E,S,W (index 0,1,2,3) / 좌표 이동 / 반대변
DIRS = [(-1, 0), (0, 1), (1, 0), (0, -1)]
OPP = [2, 3, 0, 1]

# 보드 크기
ROWS = 5           # 0..4, 중앙 2
START = (2, 0)
NEXUS_COL = 8
NEXUS_CELLS = [(1, NEXUS_COL), (2, NEXUS_COL), (3, NEXUS_COL)]
MIN_COL, MAX_COL = 1, NEXUS_COL - 1   # 길 놓기 가능 열 1..7

# 인원별 (소환사, 스파이). 여분 1장은 비공개(구현상 생략, 비율만 반영)
ROLE_RATIO = {4: (3, 1), 5: (3, 2), 6: (4, 2), 7: (4, 3)}

HAND_SIZE = 5


def rot180(edges):
    """180도 회전: N<->S, E<->W"""
    return [edges[2], edges[3], edges[0], edges[1]]


def _build_deck():
    """길 카드 + 액션 카드 덱 생성 후 셔플."""
    # (edges[N,E,S,W], conn, count)
    shapes = [
        ([1, 1, 1, 1], 1, 4),   # 십자
        ([1, 1, 1, 0], 1, 3),   # T
        ([0, 1, 1, 1], 1, 3),
        ([1, 0, 1, 1], 1, 3),
        ([1, 1, 0, 1], 1, 3),
        ([1, 0, 1, 0], 1, 5),   # 직선(종)
        ([0, 1, 0, 1], 1, 5),   # 직선(횡)
        ([1, 1, 0, 0], 1, 4),   # 커브
        ([0, 1, 1, 0], 1, 4),
        ([0, 0, 1, 1], 1, 4),
        ([1, 0, 0, 1], 1, 4),
        ([1, 0, 1, 0], 0, 2),   # 막힌 길(관통X)
        ([0, 1, 0, 1], 0, 2),
        ([1, 1, 0, 0], 0, 1),
        ([0, 0, 1, 1], 0, 1),
    ]
    deck = []
    for edges, conn, cnt in shapes:
        for _ in range(cnt):
            deck.append({"type": "path", "edges": list(edges), "conn": conn})
    actions = [("stun", 6), ("heal", 4), ("gank", 4), ("ward", 4)]
    for a, cnt in actions:
        for _ in range(cnt):
            deck.append({"type": "action", "action": a})
    random.shuffle(deck)
    return deck


class Game:
    def __init__(self, players):
        """players: [{'id','name'}, ...] (4~7명)"""
        n = len(players)
        if n not in ROLE_RATIO:
            raise ValueError("4~7인만 지원합니다.")
        nm, ns = ROLE_RATIO[n]
        roles = ["소환사"] * nm + ["스파이"] * ns
        random.shuffle(roles)

        self.deck = _build_deck()
        self.board = {}   # (r,c) -> {'kind':'start'|'path'|'nexus', 'edges','conn','revealed','real'}
        self.board[START] = {"kind": "start", "edges": [1, 1, 1, 1], "conn": 1}
        real = random.randrange(3)
        for i, cell in enumerate(NEXUS_CELLS):
            self.board[cell] = {"kind": "nexus", "revealed": False, "real": (i == real)}

        self.players = []
        for p, role in zip(players, roles):
            self.players.append({
                "id": p["id"], "name": p["name"], "role": role,
                "hand": [self.deck.pop() for _ in range(HAND_SIZE)],
                "blocked": False,        # 스턴 여부
                "ward_seen": {},         # {nexus_index: bool real} 본인만
                "connected": True,
            })
        self.turn = 0
        self.phase = "진행"
        self.winner = None            # '소환사' | '스파이'
        self.log = ["게임 시작!"]

    # ---------- 좌표/연결 유틸 ----------
    def _in_bounds(self, r, c):
        return 0 <= r < ROWS and 0 <= c <= NEXUS_COL

    def _reachable(self):
        """본진에서 관통 통로를 따라 도달 가능한 길 타일 좌표 집합."""
        seen = set()
        stack = [START]
        seen.add(START)
        while stack:
            r, c = stack.pop()
            tile = self.board.get((r, c))
            if not tile or tile.get("conn", 0) != 1:
                # 관통 안 되는 타일(막힌 길)에는 도달은 하되 통과 불가
                if (r, c) != START:
                    continue
            for d, (dr, dc) in enumerate(DIRS):
                if tile["kind"] == "nexus":
                    continue
                if tile.get("edges", [0, 0, 0, 0])[d] != 1:
                    continue
                nr, nc = r + dr, c + dc
                nb = self.board.get((nr, nc))
                if not nb or nb["kind"] == "nexus":
                    continue
                if nb.get("edges", [0, 0, 0, 0])[OPP[d]] != 1:
                    continue
                if (nr, nc) not in seen:
                    seen.add((nr, nc))
                    stack.append((nr, nc))
        return seen

    def can_place(self, edges, conn, pos):
        """놓기 가능? (사유 문자열 반환, ''이면 가능)"""
        r, c = pos
        if not self._in_bounds(r, c) or not (MIN_COL <= c <= MAX_COL):
            return "보드 범위를 벗어났습니다."
        if (r, c) in self.board:
            return "이미 카드가 있습니다."
        placed_neighbors = 0
        reachable = self._reachable()
        connects = False
        for d, (dr, dc) in enumerate(DIRS):
            nr, nc = r + dr, c + dc
            nb = self.board.get((nr, nc))
            if not nb:
                continue
            if nb["kind"] in ("start", "path"):
                placed_neighbors += 1
                # 변 일치 규칙: 통로↔통로 / 벽↔벽
                if edges[d] != nb["edges"][OPP[d]]:
                    return "인접한 길과 통로가 맞지 않습니다."
                # 도달 가능한 이웃과 통로가 열려 연결되는가
                if edges[d] == 1 and nb["edges"][OPP[d]] == 1 and (nr, nc) in reachable:
                    connects = True
        if placed_neighbors == 0:
            return "기존 길에 이어서 놓아야 합니다."
        if not connects:
            return "본진에서 이어진 통로에 연결되지 않습니다."
        return ""

    def _check_nexus(self):
        """넥서스 연결 판정 → 공개 및 승리 처리."""
        reachable = self._reachable()
        for i, cell in enumerate(NEXUS_CELLS):
            nx = self.board[cell]
            if nx["revealed"]:
                continue
            nr, nc = cell
            for d, (dr, dc) in enumerate(DIRS):
                pr, pc = nr - dr, nc - dc  # 넥서스에 인접한 칸
                nb = self.board.get((pr, pc))
                if nb and nb["kind"] in ("start", "path") and (pr, pc) in reachable:
                    if nb["edges"][d] == 1:  # 그 칸이 넥서스 방향으로 열림
                        nx["revealed"] = True
                        self.log.append(f"넥서스 후보 {i+1} 공개 — {'진짜!' if nx['real'] else '가짜(억제기)'}")
                        if nx["real"]:
                            self.phase = "종료"
                            self.winner = "소환사"
                            self.log.append("소환사 승리! 진짜 넥서스 연결.")
                        break

    # ---------- 액션 ----------
    def cur(self):
        return self.players[self.turn]

    def _draw(self, pl):
        if self.deck:
            pl["hand"].append(self.deck.pop())

    def _advance(self):
        if self.phase != "진행":
            return
        # 덱 소진 & 전원 손패 없음 → 스파이 승
        if not self.deck and all(len(p["hand"]) == 0 for p in self.players):
            self.phase = "종료"
            self.winner = "스파이"
            self.log.append("덱 소진 — 스파이 승리! 넥서스를 지켜냈다.")
            return
        for _ in range(len(self.players)):
            self.turn = (self.turn + 1) % len(self.players)
            if self.phase == "진행":
                break

    def action(self, pid, kind, payload):
        """플레이어 행동. (ok, msg)"""
        if self.phase != "진행":
            return False, "게임이 끝났습니다."
        pl = self.cur()
        if pl["id"] != pid:
            return False, "당신의 턴이 아닙니다."

        if kind == "place":
            idx, pos, rot = payload["hand"], tuple(payload["pos"]), payload.get("rot", 0)
            if pl["blocked"]:
                return False, "스턴 상태 — 길을 놓을 수 없습니다."
            if not (0 <= idx < len(pl["hand"])):
                return False, "잘못된 카드."
            card = pl["hand"][idx]
            if card["type"] != "path":
                return False, "길 카드가 아닙니다."
            edges = rot180(card["edges"]) if rot else list(card["edges"])
            why = self.can_place(edges, card["conn"], pos)
            if why:
                return False, why
            self.board[pos] = {"kind": "path", "edges": edges, "conn": card["conn"]}
            pl["hand"].pop(idx)
            self.log.append(f"{pl['name']} 길 배치 {pos}")
            self._check_nexus()
            self._draw(pl)
            self._advance()
            return True, ""

        if kind == "pass":
            idx = payload.get("hand")
            if idx is not None and 0 <= idx < len(pl["hand"]):
                pl["hand"].pop(idx)   # 패스 시 카드 1장 버리기(선택)
            self.log.append(f"{pl['name']} 패스")
            self._draw(pl)
            self._advance()
            return True, ""

        if kind == "action":
            idx = payload["hand"]
            if not (0 <= idx < len(pl["hand"])):
                return False, "잘못된 카드."
            card = pl["hand"][idx]
            if card["type"] != "action":
                return False, "액션 카드가 아닙니다."
            a = card["action"]
            tgt = payload.get("target")

            if a in ("stun", "heal"):
                t = self._find(tgt)
                if not t:
                    return False, "대상을 찾을 수 없습니다."
                if a == "stun":
                    if t["blocked"]:
                        return False, "이미 스턴 상태입니다."
                    t["blocked"] = True
                    self.log.append(f"{pl['name']} → {t['name']} 스턴")
                else:
                    if not t["blocked"]:
                        return False, "정화할 CC가 없습니다."
                    t["blocked"] = False
                    self.log.append(f"{pl['name']} → {t['name']} 정화")

            elif a == "gank":
                pos = tuple(payload["pos"])
                cell = self.board.get(pos)
                if not cell or cell["kind"] != "path":
                    return False, "부술 길이 없습니다."
                del self.board[pos]
                self.log.append(f"{pl['name']} 갱킹 {pos}")

            elif a == "ward":
                ni = payload.get("nexus")
                if ni is None or not (0 <= ni < 3):
                    return False, "넥서스 후보를 지정하세요."
                pl["ward_seen"][ni] = self.board[NEXUS_CELLS[ni]]["real"]
                self.log.append(f"{pl['name']} 와드 정찰")

            else:
                return False, "알 수 없는 액션."

            pl["hand"].pop(idx)
            self._draw(pl)
            self._advance()
            return True, ""

        return False, "알 수 없는 행동."

    def _find(self, pid):
        for p in self.players:
            if p["id"] == pid:
                return p
        return None

    # ---------- 히든롤 필터 ----------
    def public_board(self):
        out = {}
        for (r, c), t in self.board.items():
            if t["kind"] == "nexus":
                out[f"{r},{c}"] = {"kind": "nexus", "revealed": t["revealed"],
                                   "real": (t["real"] if t["revealed"] else None)}
            else:
                out[f"{r},{c}"] = {"kind": t["kind"], "edges": t["edges"], "conn": t["conn"]}
        return out

    def view_for(self, pid):
        me = self._find(pid)
        return {
            "phase": self.phase,
            "winner": self.winner,
            "turn": self.turn,
            "turnName": self.players[self.turn]["name"] if self.players else "",
            "board": self.public_board(),
            "players": [{"id": p["id"], "name": p["name"], "blocked": p["blocked"],
                         "hand": len(p["hand"]), "connected": p["connected"]} for p in self.players],
            "log": self.log[-12:],
            # 개인 비공개
            "me": None if not me else {
                "id": me["id"], "role": me["role"], "blocked": me["blocked"],
                "hand": me["hand"], "wardSeen": me["ward_seen"],
                "myTurn": (self.players[self.turn]["id"] == pid and self.phase == "진행"),
            },
            "meta": {"rows": ROWS, "cols": NEXUS_COL + 1, "start": list(START),
                     "nexus": [list(c) for c in NEXUS_CELLS]},
        }


# ---------- 자체 테스트 ----------
if __name__ == "__main__":
    g = Game([{"id": str(i), "name": f"P{i}"} for i in range(4)])
    print("역할:", [(p["name"], p["role"]) for p in g.players])
    print("본진:", START, "넥서스:", NEXUS_CELLS)
    # 직선 횡 카드로 본진 오른쪽에 이어보기
    straight = {"type": "path", "edges": [0, 1, 0, 1], "conn": 1}
    print("(2,1) 직선 배치 가능?:", g.can_place(straight["edges"], 1, (2, 1)) or "가능")
    print("(2,2) 먼저 배치(불가여야):", g.can_place(straight["edges"], 1, (2, 2)) or "가능")
    # 실제 배치 시뮬
    b = g.board
    for c in range(1, 8):
        b[(2, c)] = {"kind": "path", "edges": [0, 1, 0, 1], "conn": 1}
    g._check_nexus()
    print("일직선 연결 후 phase:", g.phase, "winner:", g.winner)
    print("뷰(P0) me.role:", g.view_for("0")["me"]["role"], "/ 손패수:", len(g.view_for("0")["me"]["hand"]))
    print("뷰에 남의 역할 노출 안 됨:", all("role" not in pp for pp in g.view_for("0")["players"]))
