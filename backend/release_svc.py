"""正式册释放/重开：任务成功 = 正式作废 + 还席，甲路径同事务闭环。

方案钉死见 docs/释放任务闭环说明.md（2026-11-21，当日不得改口）：
任务成功、正式作废、还席三写同一 commit；任一步失败三处同滚，盘面零改。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, write_audit
from locks import release, try_acquire
from slots_svc import return_seats

router = APIRouter(tags=["release"])


class ReleaseIn(BaseModel):
    booking_id: int
    note: str = ""


@router.get("/api/release/jobs")
def list_jobs(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM release_jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows], "stub": False}


@router.post("/api/release")
def release_booking(body: ReleaseIn, authorization: AuthHeader = None):
    """甲路径：任务成功、正式作废、还席同事务；失败三处同滚。"""
    user = require_writer(authorization)
    with connect() as conn:
        b = conn.execute(
            "SELECT * FROM bookings WHERE id=?", (body.booking_id,)
        ).fetchone()
        if not b:
            raise HTTPException(404, "正式预订不存在")
        slot = conn.execute(
            "SELECT * FROM slots WHERE id=?", (b["slot_id"],)
        ).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"
        if not try_acquire(conn, lock_key, user):
            # 后到只留争用痕：争用与审计落盘，业务零写
            write_audit(
                conn,
                user,
                "release_contention",
                str(body.booking_id),
                {"lock_key": lock_key},
            )
            conn.commit()
            raise HTTPException(409, "争用：他人在途释放，后到只记争用痕")
        try:
            if b["status"] != "已确认":
                # 幂等守卫：只留拒绝痕，不重复还席
                write_audit(
                    conn,
                    user,
                    "release_reject",
                    str(body.booking_id),
                    {"reason": f"状态={b['status']}，禁止重复释放"},
                )
                conn.commit()
                raise HTTPException(409, "正式册已作废，禁止重复释放")
            now = datetime.now().isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO release_jobs(booking_id,status,note,created_at)
                   VALUES (?,?,?,?)""",
                (body.booking_id, "成功", body.note or "释放闭环", now),
            )
            conn.execute(
                "UPDATE bookings SET status='已作废' WHERE id=?",
                (body.booking_id,),
            )
            return_seats(conn, slot["id"], b["seats"])
            write_audit(
                conn,
                user,
                "release_ok",
                str(body.booking_id),
                {
                    "job_id": cur.lastrowid,
                    "slot": slot["code"],
                    "seats_returned": b["seats"],
                },
            )
            conn.commit()  # 任务/正式/占用/审计 同一 commit
        except Exception:
            conn.rollback()  # 甲路径：失败三处同滚，盘面零改
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
        return {
            "ok": True,
            "stub": False,
            "job_id": cur.lastrowid,
            "message": "释放闭环：任务成功、正式作废、还席完成",
        }
