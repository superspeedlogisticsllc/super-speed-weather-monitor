#!/usr/bin/env python3
"""Super Speed Logistics — live U.S. NWS cargo-van weather alert email monitor.

Uses the official National Weather Service active-alert API. Standard library only.
Required environment variables: SENDER_EMAIL, SENDER_APP_PASSWORD, RECIPIENT_EMAIL.
"""

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import smtplib
import ssl
import os
import re
import time
import html

NWS_URL = "https://api.weather.gov/alerts/active?status=actual&message_type=alert,update"
TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 4
RETRY_BASE_SECONDS = 2
MAX_PAGES = 20
EASTERN = ZoneInfo("America/New_York")

# SAME geocodes are numeric: 0 + two-digit state FIPS + three-digit county.
# UGC geocodes are alphabetic: state abbreviation + county/zone code.
FIPS_TO_STATE = {
    "01":"AL", "02":"AK", "04":"AZ", "05":"AR", "06":"CA", "08":"CO", "09":"CT",
    "10":"DE", "11":"DC", "12":"FL", "13":"GA", "15":"HI", "16":"ID", "17":"IL",
    "18":"IN", "19":"IA", "20":"KS", "21":"KY", "22":"LA", "23":"ME", "24":"MD",
    "25":"MA", "26":"MI", "27":"MN", "28":"MS", "29":"MO", "30":"MT", "31":"NE",
    "32":"NV", "33":"NH", "34":"NJ", "35":"NM", "36":"NY", "37":"NC", "38":"ND",
    "39":"OH", "40":"OK", "41":"OR", "42":"PA", "44":"RI", "45":"SC", "46":"SD",
    "47":"TN", "48":"TX", "49":"UT", "50":"VT", "51":"VA", "53":"WA", "54":"WV",
    "55":"WI", "56":"WY",
}
SUPPORTED_STATES = set(FIPS_TO_STATE.values())
STATE_NAMES = {
    "Alabama":"AL", "Alaska":"AK", "Arizona":"AZ", "Arkansas":"AR", "California":"CA",
    "Colorado":"CO", "Connecticut":"CT", "Delaware":"DE", "District of Columbia":"DC",
    "Florida":"FL", "Georgia":"GA", "Hawaii":"HI", "Idaho":"ID", "Illinois":"IL",
    "Indiana":"IN", "Iowa":"IA", "Kansas":"KS", "Kentucky":"KY", "Louisiana":"LA",
    "Maine":"ME", "Maryland":"MD", "Massachusetts":"MA", "Michigan":"MI", "Minnesota":"MN",
    "Mississippi":"MS", "Missouri":"MO", "Montana":"MT", "Nebraska":"NE", "Nevada":"NV",
    "New Hampshire":"NH", "New Jersey":"NJ", "New Mexico":"NM", "New York":"NY",
    "North Carolina":"NC", "North Dakota":"ND", "Ohio":"OH", "Oklahoma":"OK", "Oregon":"OR",
    "Pennsylvania":"PA", "Rhode Island":"RI", "South Carolina":"SC", "South Dakota":"SD",
    "Tennessee":"TN", "Texas":"TX", "Utah":"UT", "Vermont":"VT", "Virginia":"VA",
    "Washington":"WA", "West Virginia":"WV", "Wisconsin":"WI", "Wyoming":"WY",
}
REGIONS = {
    "Northeast / Mid-Atlantic": set("CT DE ME MD MA NH NJ NY PA RI VT VA WV DC".split()),
    "Southeast": set("AL FL GA KY LA MS NC SC TN".split()),
    "Midwest / Great Lakes": set("IL IN MI MN OH WI IA MO".split()),
    "Plains": set("AR KS ND NE OK SD TX".split()),
    "Mountain West": set("CO ID MT UT WY".split()),
    "Southwest": set("AZ NM NV".split()),
    "West Coast": set("CA OR WA".split()),
    "Alaska / Hawaii": {"AK", "HI"},
}

# These are event-name rules, not keyword scans of long narrative descriptions.
TIER1_EVENTS = (
    "tornado warning", "tornado emergency", "flash flood warning", "blizzard warning",
    "ice storm warning", "high wind warning", "extreme wind warning", "hurricane warning",
    "storm surge warning", "dust storm warning", "snow squall warning", "tsunami warning",
)
TIER2_EVENTS = (
    "severe thunderstorm warning", "winter storm warning", "flood warning", "tornado watch",
    "severe thunderstorm watch", "high wind watch", "wind advisory", "winter storm watch",
    "hurricane watch", "tropical storm warning", "tropical storm watch", "coastal flood warning",
    "lakeshore flood warning", "flood watch", "dense fog advisory", "freezing fog advisory",
    "freezing rain advisory", "blowing dust advisory", "dust advisory",
)

HIGH_WIND_EVENTS = (
    "high wind warning", "high wind watch", "wind advisory", "extreme wind warning",
    "severe thunderstorm warning", "severe thunderstorm watch", "tornado warning", "tornado watch",
    "dust storm warning", "blowing dust advisory", "strong wind advisory",
)
SNOW_ICE_EVENTS = (
    "blizzard", "ice storm", "winter storm", "winter weather", "snow squall", "snow advisory",
    "lake effect snow", "freezing rain", "freezing drizzle", "freezing fog", "sleet", "snow squall",
)
MARINE_ONLY_PREFIXES = ("PZ", "AN", "AM", "GM", "LM", "LO", "LH", "LS")
MARINE_WORDS = ("offshore waters", "coastal waters", "waters from", "waters out")


def nws_user_agent():
    contact = os.environ.get("SENDER_EMAIL", "weather-monitor-contact-not-set").strip()
    return f"SuperSpeedLogisticsWeatherMonitor (contact: {contact})"


def request_json(url):
    """Request one NWS JSON page with bounded retries."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("api.weather.gov", "www.weather.gov"):
        raise ValueError(f"Refusing unexpected NWS pagination URL: {url}")
    request = Request(url, headers={
        "User-Agent": nws_user_agent(),
        "Accept": "application/geo+json, application/json",
    })
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)
            if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                raise ValueError("NWS response did not contain a valid GeoJSON features list")
            return data
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(RETRY_BASE_SECONDS * (2 ** attempt))
    raise RuntimeError(f"NWS request failed after {MAX_ATTEMPTS} attempts: {last_error}")


def fetch_alerts():
    """Fetch all pages of currently active NWS alerts, not a saved or cached report."""
    url = NWS_URL
    features = []
    seen_urls = set()
    page_count = 0
    while url:
        if url in seen_urls:
            raise RuntimeError("NWS API returned a repeated pagination URL; stopping to avoid a loop")
        seen_urls.add(url)
        page_count += 1
        if page_count > MAX_PAGES:
            raise RuntimeError(f"NWS active-alert response exceeded the {MAX_PAGES}-page safety limit")
        data = request_json(url)
        features.extend(data["features"])
        pagination = data.get("pagination") or {}
        next_url = pagination.get("next")
        url = next_url if isinstance(next_url, str) and next_url.strip() else None
    return features, page_count


def get_state_codes(properties):
    """Resolve state/DC codes from UGC first, SAME/FIPS second, then area text."""
    codes = set()
    geocode = properties.get("geocode") or {}

    # UGC examples: PAZ067 (zone) and PAC071 (county); first two letters are state.
    for ugc in geocode.get("UGC", []) or []:
        value = str(ugc).strip().upper()
        if len(value) >= 5 and value[:2] in SUPPORTED_STATES and value[2].isalpha():
            codes.add(value[:2])

    # SAME is numeric 0SSCCC; SS is the two-digit state FIPS code, not a state abbreviation.
    for same in geocode.get("SAME", []) or []:
        value = re.sub(r"\D", "", str(same))
        match = re.fullmatch(r"0?(\d{2})\d{3}", value)
        if match and match.group(1) in FIPS_TO_STATE:
            codes.add(FIPS_TO_STATE[match.group(1)])

    if not codes:
        area = str(properties.get("areaDesc") or "")
        for name, code in STATE_NAMES.items():
            if re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", area, re.I):
                codes.add(code)
        # Abbreviations are used only as standalone tokens in the area description.
        for token in re.findall(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", area.upper()):
            if token in SUPPORTED_STATES:
                codes.add(token)
    return sorted(codes & SUPPORTED_STATES)


def is_land_transport_alert(properties):
    """Exclude alerts that are solely for marine zones, retaining coastal flood alerts."""
    event = str(properties.get("event") or "").lower()
    ugc = (properties.get("geocode") or {}).get("UGC", []) or []
    marine_codes = bool(ugc) and all(str(code).strip().upper().startswith(MARINE_ONLY_PREFIXES) for code in ugc)
    area = str(properties.get("areaDesc") or "").lower()
    marine_description = any(term in area for term in MARINE_WORDS)
    coastal_flood = any(term in event for term in ("coastal flood", "lakeshore flood"))
    return not ((marine_codes or marine_description) and not coastal_flood)


def classify_tier(event, severity="Unknown", urgency="Unknown"):
    """Classify from the official event name and NWS severity/urgency fields only."""
    name = re.sub(r"\s+", " ", str(event or "").strip().lower())
    if any(name == item or name.startswith(item + " ") for item in TIER1_EVENTS):
        return 1
    if any(name == item or name.startswith(item + " ") for item in TIER2_EVENTS):
        return 2
    # NWS CAP severity/urgency are structured fields; narrative wording is not used.
    sev = str(severity or "").lower()
    urg = str(urgency or "").lower()
    if sev == "extreme" and urg in ("immediate", "expected"):
        return 1
    if sev == "severe" or urg == "expected":
        return 2
    if any(word in name for word in ("warning", "watch", "advisory", "statement", "outlook")):
        return 3
    return None


def parse_nws_time(value):
    if not value:
        return "Not specified"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(EASTERN).strftime("%a %b %d, %Y %I:%M %p ET")
    except (TypeError, ValueError):
        return str(value)


def concise_impact(event, headline, description, instruction):
    """Show concise source text; do not use narrative text to classify alert tiers."""
    source = " ".join(part for part in (headline, description, instruction) if part)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", source)
    keywords = (
        "wind", "gust", "mph", "snow", "ice", "sleet", "freezing rain", "visibility",
        "fog", "smoke", "dust", "flood", "water", "road", "highway", "interstate",
        "bridge", "overpass", "travel", "driving", "closure", "accumulation", "black ice",
    )
    selected = []
    for sentence in sentences:
        clean = " ".join(sentence.split()).strip(" -•")
        if clean and any(word in clean.lower() for word in keywords) and clean not in selected:
            selected.append(clean)
        if len(selected) >= 4:
            break
    if selected:
        return " ".join(selected)
    if description:
        return " ".join(description.split())[:650]
    return f"{event}: verify local road and route conditions before dispatch."


def normalize_alert(feature):
    props = feature.get("properties") or {}
    event = str(props.get("event") or "Unspecified NWS alert")
    headline = str(props.get("headline") or event)
    description = str(props.get("description") or "")
    instruction = str(props.get("instruction") or "")
    severity = str(props.get("severity") or "Unknown")
    urgency = str(props.get("urgency") or "Unknown")
    tier = classify_tier(event, severity, urgency)
    if tier is None or not is_land_transport_alert(props):
        return None
    states = get_state_codes(props)
    if not states:
        return None
    alert_id = str(feature.get("id") or props.get("id") or "")
    alert_url = alert_id if alert_id.startswith("https://api.weather.gov/alerts/") else ""
    return {
        "id": alert_id,
        "url": alert_url,
        "states": states,
        "event": event,
        "tier": tier,
        "severity": severity,
        "urgency": urgency,
        "certainty": str(props.get("certainty") or "Unknown"),
        "area": str(props.get("areaDesc") or "Affected area not specified"),
        "effective": parse_nws_time(props.get("effective") or props.get("onset")),
        "expires": parse_nws_time(props.get("expires") or props.get("ends")),
        "headline": headline,
        "impact": concise_impact(event, headline, description, instruction),
        "sent": str(props.get("sent") or ""),
    }


def region_for(state):
    for region, states in REGIONS.items():
        if state in states:
            return region
    return "Other"


def esc(value):
    return html.escape(str(value), quote=True)


def build_email(alerts, fetched_at, raw_nws_count, page_count, fetch_error=None):
    now = datetime.now(EASTERN)
    counts = Counter(alert["tier"] for alert in alerts)
    state_counts = Counter(state for alert in alerts for state in alert["states"])
    top_states = state_counts.most_common(10)

    high_wind_states = sorted({
        state for alert in alerts
        if any(alert["event"].lower().startswith(term) for term in HIGH_WIND_EVENTS)
        for state in alert["states"]
    })
    snow_ice_states = sorted({
        state for alert in alerts
        if any(term in alert["event"].lower() for term in SNOW_ICE_EVENTS)
        for state in alert["states"]
    })

    grouped = defaultdict(list)
    for alert in alerts:
        for state in alert["states"]:
            grouped[(region_for(state), state)].append(alert)

    tier_colors = {1: "#b91c1c", 2: "#c2410c", 3: "#a16207"}
    tier_names = {
        1: "TIER 1 — CRITICAL / IMMEDIATE REVIEW",
        2: "TIER 2 — HIGH CAUTION",
        3: "TIER 3 — MODERATE / MONITOR",
    }

    def alert_card(alert):
        color = tier_colors[alert["tier"]]
        source_link = (f'<p style="margin:4px 0;"><b>NWS source:</b> <a href="{esc(alert["url"])}">Open official alert</a></p>'
                       if alert["url"] else "")
        return f"""
        <div style="border:1px solid #d1d5db;border-left:6px solid {color};padding:13px;margin:10px 0;border-radius:5px;background:#fff;">
          <div style="font-size:12px;font-weight:bold;color:{color};">{esc(tier_names[alert['tier']])}</div>
          <h3 style="margin:6px 0 8px;color:#111827;">{esc(alert['event'])}</h3>
          <p style="margin:4px 0;"><b>Severity / Urgency / Certainty:</b> {esc(alert['severity'])} / {esc(alert['urgency'])} / {esc(alert['certainty'])}</p>
          <p style="margin:4px 0;"><b>Affected counties / zones:</b> {esc(alert['area'])}</p>
          <p style="margin:4px 0;"><b>Effective:</b> {esc(alert['effective'])} &nbsp; <b>Expires:</b> {esc(alert['expires'])}</p>
          <p style="margin:7px 0;"><b>Headline:</b> {esc(alert['headline'])}</p>
          <p style="margin:7px 0;"><b>Cargo-van driving impact (NWS text):</b> {esc(alert['impact'])}</p>
          {source_link}
        </div>"""

    body_sections = []
    region_order = list(REGIONS.keys()) + ["Other"]
    for region in region_order:
        region_states = sorted(state for reg, state in grouped if reg == region)
        if not region_states:
            continue
        region_html = [f'<h2 style="background:#1f2937;color:#fff;padding:10px;font-size:17px;">{esc(region)}</h2>']
        for state in region_states:
            items = sorted(grouped[(region, state)], key=lambda a: (a["tier"], a["event"]))
            region_html.append(f'<h3 style="color:#1d4ed8;border-bottom:1px solid #cbd5e1;padding-bottom:5px;">{esc(state)} — {len(items)} alert(s)</h3>')
            region_html.extend(alert_card(item) for item in items)
        body_sections.append("\n".join(region_html))

    top_html = ("".join(f"<li><b>{esc(state)}</b>: {count} alert-state occurrence(s)</li>" for state, count in top_states)
                if top_states else "<li>No active qualifying alerts in the configured state list.</li>")
    wind_html = ", ".join(high_wind_states) if high_wind_states else "None identified from active alert event names."
    snow_html = ", ".join(snow_ice_states) if snow_ice_states else "None identified from active alert event names."
    error_html = (
        f'<div style="background:#fee2e2;border:1px solid #ef4444;padding:12px;color:#991b1b;"><b>LIVE DATA FETCH ERROR:</b> {esc(fetch_error)}<br>Alert data is unavailable for this run. This is NOT an all-clear report.</div>'
        if fetch_error else ""
    )
    quiet_html = (
        '<p style="padding:12px;background:#ecfdf5;border:1px solid #10b981;">The live NWS request succeeded, and no qualifying active land-transport alerts were returned for the configured states at this check. This is not a guarantee of safe roads.</p>'
        if not alerts and not fetch_error else ""
    )
    unique_count = len(alerts)
    fetched_text = fetched_at.astimezone(EASTERN).strftime("%A, %B %d, %Y at %I:%M:%S %p ET") if fetched_at else "Unavailable — fetch failed"
    html_body = f"""<!doctype html>
<html><body style="margin:0;padding:18px;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;">
<div style="max-width:950px;margin:auto;background:#fff;padding:20px;border-radius:8px;">
  <div style="background:#0f172a;color:#fff;padding:18px;border-radius:6px;">
    <h1 style="margin:0;font-size:23px;">Super Speed Logistics — U.S. Weather Dispatch Report</h1>
    <p style="margin:7px 0 0;">Email generated {esc(now.strftime('%A, %B %d, %Y at %I:%M %p ET'))}</p>
    <p style="margin:5px 0 0;">Live NWS data retrieval: {esc(fetched_text)} | Automated 4-hour monitor</p>
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
  <p><b>Unique qualifying active alerts:</b> {unique_count} &nbsp; | &nbsp; <b>States represented:</b> {len(state_counts)}</p>
  <p><b>Total NWS alert records retrieved:</b> {raw_nws_count} &nbsp; | &nbsp; <b>API pages retrieved:</b> {page_count}</p>
  <h3>Top 10 Most Impacted States</h3><ol>{top_html}</ol>
  <p style="font-size:12px;color:#4b5563;">State rankings count each unique alert once in every state it affects. A multi-state alert therefore appears in multiple state totals, but only once in the unique-alert total.</p>
  <div style="background:#fee2e2;border-left:6px solid #b91c1c;padding:12px;margin:12px 0;">
    <h3 style="margin:0 0 6px;color:#991b1b;">High-Wind / Blow-Over Hazard Alert States</h3>
    <b>{esc(wind_html)}</b><p style="margin:6px 0 0;">Listed only when the official NWS event name identifies a wind, tornado, severe-thunderstorm, or dust-storm hazard. Review forecast gusts, vehicle limits, bridges, exposed roads, and company stop-work policy before dispatch.</p>
  </div>
  <div style="background:#e0f2fe;border-left:6px solid #0369a1;padding:12px;margin:12px 0;">
    <h3 style="margin:0 0 6px;color:#075985;">Snow / Ice / Freezing-Hazard Alert States</h3>
    <b>{esc(snow_html)}</b><p style="margin:6px 0 0;">Listed from official NWS snow/ice event names. Check each alert's details for precipitation type and amounts; black ice is not assumed unless the NWS text says so.</p>
  </div>
  {quiet_html}
  <h2>Detailed Alerts by Region and State</h2>
  {''.join(body_sections) if body_sections else '<p>No alert details to display.</p>'}
  <hr style="border:0;border-top:1px solid #d1d5db;margin-top:25px;">
  <p style="font-size:12px;color:#4b5563;">Live source: National Weather Service API (api.weather.gov), active alerts endpoint. All timestamps displayed in U.S. Eastern Time. Classification is an operational screening aid, not a safety determination. Always verify live road conditions, closures, local forecasts, and carrier stop-work procedures. GitHub Actions scheduled runs may be delayed by GitHub; this monitor runs on the configured four-hour UTC schedule when the service executes it.</p>
</div></body></html>"""
    subject = f"Weather Dispatch: {counts[1]} Critical | {counts[2]} High | {counts[3]} Moderate — {now:%b %d %I:%M %p ET}"
    return subject, html_body


def send_email(subject, html_body):
    sender = os.environ.get("SENDER_EMAIL", "").strip()
    password = os.environ.get("SENDER_APP_PASSWORD", "").strip()
    recipient = os.environ.get("RECIPIENT_EMAIL", "").strip()
    missing = [name for name, value in (
        ("SENDER_EMAIL", sender), ("SENDER_APP_PASSWORD", password), ("RECIPIENT_EMAIL", recipient)
    ) if not value]
    if missing:
        raise RuntimeError("Missing required GitHub secret/environment variable(s): " + ", ".join(missing))

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.attach(MIMEText(
        "Your email client does not support HTML. Open the HTML version for the live NWS weather report.",
        "plain", "utf-8"
    ))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], message.as_string())


def main():
    fetch_error = None
    alerts = []
    raw_nws_count = 0
    page_count = 0
    fetched_at = None
    try:
        features, page_count = fetch_alerts()
        fetched_at = datetime.now(timezone.utc)
        raw_nws_count = len(features)
        unique = {}
        for feature in features:
            alert = normalize_alert(feature)
            if not alert:
                continue
            # NWS feature URLs are unique identifiers. Fallback key avoids collapsing
            # separate alerts that happen to share the same event and area.
            key = alert["id"] or json.dumps((alert["event"], alert["area"], alert["effective"], alert["expires"]))
            unique[key] = alert
        alerts = list(unique.values())
    except Exception as exc:
        fetch_error = str(exc)

    subject, body = build_email(alerts, fetched_at, raw_nws_count, page_count, fetch_error)
    send_email(subject, body)
    print(
        f"Weather report email sent. NWS records retrieved: {raw_nws_count}; "
        f"qualifying unique alerts: {len(alerts)}; pages: {page_count}; fetch error: {bool(fetch_error)}"
    )
    if fetch_error:
        # Email contains a visible data-failure warning; fail the workflow so the
        # GitHub Actions history does not present an unsuccessful fetch as healthy.
        raise RuntimeError(fetch_error)


if __name__ == "__main__":
    main()
