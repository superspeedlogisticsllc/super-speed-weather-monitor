#!/usr/bin/env python3
"""
Super Speed Logistics — U.S. NWS Weather Dispatch Alert Monitor

Uses the public National Weather Service API (no API key required).
Sends an HTML dispatch report through Gmail SMTP using GitHub Actions secrets.

Required environment variables:
  SENDER_EMAIL
  SENDER_APP_PASSWORD
  RECIPIENT_EMAIL
"""

import html
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo


# Replace the contact address below with a monitored business email address.
USER_AGENT = "SuperSpeedLogisticsWeatherMonitor/1.0 (contact: YOUR_EMAIL@example.com)"
NWS_URL = (
    "https://api.weather.gov/alerts/active"
    "?status=actual&message_type=alert,update"
)
EASTERN = ZoneInfo("America/New_York")

# State abbreviations are taken from NWS UGC/SAME identifiers and area descriptions.
STATE_NAMES = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
    "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada",
    "NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York",
    "NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma",
    "OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina",
    "SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont",
    "VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin",
    "WY":"Wyoming","DC":"District of Columbia",
}
NAME_TO_ABBR = {v.lower(): k for k, v in STATE_NAMES.items()}

REGIONS = {
    "Northeast / Mid-Atlantic": {"CT","DE","DC","ME","MD","MA","NH","NJ","NY","PA","RI","VT","VA","WV"},
    "Southeast": {"AL","FL","GA","KY","LA","MS","NC","SC","TN"},
    "Midwest / Great Lakes": {"IL","IN","MI","MN","OH","WI","IA","MO"},
    "Plains": {"AR","KS","NE","ND","OK","SD","TX"},
    "Mountain West": {"CO","ID","MT","NV","UT","WY","AZ","NM"},
    "West Coast": {"CA","OR","WA","AK","HI"},
}

# Event words are intentionally broad screening terms, not a guarantee that
# every weather hazard affecting a road will be captured.
CRITICAL_WORDS = (
    "tornado warning", "flash flood warning", "blizzard warning",
    "ice storm warning", "hurricane warning", "extreme wind warning",
    "storm surge warning", "tsunami warning", "snow squall warning",
)
HIGH_WORDS = (
    "severe thunderstorm warning", "winter storm warning",
    "high wind warning", "flood warning", "coastal flood warning",
    "hurricane watch", "tropical storm warning", "wind advisory",
    "winter weather advisory", "freezing rain", "freezing drizzle",
    "dense fog advisory", "dust storm warning", "fire warning",
    "red flag warning", "excessive heat warning",
)
WIND_WORDS = ("wind", "gust", "straight-line", "crosswind", "blow-over")
ICE_SNOW_WORDS = (
    "snow", "ice", "freezing", "sleet", "black ice", "snow squall",
    "blizzard", "winter storm",
)
RELEVANT_WORDS = (
    "tornado", "thunderstorm", "wind", "gust", "snow", "ice", "freezing",
    "sleet", "blizzard", "winter storm", "snow squall", "flood",
    "coastal flood", "hurricane", "tropical storm", "storm surge",
    "fog", "dust", "smoke", "ash", "heat", "fire", "red flag",
    "avalanche", "waterspout", "hail", "cold", "wind chill",
)


def esc(value):
    return html.escape(str(value or ""), quote=True)


def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(EASTERN)
    except (ValueError, TypeError):
        return None


def fmt_time(value):
    dt = parse_time(value)
    return dt.strftime("%b %d, %Y %I:%M %p ET") if dt else "Not specified"


def get_state_codes(props):
    found = set()
    # NWS UGC codes commonly begin with a two-letter state abbreviation.
    for field in ("geocode",):
        geocode = props.get(field) or {}
        for code in (geocode.get("UGC") or []) + (geocode.get("SAME") or []):
            if isinstance(code, str) and len(code) >= 2:
                prefix = code[:2].upper()
                if prefix in STATE_NAMES:
                    found.add(prefix)

    area = (props.get("areaDesc") or "").lower()
    # Match full state names in the area description.
    for name, abbr in NAME_TO_ABBR.items():
        if name in area:
            found.add(abbr)

    # Include state abbreviations appearing as standalone tokens.
    import re
    for match in re.findall(r"\b[A-Z]{2}\b", props.get("areaDesc") or ""):
        if match in STATE_NAMES:
            found.add(match)
    return sorted(found)


def classify(props):
    event = (props.get("event") or "").lower()
    text = " ".join([
        event,
        props.get("headline") or "",
        props.get("description") or "",
        props.get("instruction") or "",
    ]).lower()

    if not any(word in text for word in RELEVANT_WORDS):
        return None

    if any(word in text for word in CRITICAL_WORDS):
        return 1
    if any(word in text for word in HIGH_WORDS):
        return 2

    severity = (props.get("severity") or "").lower()
    urgency = (props.get("urgency") or "").lower()
    if severity == "extreme" or urgency == "immediate":
        return 1
    if severity == "severe" or urgency == "expected":
        return 2
    return 3


def fetch_alerts():
    request = urllib.request.Request(
        NWS_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/geo+json",
        },
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("features", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Unable to retrieve NWS active alerts after 3 attempts: {last_error}")


def normalize_alert(feature):
    props = feature.get("properties") or {}
    tier = classify(props)
    if tier is None:
        return None
    states = get_state_codes(props)
    if not states:
        return None
    return {
        "id": feature.get("id") or props.get("@id") or "",
        "event": props.get("event") or "Weather alert",
        "tier": tier,
        "severity": props.get("severity") or "Unknown",
        "urgency": props.get("urgency") or "Unknown",
        "certainty": props.get("certainty") or "Unknown",
        "states": states,
        "area": props.get("areaDesc") or "Area not specified",
        "headline": props.get("headline") or "",
        "description": props.get("description") or "",
        "instruction": props.get("instruction") or "",
        "effective": props.get("effective") or props.get("onset") or "",
        "expires": props.get("expires") or "",
        "wind_related": any(w in " ".join([
            props.get("event") or "", props.get("headline") or "",
            props.get("description") or "",
        ]).lower() for w in WIND_WORDS),
        "snow_ice_related": any(w in " ".join([
            props.get("event") or "", props.get("headline") or "",
            props.get("description") or "",
        ]).lower() for w in ICE_SNOW_WORDS),
    }


def truncate(value, limit=1800):
    value = str(value or "").strip()
    return value if len(value) <= limit else value[:limit].rsplit(" ", 1)[0] + "…"


def build_email(alerts, fetch_error=None):
    now = datetime.now(EASTERN)
    counts = Counter(a["tier"] for a in alerts)
    state_counts = Counter()
    by_region = defaultdict(lambda: defaultdict(list))

    for alert in alerts:
        for state in alert["states"]:
            state_counts[state] += 1
            region = next(
                (name for name, members in REGIONS.items() if state in members),
                "Other / Unassigned",
            )
            by_region[region][state].append(alert)

    top_states = state_counts.most_common(10)
    wind_states = sorted({
        STATE_NAMES[s] for a in alerts if a["wind_related"] for s in a["states"]
    })
    snow_states = sorted({
        STATE_NAMES[s] for a in alerts if a["snow_ice_related"] for s in a["states"]
    })

    error_html = ""
    if fetch_error:
        error_html = (
            '<div style="padding:12px;background:#fee2e2;border-left:5px solid #b91c1c;'
            'margin:14px 0;"><b>NWS DATA RETRIEVAL ERROR</b><br>'
            + esc(fetch_error)
            + '</div>'
        )

    quiet_html = ""
    if not alerts and not fetch_error:
        quiet_html = (
            '<p style="padding:12px;background:#ecfdf5;border:1px solid #10b981;">'
            'No qualifying active land-transport alerts were returned for the configured '
            'states at this check. This is a monitor result, not a guarantee of safe roads.'
            '</p>'
        )

    if top_states:
        top_html = "".join(
            f"<li><b>{esc(STATE_NAMES[state])}</b> — {number} qualifying alert/state entries</li>"
            for state, number in top_states
        )
    else:
        top_html = "<li>No qualifying state entries</li>"

    wind_html = ", ".join(wind_states) if wind_states else "None identified in qualifying alerts"
    snow_html = ", ".join(snow_states) if snow_states else "None identified in qualifying alerts"

    body_sections = []
    for region in REGIONS:
        if region not in by_region:
            continue
        region_html = [f"<h2>{esc(region)}</h2>"]
        for state in sorted(by_region[region], key=lambda s: STATE_NAMES[s]):
            region_html.append(f"<h3>{esc(STATE_NAMES[state])}</h3>")
            for alert in sorted(
                by_region[region][state],
                key=lambda a: (a["tier"], a["event"]),
            ):
                tier_color = {1: "#b91c1c", 2: "#c2410c", 3: "#a16207"}[alert["tier"]]
                region_html.append(
                    '<div style="border:1px solid #d1d5db;border-left:6px solid '
                    f'{tier_color};padding:13px;margin:10px 0;border-radius:5px;">'
                    f'<div style="font-size:12px;color:{tier_color};font-weight:bold;">'
                    f'TIER {alert["tier"]} — {("CRITICAL" if alert["tier"] == 1 else "HIGH" if alert["tier"] == 2 else "MODERATE")}'
                    '</div>'
                    f'<h4 style="margin:5px 0;">{esc(alert["event"])}</h4>'
                    f'<p><b>Severity:</b> {esc(alert["severity"])} &nbsp; '
                    f'<b>Urgency:</b> {esc(alert["urgency"])} &nbsp; '
                    f'<b>Certainty:</b> {esc(alert["certainty"])}</p>'
                    f'<p><b>Affected area:</b> {esc(alert["area"])}</p>'
                    f'<p><b>Effective:</b> {esc(fmt_time(alert["effective"]))}<br>'
                    f'<b>Expires:</b> {esc(fmt_time(alert["expires"]))}</p>'
                    + (f'<p><b>Headline:</b> {esc(alert["headline"])}</p>' if alert["headline"] else "")
                    + (f'<p><b>Details:</b><br>{esc(truncate(alert["description"])).replace(chr(10), "<br>")}</p>' if alert["description"] else "")
                    + (f'<p><b>Instructions:</b><br>{esc(truncate(alert["instruction"], 1000)).replace(chr(10), "<br>")}</p>' if alert["instruction"] else "")
                    + '</div>'
                )
        body_sections.append("".join(region_html))

    report_count = len(alerts)
    html_body = f"""<!doctype html>
<html><body style="margin:0;padding:18px;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;">
<div style="max-width:950px;margin:auto;background:#fff;padding:20px;border-radius:8px;">
  <div style="background:#0f172a;color:#fff;padding:18px;border-radius:6px;">
    <h1 style="margin:0;font-size:23px;">Super Speed Logistics — U.S. Weather Dispatch Report</h1>
    <p style="margin:7px 0 0;">Generated {esc(now.strftime('%A, %B %d, %Y at %I:%M %p ET'))} | NWS active alerts | 4-hour monitor</p>
  </div>
  {error_html}
  <h2>Executive Dispatch Summary</h2>
  <table role="presentation" style="width:100%;border-collapse:collapse;text-align:center;">
    <tr>
      <td style="padding:13px;background:#fee2e2;border:1px solid #fff;"><div style="font-size:12px;">TIER 1 — CRITICAL</div><b style="font-size:26px;">{counts[1]}</b></td>
      <td style="padding:13px;background:#ffedd5;border:1px solid #fff;"><div style="font-size:12px;">TIER 2 — HIGH</div><b style="font-size:26px;">{counts[2]}</b></td>
      <td style="padding:13px;background:#fef3c7;border:1px solid #fff;"><div style="font-size:12px;">TIER 3 — MODERATE</div><b style="font-size:26px;">{counts[3]}</b></td>
    </tr>
  </table>
  <p><b>Total qualifying alerts:</b> {report_count} &nbsp; | &nbsp; <b>States represented:</b> {len(state_counts)}</p>
  <h3>Top 10 Most Impacted States</h3><ol>{top_html}</ol>
  <div style="background:#fee2e2;border-left:6px solid #b91c1c;padding:12px;margin:12px 0;">
    <h3 style="margin:0 0 6px;color:#991b1b;">High-Wind &amp; Blow-Over Risk States</h3>
    <b>{esc(wind_html)}</b>
    <p style="margin:6px 0 0;">High-roof cargo vans have increased crosswind exposure. Review actual gust forecasts, vehicle limits, bridges, exposed roads, and company stop-work policy before dispatch.</p>
  </div>
  <div style="background:#e0f2fe;border-left:6px solid #0369a1;padding:12px;margin:12px 0;">
    <h3 style="margin:0 0 6px;color:#075985;">Active Snow / Ice / Black-Ice-Related Alert States</h3>
    <b>{esc(snow_html)}</b>
    <p style="margin:6px 0 0;">Expect rapidly changing traction and visibility where applicable. Check state 511 road conditions and local restrictions before moving.</p>
  </div>
  {quiet_html}
  <h2>Detailed Alerts by Region and State</h2>
  {''.join(body_sections) if body_sections else '<p>No alert details to display.</p>'}
  <hr style="border:0;border-top:1px solid #d1d5db;margin-top:25px;">
  <p style="font-size:12px;color:#4b5563;">Source: National Weather Service API (api.weather.gov). Times shown in U.S. Eastern Time. Multi-state alerts appear under each affected state, so state-level entries may exceed the unique alert total. Classification is an operational screening aid, not a safety determination. Always verify live road conditions, closures, local forecasts, and carrier stop-work procedures.</p>
</div></body></html>"""

    subject = (
        f"Weather Dispatch: {counts[1]} Critical | {counts[2]} High | "
        f"{counts[3]} Moderate — {now:%b %d %I:%M %p ET}"
    )
    if fetch_error:
        subject = "DATA ERROR — " + subject
    return subject, html_body


def send_email(subject, html_body):
    sender = os.environ.get("SENDER_EMAIL", "").strip()
    password = os.environ.get("SENDER_APP_PASSWORD", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip()

    missing = [
        name for name, value in (
            ("SENDER_EMAIL", sender),
            ("SENDER_APP_PASSWORD", password),
            ("RECIPIENT_EMAIL", recipient),
        ) if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required GitHub secret/environment variable(s): "
            + ", ".join(missing)
        )

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.attach(MIMEText(
        "Your email client does not support HTML. The weather report is available in the HTML version.",
        "plain", "utf-8"
    ))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        "smtp.gmail.com", 465, context=context, timeout=30
    ) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], message.as_string())


def main():
    fetch_error = None
    alerts = []
    try:
        features = fetch_alerts()
        unique = {}
        for feature in features:
            alert = normalize_alert(feature)
            if alert:
                key = alert["id"] or (
                    alert["event"], alert["area"], alert["effective"], alert["expires"]
                )
                unique[key] = alert
        alerts = list(unique.values())
    except Exception as exc:
        fetch_error = str(exc)

    subject, body = build_email(alerts, fetch_error)
    send_email(subject, body)
    print(
        f"Weather report email sent. Qualifying alerts: {len(alerts)}. "
        f"Fetch error: {bool(fetch_error)}"
    )

    # The email is sent first so the data problem is visible in the report.
    # Then fail the workflow to make the retrieval issue visible in GitHub Actions.
    if fetch_error:
        raise RuntimeError(fetch_error)


if __name__ == "__main__":
    main()
