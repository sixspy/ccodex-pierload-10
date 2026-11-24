"""闭环重裁第10号三红（闭环案癸）。

红测甲：闭环后毛重未重裁 —— 点破挂起已闭、正式毛重仍超载的假绿。
红测乙：影子裁定未晋升 —— 点破影子/缺重裁被放行页当已裁。
争用测：双会话同挂起抢闭 —— 点破后到者只留争用痕、不写业务。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin", password: str = "admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _hold_of(c: TestClient, h: dict, ticket_code: str) -> dict:
    holds = c.get("/api/overloads?status=开放", headers=h).json()["items"]
    return next(x for x in holds if x["ticket_code"] == ticket_code)


def _rulings(c: TestClient, h: dict, ticket_id: int) -> list:
    detail = c.get(f"/api/tickets/{ticket_id}", headers=h).json()
    return detail.get("gross_rulings", [])


def _make_overloaded_ticket(c: TestClient, h: dict, code: str, gross: float = 50.0) -> dict:
    """经 API 造一张超重开放挂起票（LN-N-01-IN 阈值 49）。"""
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    lane = next(x for x in lanes if x["lane_code"] == "LN-N-01-IN")
    half = gross / 2
    ticket = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0103",
            "vehicle_plate": f"测挂·{code}",
            "axle_weights": [
                {"axle": 1, "w": half},
                {"axle": 2, "w": half},
            ],
            "ticket_code": code,
        },
    ).json()
    assert ticket["overload"] is True
    assert ticket["status"] == "旁路挂起"
    return ticket


# ---------------------------------------------------------------------------
# 红测甲：闭环须同事务重裁毛重并写新裁定，否则整笔回滚拒收
# ---------------------------------------------------------------------------

def test_a_close_must_re_adjudicate_gross_with_formal_ruling():
    c = TestClient(app)
    h = _auth(c)
    hold = _hold_of(c, h, "WT-OL-J60")
    ticket_id = hold["ticket_id"]

    # 案发态：正式毛重 58.5 仍超阈值 49
    detail = c.get(f"/api/tickets/{ticket_id}", headers=h).json()["ticket"]
    assert float(detail["gross_t"]) == 58.5

    close = c.post(
        "/api/overloads/close",
        headers=h,
        json={
            "hold_id": hold["id"],
            "note": "驳载后复磅 48.5t",
            "reweigh_gross_t": 48.5,
        },
    )
    assert close.status_code == 200, close.text
    body = close.json()
    assert body["ruling_scope"] == "正式"
    assert float(body["reweigh_gross_t"]) == 48.5

    # 正式毛重已重裁，票态回已裁定
    detail = c.get(f"/api/tickets/{ticket_id}", headers=h).json()["ticket"]
    assert float(detail["gross_t"]) == 48.5
    assert detail["status"] == "已裁定"

    # 新裁定落表，且只认正式口径
    rulings = _rulings(c, h, ticket_id)
    assert len(rulings) == 1
    assert rulings[0]["scope"] == "正式"
    assert float(rulings[0]["gross_t"]) == 48.5
    assert rulings[0]["source"] == "闭环重裁"
    assert all(r["scope"] != "影子" for r in rulings)

    # 挂起已关
    holds = c.get("/api/overloads", headers=h).json()["items"]
    closed = next(x for x in holds if x["id"] == hold["id"])
    assert closed["status"] == "已关闭"

    # 对账页同源重裁后的正式毛重（不再是超载值 58.5）
    recon = c.get("/api/reconcile/BL-2026-0103", headers=h).json()
    recon_ticket = next(t for t in recon["tickets"] if t["ticket_code"] == "WT-OL-J60")
    assert float(recon_ticket["gross_t"]) == 48.5

    # 放行页：正式裁定在案方可抬杆，且证明一致
    gate = c.post(
        "/api/gate/release",
        headers=h,
        json={"ticket_id": ticket_id, "officer": "闭环验收员"},
    )
    assert gate.status_code == 200, gate.text
    proof = c.get("/api/proof/ticket/WT-OL-J60", headers=h).json()
    assert proof["released"] is True
    assert proof["overload_closed"] is True
    assert proof["coherent"] is True


def test_a_rollback_when_reweigh_still_overloaded():
    """重裁后仍超重：整笔回滚拒收，挂起保持开放。"""
    c = TestClient(app)
    h = _auth(c)
    ticket = _make_overloaded_ticket(c, h, "WT-OL-J60-RB")
    hold = _hold_of(c, h, "WT-OL-J60-RB")

    close = c.post(
        "/api/overloads/close",
        headers=h,
        json={"hold_id": hold["id"], "note": "复磅仍超", "reweigh_gross_t": 51.0},
    )
    assert close.status_code == 409
    assert "回滚" in close.json()["detail"]

    # 整笔回滚：挂起未闭、票态未动、正式毛重零改、无裁定
    hold_row = c.get("/api/overloads", headers=h).json()["items"]
    hold_row = next(x for x in hold_row if x["id"] == hold["id"])
    assert hold_row["status"] == "开放"
    detail = c.get(f"/api/tickets/{ticket['id']}", headers=h).json()["ticket"]
    assert detail["status"] == "旁路挂起"
    assert float(detail["gross_t"]) == 50.0
    assert _rulings(c, h, ticket["id"]) == []

    # 未重裁通过不得放行
    gate = c.post(
        "/api/gate/release", headers=h, json={"ticket_id": ticket["id"]}
    )
    assert gate.status_code == 409


# ---------------------------------------------------------------------------
# 红测乙：影子裁定/缺重裁属偏航，拒写且不得被放行页当已裁
# ---------------------------------------------------------------------------

def test_b_shadow_ruling_is_yaw_and_never_promoted():
    c = TestClient(app)
    h = _auth(c)
    ticket = _make_overloaded_ticket(c, h, "WT-OL-J60-SH")
    hold = _hold_of(c, h, "WT-OL-J60-SH")

    # 影子口径当班禁用：记闭环重裁偏航并拒写
    shadow = c.post(
        "/api/overloads/close",
        headers=h,
        json={
            "hold_id": hold["id"],
            "note": "只落影子",
            "reweigh_gross_t": 48.0,
            "scope": "影子",
        },
    )
    assert shadow.status_code == 409
    assert "偏航" in shadow.json()["detail"]

    # 缺重裁毛重同样属偏航
    no_reweigh = c.post(
        "/api/overloads/close",
        headers=h,
        json={"hold_id": hold["id"], "note": "裸闭环"},
    )
    assert no_reweigh.status_code == 409
    assert "偏航" in no_reweigh.json()["detail"]

    audit = c.get("/api/audit", headers=h).json()["items"]
    yaw = [a for a in audit if a["action"] == "闭环重裁偏航"]
    assert len(yaw) >= 2

    # 挂起仍开放、正式毛重零改、无任何裁定行
    hold_row = next(
        x for x in c.get("/api/overloads", headers=h).json()["items"]
        if x["id"] == hold["id"]
    )
    assert hold_row["status"] == "开放"
    detail = c.get(f"/api/tickets/{ticket['id']}", headers=h).json()["ticket"]
    assert float(detail["gross_t"]) == 50.0
    assert detail["status"] == "旁路挂起"
    assert _rulings(c, h, ticket["id"]) == []

    # 放行页不得把未晋升影子当已裁
    gate = c.post(
        "/api/gate/release", headers=h, json={"ticket_id": ticket["id"]}
    )
    assert gate.status_code == 409


# ---------------------------------------------------------------------------
# 争用测：双人同挂起抢闭，后到只留争用痕；并发双请求只成一笔
# ---------------------------------------------------------------------------

def test_c_concurrent_close_contention_leaves_only_trace():
    c = TestClient(app)
    h1 = _auth(c, "admin", "admin123")
    h2 = _auth(c, "writer", "writer123")
    hold = _hold_of(c, h1, "WT-OL-J60")
    ticket_id = hold["ticket_id"]
    lock_key = f"ticket:{ticket_id}"

    # 甲会话闭环进行中持挂起锁，乙会话后到
    assert c.post(
        "/api/locks/acquire", headers=h1, json={"lock_key": lock_key}
    ).status_code == 200
    loser = c.post(
        "/api/overloads/close",
        headers=h2,
        json={"hold_id": hold["id"], "note": "乙抢闭", "reweigh_gross_t": 48.5},
    )
    assert loser.status_code == 409
    contention = c.get("/api/contention", headers=h1).json()["items"]
    assert any(x["lock_key"] == lock_key and x["actor"] == "writer" for x in contention)

    # 后到只留争用痕：不写业务
    hold_row = next(
        x for x in c.get("/api/overloads", headers=h1).json()["items"]
        if x["id"] == hold["id"]
    )
    assert hold_row["status"] == "开放"
    detail = c.get(f"/api/tickets/{ticket_id}", headers=h1).json()["ticket"]
    assert detail["status"] == "旁路挂起"
    assert _rulings(c, h1, ticket_id) == []

    # 甲会话完成闭环
    c.post("/api/locks/release", headers=h1, json={"lock_key": lock_key})
    winner = c.post(
        "/api/overloads/close",
        headers=h1,
        json={"hold_id": hold["id"], "note": "甲闭环", "reweigh_gross_t": 48.5},
    )
    assert winner.status_code == 200

    # 重复闭环须 409（而非 404），且不产生第二道裁定
    again = c.post(
        "/api/overloads/close",
        headers=h2,
        json={"hold_id": hold["id"], "note": "重复闭", "reweigh_gross_t": 48.5},
    )
    assert again.status_code == 409
    assert len(_rulings(c, h1, ticket_id)) == 1

    # 第10路：并发双请求打同一新挂起，恰成一笔
    cc = TestClient(app)
    hh = _auth(cc)
    ticket = _make_overloaded_ticket(cc, hh, "WT-OL-J60-CC")
    cc_hold = _hold_of(cc, hh, "WT-OL-J60-CC")
    barrier = threading.Barrier(2)
    results: list[dict] = []
    results_lock = threading.Lock()

    def worker(username: str, password: str) -> None:
        cli = TestClient(app)
        head = _auth(cli, username, password)
        barrier.wait()
        r = cli.post(
            "/api/overloads/close",
            headers=head,
            json={
                "hold_id": cc_hold["id"],
                "note": f"{username}并发闭",
                "reweigh_gross_t": 48.5,
            },
        )
        with results_lock:
            results.append({"status": r.status_code})

    t1 = threading.Thread(target=worker, args=("admin", "admin123"))
    t2 = threading.Thread(target=worker, args=("writer", "writer123"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r["status"] for r in results)
    assert statuses == [200, 409]
    cc_detail = cc.get(f"/api/tickets/{ticket['id']}", headers=hh).json()["ticket"]
    assert cc_detail["status"] == "已裁定"
    assert float(cc_detail["gross_t"]) == 48.5
    assert len(_rulings(cc, hh, ticket["id"])) == 1
    cc_hold_row = next(
        x for x in cc.get("/api/overloads", headers=hh).json()["items"]
        if x["id"] == cc_hold["id"]
    )
    assert cc_hold_row["status"] == "已关闭"
