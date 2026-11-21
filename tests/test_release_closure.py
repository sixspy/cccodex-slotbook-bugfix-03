"""释放闭环：任务成功必须带动正式作废与还席（甲路径同事务，见 docs/释放任务闭环说明.md）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import init_db  # noqa: E402
import db as dbmod  # noqa: E402

FAKE_GREEN = json.loads(
    (ROOT / "fixtures" / "release_fake_green.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user="admin", password="123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_booking(c: TestClient, h):
    books = c.get("/api/bookings", headers=h).json()["items"]
    seed = [b for b in books if b["party_key"] == FAKE_GREEN["party_key"]]
    assert seed, "种子正式册缺失"
    return seed[0]


def _fill_slot(c: TestClient, h, slot_code: str, seats: int):
    """逐座占位并确认，返回 booking_id 列表。"""
    ids = []
    for i in range(seats):
        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": slot_code, "party_key": f"P-FILL-{i}", "seats": 1},
        )
        assert r.json()["disposition"] == "占位", r.text
        hid = r.json()["hold_id"]
        r2 = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
        assert r2.status_code == 200, r2.text
        ids.append(r2.json()["booking_id"])
    return ids


def test_release_must_close_loop():
    """释放未闭环失败测：任务成功后五处同态，实测必须偏离假绿夹具。"""
    with TestClient(app) as c:
        h = _login(c)
        seed = _seed_booking(c, h)
        slot_before = c.get(f"/api/slots/{FAKE_GREEN['slot_code']}", headers=h).json()

        r = c.post("/api/release", headers=h, json={"booking_id": seed["id"]})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        slot_after = c.get(f"/api/slots/{FAKE_GREEN['slot_code']}", headers=h).json()
        booking_after = [
            b
            for b in c.get("/api/bookings", headers=h).json()["items"]
            if b["id"] == seed["id"]
        ][0]
        jobs = c.get("/api/release/jobs", headers=h).json()["items"]
        assert len(jobs) == 1
        actual = {
            "job_status": jobs[0]["status"],
            "booking_status": booking_after["status"],
            "booked_delta": slot_after["booked"] - slot_before["booked"],
            "reconcile_ok": c.get("/api/reconcile", headers=h).json()["all_ok"],
        }
        assert actual != FAKE_GREEN["fake_green"], "仍停在假绿：任务成功但正式未作废"
        assert actual == FAKE_GREEN["closed_loop"]

        # 审计同态：release_ok 落痕且带还席数
        audits = c.get("/api/audit", headers=h).json()["items"]
        rel = [a for a in audits if a["action"] == "release_ok"]
        assert rel, "缺 release_ok 审计"
        payload = json.loads(rel[0]["payload"])
        assert payload["seats_returned"] == FAKE_GREEN["seats"]

        # 候补同态：释放不篡改候补队列
        waits = c.get("/api/waitlist", headers=h).json()["items"]
        assert waits and all(w["status"] == "候补中" for w in waits)


def test_double_release_contention():
    """双人同释争用失败测：后到只留争用痕，占用只还一次。"""
    slot_code = FAKE_GREEN["slot_code"]
    lock_key = f"slot:{slot_code}"
    with TestClient(app) as c:
        admin = _login(c, "admin")
        clerk = _login(c, "clerk")
        seed = _seed_booking(c, admin)
        bid = seed["id"]
        booked_before = c.get(f"/api/slots/{slot_code}", headers=admin).json()["booked"]

        # 甲持锁模拟在途释放
        r = c.post("/api/locks/acquire", headers=admin, json={"lock_key": lock_key})
        assert r.status_code == 200, r.text

        # 乙后到 → 409，只留争用痕，业务零写
        r2 = c.post("/api/release", headers=clerk, json={"booking_id": bid})
        assert r2.status_code == 409, r2.text
        contention = c.get("/api/contention", headers=admin).json()["items"]
        assert any(
            x["actor"] == "clerk" and x["lock_key"] == lock_key for x in contention
        ), "后到未留争用痕"
        assert c.get("/api/release/jobs", headers=admin).json()["items"] == []
        still = [
            b
            for b in c.get("/api/bookings", headers=admin).json()["items"]
            if b["id"] == bid
        ][0]
        assert still["status"] == "已确认"
        assert (
            c.get(f"/api/slots/{slot_code}", headers=admin).json()["booked"]
            == booked_before
        )

        # 甲放锁后完成释放 → 闭环
        c.post("/api/locks/release", headers=admin, json={"lock_key": lock_key})
        r3 = c.post("/api/release", headers=admin, json={"booking_id": bid})
        assert r3.status_code == 200, r3.text

        # 乙再试 → 409 重复释放；占用只还一次，任务表仅一条成功
        r4 = c.post("/api/release", headers=clerk, json={"booking_id": bid})
        assert r4.status_code == 409, r4.text
        assert (
            c.get(f"/api/slots/{slot_code}", headers=admin).json()["booked"]
            == booked_before - FAKE_GREEN["seats"]
        )
        jobs = c.get("/api/release/jobs", headers=admin).json()["items"]
        assert len([j for j in jobs if j["status"] == "成功"]) == 1


def test_release_failure_rolls_back_all(monkeypatch):
    """甲路径：还席中途失败 → 任务/正式/占用三处同滚，盘面零改。"""
    import release_svc

    def _boom(conn, slot_id, seats):
        raise RuntimeError("还席故障注入")

    with TestClient(app, raise_server_exceptions=False) as c:
        h = _login(c)
        seed = _seed_booking(c, h)
        slot_before = c.get(f"/api/slots/{FAKE_GREEN['slot_code']}", headers=h).json()

        monkeypatch.setattr(release_svc, "return_seats", _boom)
        r = c.post("/api/release", headers=h, json={"booking_id": seed["id"]})
        assert r.status_code == 500

        assert c.get("/api/release/jobs", headers=h).json()["items"] == []
        still = [
            b
            for b in c.get("/api/bookings", headers=h).json()["items"]
            if b["id"] == seed["id"]
        ][0]
        assert still["status"] == "已确认"
        slot_after = c.get(f"/api/slots/{FAKE_GREEN['slot_code']}", headers=h).json()
        assert slot_after["booked"] == slot_before["booked"]
        audits = c.get("/api/audit", headers=h).json()["items"]
        assert not any(a["action"] == "release_ok" for a in audits)


def test_release_frees_seats_for_waitlist():
    """还席后候补可晋升：还席前晋升 409，还席后 200（候补同态）。"""
    with TestClient(app) as c:
        h = _login(c)
        ids = _fill_slot(c, h, "S-EVE-03", 6)
        r = c.post(
            "/api/holds",
            headers=h,
            json={"slot_code": "S-EVE-03", "party_key": "P-OVER", "seats": 1},
        )
        assert r.json()["disposition"] == "候补"
        wid = [
            w
            for w in c.get("/api/waitlist", headers=h).json()["items"]
            if w["party_key"] == "P-OVER"
        ][0]["id"]

        # 满员：晋升被拒
        r1 = c.post("/api/waitlist/promote", headers=h, json={"wait_id": wid})
        assert r1.status_code == 409, r1.text

        # 释放一笔正式 → 还席
        r2 = c.post("/api/release", headers=h, json={"booking_id": ids[0]})
        assert r2.status_code == 200, r2.text

        # 候补晋升成功，且对账仍平
        r3 = c.post("/api/waitlist/promote", headers=h, json={"wait_id": wid})
        assert r3.status_code == 200, r3.text
        assert c.get("/api/reconcile", headers=h).json()["all_ok"] is True
