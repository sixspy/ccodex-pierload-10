"""过磅与闸口校验。"""
from __future__ import annotations

from fastapi import HTTPException


def validate_lane_open(lane: dict) -> None:
    if lane["status"] not in ("开放", "维护"):
        raise HTTPException(409, "车道已关闭")
    if lane["status"] == "维护":
        raise HTTPException(409, "车道维护中禁止过磅")


def validate_axle_count(axles: list, axle_limit: int) -> None:
    if len(axles) < 2:
        raise HTTPException(422, "至少两根轴重")
    if len(axles) > axle_limit:
        raise HTTPException(422, f"轴数超限（限 {axle_limit} 轴）")


def validate_axle_weights(axles: list) -> tuple[float, float, float]:
    weights = []
    for a in axles:
        w = float(a.get("w", a.w if hasattr(a, "w") else 0))
        if w <= 0:
            raise HTTPException(422, "轴重须为正")
        weights.append(w)
    gross = round(sum(weights), 3)
    peak_axle = max(weights)
    spread = peak_axle - min(weights)
    return gross, peak_axle, spread


def validate_gross_cap(gross: float, max_gross: float) -> None:
    if gross > max_gross * 1.15:
        raise HTTPException(422, "毛重远超地磅额定量程")
    if gross <= 0:
        raise HTTPException(422, "毛重无效")


def validate_bl_match(declared: float, gross: float, tolerance: float = 0.08) -> bool:
    if declared <= 0:
        return False
    return abs(gross - declared) / declared <= tolerance


def adjudicate_overload(gross: float, threshold: float) -> tuple[bool, float]:
    overweight = round(gross - threshold, 3)
    return overweight > 0, max(overweight, 0.0)


def validate_ticket_mutable(ticket: dict) -> None:
    if ticket["status"] in ("已封闸", "已放行", "作废"):
        raise HTTPException(409, "票态已锁定")


def validate_ticket_sealable(ticket: dict) -> None:
    if ticket["status"] in ("已封闸", "作废"):
        raise HTTPException(409, "票态已锁定")


def validate_no_open_overload(conn, ticket_id: int) -> None:
    row = conn.execute(
        """SELECT id FROM overload_holds
           WHERE ticket_id=? AND status='开放' LIMIT 1""",
        (ticket_id,),
    ).fetchone()
    if row:
        raise HTTPException(409, "存在开放超重挂起，不得放行")


def validate_formal_gross_ruling(conn, ticket: dict) -> None:
    """放行口径：凡走过超重挂起的票，须有与当前正式毛重同值的正式裁定。

    影子裁定不晋升、不采信；缺正式裁定或裁定毛重与正式毛重不符即拒放，
    防止「挂起已闭、毛重假绿」。
    """
    hold = conn.execute(
        "SELECT id FROM overload_holds WHERE ticket_id=? LIMIT 1",
        (ticket["id"],),
    ).fetchone()
    if not hold:
        return
    ruling = conn.execute(
        """SELECT gross_t FROM gross_rulings
           WHERE ticket_id=? AND scope='正式' AND status='已裁定'
           ORDER BY id DESC LIMIT 1""",
        (ticket["id"],),
    ).fetchone()
    if not ruling:
        raise HTTPException(409, "无正式毛重裁定，影子不晋升，不得放行")
    if abs(float(ruling["gross_t"]) - float(ticket["gross_t"])) > 1e-6:
        raise HTTPException(409, "正式裁定毛重与当前正式毛重不符，不得放行")


def format_manifest(ticket: dict, gross: float, generation: int) -> str:
    return (
        f"票号={ticket['ticket_code']};"
        f"提单={ticket['bl_ref']};"
        f"车牌={ticket['vehicle_plate']};"
        f"毛重={gross}t;"
        f"闸代={generation}"
    )