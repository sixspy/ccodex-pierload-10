"""超重挂起：旁路驳回与闭环同重裁。

闭环重裁纪要（当班锁死，甲路径）：
- 闭环须在挂起锁内、同一事务重裁毛重并写正式裁定；重裁仍超载则整笔回滚拒收。
- 影子裁定/缺重裁属偏航，记「闭环重裁偏航」审计并拒写，正式毛重零改。
- 正式与影子两口径不得混用：放行只认正式裁定，影子永不晋升。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import OverloadCloseIn, OverloadOpenIn
from validators import adjudicate_overload, validate_ticket_mutable

router = APIRouter(tags=["overload"])

FORMAL_SCOPE = "正式"
YAW_ACTION = "闭环重裁偏航"


def _write_deviation(user: str, hold_key: str, payload: dict) -> None:
    """偏航纪要独立事务落库，不随业务回滚而丢失。"""
    with connect() as audit_conn:
        write_audit(audit_conn, user, YAW_ACTION, hold_key, payload)
        audit_conn.commit()


@router.get("/api/overloads")
def list_overloads(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT h.*, t.ticket_code, t.vehicle_plate, t.gross_t,
                      (SELECT r.scope FROM gross_rulings r
                       WHERE r.hold_id=h.id ORDER BY r.id DESC LIMIT 1) ruling_scope
               FROM overload_holds h JOIN weigh_tickets t ON t.id=h.ticket_id"""
        if status:
            rows = conn.execute(
                q + " WHERE h.status=? ORDER BY h.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY h.id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/overloads/open")
def open_overload(body: OverloadOpenIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"ticket:{body.ticket_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            ticket = conn.execute(
                """SELECT t.*, l.overload_threshold_t FROM weigh_tickets t
                   JOIN weigh_lanes l ON l.id=t.lane_id WHERE t.id=?""",
                (body.ticket_id,),
            ).fetchone()
            if not ticket:
                raise HTTPException(404, "过磅票不存在")
            validate_ticket_mutable(dict(ticket))
            open_one = conn.execute(
                """SELECT id FROM overload_holds WHERE ticket_id=? AND status='开放'""",
                (body.ticket_id,),
            ).fetchone()
            if open_one:
                raise HTTPException(409, "已有开放挂起")
            threshold = body.threshold_t or float(ticket["overload_threshold_t"])
            is_over, overweight = adjudicate_overload(
                float(ticket["gross_t"]), threshold
            )
            if not is_over:
                raise HTTPException(422, "未超重，不必挂起")
            now = now_iso()
            hold_key = f"HOLD-{ticket['ticket_code']}-M"
            cur = conn.execute(
                """INSERT INTO overload_holds(ticket_id,hold_key,overweight_t,threshold_t,
                   status,opened_at,side_lane_note) VALUES (?,?,?,?,?,?,?)""",
                (
                    body.ticket_id,
                    hold_key,
                    overweight,
                    threshold,
                    "开放",
                    now,
                    body.side_lane_note or "人工挂起",
                ),
            )
            conn.execute(
                "UPDATE weigh_tickets SET status='旁路挂起' WHERE id=?",
                (body.ticket_id,),
            )
            write_audit(
                conn,
                user,
                "overload_open",
                hold_key,
                {"hold_id": cur.lastrowid, "overweight_t": overweight},
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "hold_key": hold_key,
                "overweight_t": overweight,
                "status": "开放",
            }
        finally:
            release(conn, lock_key, user)


@router.post("/api/overloads/close")
def close_overload(body: OverloadCloseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    # 乙路径（影子裁定/缺重裁）当班禁用：记偏航并拒写，正式毛重零改。
    if body.scope != FORMAL_SCOPE or body.reweigh_gross_t is None:
        with connect() as probe:
            hold_key = probe.execute(
                "SELECT hold_key FROM overload_holds WHERE id=?", (body.hold_id,)
            ).fetchone()
        target = hold_key["hold_key"] if hold_key else f"hold#{body.hold_id}"
        reason = "影子裁定未晋升" if body.scope != FORMAL_SCOPE else "闭环缺重裁毛重"
        _write_deviation(
            user,
            target,
            {
                "hold_id": body.hold_id,
                "reason": reason,
                "scope": body.scope,
                "reweigh_gross_t": body.reweigh_gross_t,
            },
        )
        raise HTTPException(409, f"闭环重裁偏航：{reason}，按甲路径同事务重裁否则拒写")

    reweigh_gross = round(float(body.reweigh_gross_t), 3)
    with connect() as conn:
        hold = conn.execute(
            "SELECT * FROM overload_holds WHERE id=?", (body.hold_id,)
        ).fetchone()
        if not hold:
            raise HTTPException(404, "挂起不存在")
        if hold["status"] != "开放":
            raise HTTPException(409, "挂起已关闭")
        lock_key = f"ticket:{hold['ticket_id']}"
        require_lock(conn, lock_key, user)
        try:
            # 持锁后再读一次，杜绝并发会话在锁前把挂起关掉
            hold = conn.execute(
                "SELECT * FROM overload_holds WHERE id=?", (body.hold_id,)
            ).fetchone()
            if hold["status"] != "开放":
                raise HTTPException(409, "挂起已关闭")
            ticket = conn.execute(
                """SELECT t.*, l.overload_threshold_t FROM weigh_tickets t
                   JOIN weigh_lanes l ON l.id=t.lane_id WHERE t.id=?""",
                (hold["ticket_id"],),
            ).fetchone()
            validate_ticket_mutable(dict(ticket))
            threshold = float(hold["threshold_t"])
            is_over, overweight = adjudicate_overload(reweigh_gross, threshold)
            if is_over:
                # 重裁仍超载：整笔回滚拒收，挂起保持开放、正式毛重零改
                conn.rollback()
                write_audit(
                    conn,
                    user,
                    "overload_close_rollback",
                    hold["hold_key"],
                    {
                        "hold_id": body.hold_id,
                        "reweigh_gross_t": reweigh_gross,
                        "threshold_t": threshold,
                        "overweight_t": overweight,
                    },
                )
                conn.commit()
                raise HTTPException(
                    409,
                    f"回滚拒收：重裁毛重{reweigh_gross}t仍超阈值{threshold}t，"
                    "挂起保持开放",
                )
            now = now_iso()
            ruling_no = f"GR-{hold['hold_key']}-R1"
            cur = conn.execute(
                """INSERT INTO gross_rulings(ticket_id,hold_id,gross_t,threshold_t,
                   overweight_t,scope,source,ruling_no,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    hold["ticket_id"],
                    hold["id"],
                    reweigh_gross,
                    threshold,
                    overweight,
                    FORMAL_SCOPE,
                    "闭环重裁",
                    ruling_no,
                    "已裁定",
                    now,
                ),
            )
            conn.execute(
                """UPDATE overload_holds SET status='已关闭', closed_at=?, side_lane_note=?
                   WHERE id=?""",
                (now, body.note, body.hold_id),
            )
            # 正式毛重同事务重裁（五页同源的唯一正式毛重）
            conn.execute(
                """UPDATE weigh_tickets SET gross_t=?, status='已裁定', adjudicated_at=?
                   WHERE id=?""",
                (reweigh_gross, now, hold["ticket_id"]),
            )
            open_left = conn.execute(
                """SELECT COUNT(*) c FROM overload_holds
                   WHERE ticket_id=? AND status='开放'""",
                (hold["ticket_id"],),
            ).fetchone()["c"]
            write_audit(
                conn,
                user,
                "overload_close_ruling",
                hold["hold_key"],
                {
                    "hold_id": body.hold_id,
                    "ruling_id": cur.lastrowid,
                    "ruling_no": ruling_no,
                    "scope": FORMAL_SCOPE,
                    "old_gross_t": ticket["gross_t"],
                    "reweigh_gross_t": reweigh_gross,
                    "note": body.note,
                },
            )
            conn.commit()
            return {
                "ok": True,
                "hold_id": body.hold_id,
                "ticket_adjudicated": not open_left,
                "ruling_scope": FORMAL_SCOPE,
                "ruling_no": ruling_no,
                "reweigh_gross_t": reweigh_gross,
            }
        finally:
            release(conn, lock_key, user)