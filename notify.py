"""
notify.py
---------
Alerts **email** when fires in a given area exceed a threshold (intensity or count).

Setup (one-time) — in the .env file next to run_all.py:
    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=587
    SMTP_SECURITY=starttls          # starttls | ssl | none
    SMTP_USER=you@gmail.com
    SMTP_PASSWORD=app_password      # For Gmail: App Password not the regular password
    ALERT_EMAIL_TO=you@gmail.com,team@example.com
    ALERT_EMAIL_FROM=FireLink <you@gmail.com>   # Optional (default SMTP_USER)

Usage:
    python notify.py --test                                     # test email
    python notify.py --bbox 25,22,35,32 --min-frp 30            # check + email if condition met
    python notify.py --bbox 25,22,35,32 --min-frp 30 --dry-run  # prints the message only
    python notify.py --bbox world --min-frp 80 --min-count 5

Exit codes: 0 sent, 2 nothing to report, 1 error — suitable for cron / Task Scheduler.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Optional

from config import _read_env_file

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_state.json")


class NotifyError(RuntimeError):
    """Error in email settings or sending."""


def _setting(name: str, explicit: Optional[str] = None) -> str:
    value = explicit or os.environ.get(name) or _read_env_file().get(name) or ""
    return str(value).strip()


# ------------------------------------------------------------------ Check
def summarize(bbox: str = "world", date: Optional[str] = None, days: int = 1,
              min_frp: float = 30.0, min_count: int = 1, pipeline=None) -> dict:
    """Fetches observations from the same pipeline and counts those exceeding the threshold."""
    if pipeline is None:
        from pipeline import run_pipeline as pipeline
    payload = pipeline(bbox, date, days)
    strong = []
    for f in payload.get("features", []):
        p = f.get("properties", {})
        frp = p.get("frp_mw")
        if frp is not None and float(frp) >= min_frp:
            lon, lat = f["geometry"]["coordinates"]
            strong.append({"lat": lat, "lon": lon, "frp": float(frp),
                           "status": p.get("status"), "brightness": p.get("brightness")})
    strong.sort(key=lambda x: x["frp"], reverse=True)
    return {
        "bbox": bbox, "total": len(payload.get("features", [])), "strong": len(strong),
        "max_frp": strong[0]["frp"] if strong else 0.0, "fires": strong[:5],
        "triggered": len(strong) >= max(1, min_count),
        "meta": payload.get("meta", {}), "min_frp": min_frp, "min_count": max(1, min_count),
    }


def enrich_fire(fire: dict, wind_lookup=None, area_lookup=None) -> dict:
    """Adds wind direction and nearest protected area for the strongest fire (optional — fails silently)."""
    try:
        from weather import drift_sentence, get_wind

        wind = (wind_lookup or get_wind)(fire["lat"], fire["lon"])
        if wind:
            fire["wind"] = wind
            fire["wind_text"] = drift_sentence(wind)
    except Exception:  # noqa: BLE001
        pass
    try:
        from protected_areas import lookup as area_lookup_fn

        area = (area_lookup or area_lookup_fn)(fire["lat"], fire["lon"])
        if area:
            fire["protected_area"] = area
    except Exception:  # noqa: BLE001
        pass
    return fire


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_message(result: dict, subject: Optional[str] = None) -> tuple:
    """(subject, text, html) — text and HTML versions so any mail client can open them."""
    meta = result.get("meta") or {}
    area = result["bbox"] if result["bbox"] != "world" else "whole world"
    day = meta.get("date") or "latest"
    if subject is None:
        subject = (f"FireLink: {result['strong']} fire detection(s) ≥ {result['min_frp']:g} MW — {area} — {day}"
                   if result["triggered"]
                   else f"FireLink check — no detections above {result['min_frp']:g} MW — {area} — {day}")

    lines = [f"{result['strong']} detection(s) at or above {result['min_frp']:g} MW in {area} on {day}.",
             f"Total detections in the search: {result['total']} · peak intensity {result['max_frp']:g} MW"]
    html_rows = []
    for fire in result["fires"]:
        extra = []
        if fire.get("wind_text"):
            extra.append(fire["wind_text"])
        if fire.get("protected_area"):
            pa = fire["protected_area"]
            extra.append(f"near {pa['name']} ({pa['designation']}, {pa['distance_km']} km)")
        suffix = (" — " + "; ".join(extra)) if extra else ""
        lines.append(f"• {fire['lat']:.3f}, {fire['lon']:.3f} — {fire['frp']:g} MW ({fire['status']}){suffix}")
        html_rows.append(
            f"<tr><td>{fire['lat']:.3f}, {fire['lon']:.3f}</td><td>{fire['frp']:g} MW</td>"
            f"<td>{_esc(fire['status'])}</td><td>{_esc('; '.join(extra) or '—')}</td></tr>")
    if meta.get("note"):
        lines.append(meta["note"])
    if meta.get("partial_day"):
        lines.append("Today is still being published, so counts are partial.")

    head = (f"{result['strong']} detection(s) ≥ {result['min_frp']:g} MW"
            if result["triggered"] else "No detections above the threshold")
    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1a1a1a">
<h2 style="margin:0 0 4px">🔥 FireLink alert</h2>
<p style="margin:0 0 12px;color:#555">{_esc(head)} · area <b>{_esc(area)}</b> · day <b>{_esc(day)}</b></p>
<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-size:13px">
<thead><tr style="background:#f2f2f2"><th align="left">Location</th><th align="left">Intensity</th>
<th align="left">Status</th><th align="left">Wind / protected area</th></tr></thead>
<tbody>{''.join(html_rows) or '<tr><td colspan="4">Nothing to flag.</td></tr>'}</tbody></table>
<p style="color:#555;font-size:12px">Total detections in the search: {result['total']} · peak {result['max_frp']:g} MW</p>
{('<p style="color:#a15c00;font-size:12px">' + _esc(meta['note']) + '</p>') if meta.get('note') else ''}
{('<p style="color:#a15c00;font-size:12px">Today is still being published, so counts are partial.</p>') if meta.get('partial_day') else ''}
<p style="color:#888;font-size:11px">Sent by FireLink (NASA FIRMS VIIRS/MODIS) · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>
</body></html>"""
    return subject, "\n".join(lines), html


# ------------------------------------------------------------------ Sending
def send_email(subject: str, text: str, html: Optional[str] = None,
               settings: Optional[dict] = None, smtp_factory=None) -> None:
    """Sends HTML+text email. `smtp_factory` replaces smtplib.SMTP in tests."""
    cfg = settings or {}
    host = cfg.get("host") or _setting("SMTP_HOST")
    port = int(cfg.get("port") or _setting("SMTP_PORT") or 587)
    security = (cfg.get("security") or _setting("SMTP_SECURITY") or "starttls").lower()
    user = cfg.get("user") or _setting("SMTP_USER")
    password = cfg.get("password") or _setting("SMTP_PASSWORD")
    to_raw = cfg.get("to") or _setting("ALERT_EMAIL_TO")
    sender = cfg.get("sender") or _setting("ALERT_EMAIL_FROM") or user

    if not host:
        raise NotifyError("SMTP_HOST is not set — put your mail server in .env (e.g. smtp.gmail.com).")
    if not to_raw:
        raise NotifyError("ALERT_EMAIL_TO is not set — add the address that should receive alerts in .env.")
    if not sender:
        raise NotifyError("ALERT_EMAIL_FROM (or SMTP_USER) is required as the sender address.")
    if security in ("starttls", "ssl") and (not user or not password):
        raise NotifyError("SMTP_USER and SMTP_PASSWORD are required for starttls/ssl.")

    recipients = [a.strip() for a in to_raw.replace(";", ",").split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="firelink.local")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    factory = smtp_factory or smtplib.SMTP
    try:
        if security == "ssl":
            server = (smtp_factory or smtplib.SMTP_SSL)(host, port, timeout=20,
                                                         context=ssl.create_default_context())
        else:
            server = factory(host, port, timeout=20)
        with server as s:
            if security == "starttls":
                s.starttls(context=ssl.create_default_context())
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise NotifyError("SMTP login failed — for Gmail use an App Password (2-Step Verification → App passwords). "
                          f"({e.smtp_code})") from e
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f"Could not send the email: {e}") from e


# ------------------------------------------------------------------ Prevent duplicates
def _load_state(path: str = STATE_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, path: str = STATE_FILE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _key(bbox: str, min_frp: float) -> str:
    return f"{bbox}|{min_frp:g}"


def already_alerted(bbox: str, min_frp: float, day: str, path: str = STATE_FILE) -> bool:
    """Each region + boundary + day is sent only once (so the cron doesn't keep bothering)."""
    return _load_state(path).get(_key(bbox, min_frp), {}).get("day") == day


def mark_alerted(bbox: str, min_frp: float, day: str, path: str = STATE_FILE) -> None:
    state = _load_state(path)
    state[_key(bbox, min_frp)] = {"day": day, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save_state(state, path)


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Email an alert when fires in an area pass a threshold.")
    ap.add_argument("--bbox", default="world", help="west,south,east,north (or 'world')")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: newest published day)")
    ap.add_argument("--days", type=int, default=1, help="search window in days (1-5)")
    ap.add_argument("--min-frp", type=float, default=30.0, help="intensity threshold in MW (default 30)")
    ap.add_argument("--min-count", type=int, default=1, help="how many strong detections trigger an alert")
    ap.add_argument("--test", action="store_true", help="send a test email and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    ap.add_argument("--force", action="store_true", help="send even if this area was already alerted today")
    ap.add_argument("--no-enrich", action="store_true", help="skip wind / protected-area lookups")
    args = ap.parse_args(argv)

    try:
        if args.test:
            subject = "FireLink test email"
            text = "FireLink is connected — your email alerts are working."
            html = ("<html><body style='font-family:Arial,sans-serif'><h2>🔥 FireLink is connected</h2>"
                    "<p>This is a test email — your alerts are working.</p></body></html>")
            if args.dry_run:
                print(subject + "\n\n" + text)
                return 0
            send_email(subject, text, html)
            print("Test email sent.")
            return 0

        result = summarize(args.bbox, args.date, max(1, min(5, args.days)), args.min_frp, args.min_count)
        if result["fires"] and not args.no_enrich:
            enrich_fire(result["fires"][0])
        subject, text, html = build_message(result)
        day = result["meta"].get("date") or datetime.now(timezone.utc).date().isoformat()

        if not result["triggered"]:
            print(text)
            return 2
        if not args.force and already_alerted(args.bbox, args.min_frp, day):
            print(f"Already emailed for {args.bbox} on {day} — skipping (use --force to resend).")
            return 2
        if args.dry_run:
            print(subject + "\n\n" + text)
            return 0

        send_email(subject, text, html)
        mark_alerted(args.bbox, args.min_frp, day)
        print(f"Alert emailed ({result['strong']} detections ≥ {args.min_frp:g} MW in {args.bbox}, {day}).")
        return 0
    except NotifyError as e:
        print(f"Email: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - network/key NASA/area wrong
        print(f"Failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
