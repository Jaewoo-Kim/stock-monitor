"""텔레그램 알림 봇 (신호 변화 + 목표가 급변 알림).

CLAUDE.md 알림 스펙(docs/00 7장):
  - 섹터 시그널 발동 (일간): signal_events 중 오늘자 신규 이벤트
  - 목표가 10%+ 상향 (즉시): report_events 중 이번 실행에서 새로 적재된 리포트

환경변수:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — 없으면 warning 로그 후 skip (graceful).

실행:
    python src/notify/telegram.py
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from db import connect

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 10

EVENT_LABEL = {
    "timing_buy": "🟢 매수적기 전환",
    "timing_watch": "🔵 관찰 전환",
    "phase_turn": "🔄 국면 전환",
}

TARGET_REVISION_THRESHOLD = 0.10


def send_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 — 알림 전송 스킵")
        return False
    try:
        resp = requests.post(
            API_BASE.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.error("텔레그램 전송 실패: %s", exc, exc_info=True)
        return False


def _signal_event_lines(con, calc_date: str) -> list[str]:
    rows = con.execute(
        """
        SELECT e.event_type, i.level2_name, e.from_state, e.to_state, e.detail
        FROM signal_events e
        JOIN industries i ON e.level2_id = i.level2_id
        WHERE e.event_date = ?
        ORDER BY e.event_type
        """,
        (calc_date,),
    ).fetchall()
    lines = []
    for etype, name, frm, to, detail in rows:
        label = EVENT_LABEL.get(etype, etype)
        try:
            det = json.loads(detail) if detail else {}
        except Exception:
            det = {}
        extra = ""
        if "rs_3m" in det:
            extra = f" (RS {det['rs_3m']:+.1f}%, 폭 {det.get('breadth_pct', 0):.0f}%)"
        lines.append(f"{label}: <b>{html.escape(name)}</b>{extra}")
    return lines


def _target_revision_lines(con, calc_date: str) -> list[str]:
    rows = con.execute(
        """
        SELECT r.broker, r.title, r.target_price, r.prev_target, r.source_url,
               c.name AS company_name, i.level2_name
        FROM report_events r
        LEFT JOIN companies c ON r.ticker = c.ticker
        LEFT JOIN industries i ON r.level2_id = i.level2_id
        WHERE date(r.created_at) = ?
          AND r.prev_target IS NOT NULL AND r.prev_target > 0
          AND r.target_price IS NOT NULL
          AND (r.target_price - r.prev_target) / r.prev_target >= ?
        ORDER BY (r.target_price - r.prev_target) / r.prev_target DESC
        """,
        (calc_date, TARGET_REVISION_THRESHOLD),
    ).fetchall()
    lines = []
    for broker, title, tp, prev, url, cname, level2_name in rows:
        pct = (tp - prev) / prev * 100
        subject = cname or level2_name or html.escape(title)
        lines.append(
            f"🎯 <b>{html.escape(subject)}</b> ({html.escape(broker)}): "
            f"{prev:,.0f} → {tp:,.0f} (<b>+{pct:.1f}%</b>)\n"
            f"{url}"
        )
    return lines


def build_digest(con, calc_date: str | None = None) -> str | None:
    if calc_date is None:
        calc_date = date.today().isoformat()

    signal_lines = _signal_event_lines(con, calc_date)
    revision_lines = _target_revision_lines(con, calc_date)

    if not signal_lines and not revision_lines:
        return None

    parts = [f"📊 <b>{calc_date} 산업 모니터링</b>"]
    if signal_lines:
        parts.append("\n" + "\n".join(signal_lines))
    if revision_lines:
        parts.append("\n" + "\n\n".join(revision_lines))
    return "\n".join(parts)


def run(con=None, calc_date: str | None = None) -> bool:
    own = con is None
    if own:
        con = connect()
    try:
        digest = build_digest(con, calc_date)
        if digest is None:
            log.info("알림 대상 없음 — 전송 스킵")
            return True
        return send_message(digest)
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    run()
