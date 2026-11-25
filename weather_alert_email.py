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
import sys

NWS_URL = "https://api.weather.gov/alerts/active?status=actual&message_type=alert,update"
TIMEOUT_SECONDS = 30
MAX_ATTEMPTS = 4
RETRY_BASE_SECONDS = 2
MAX_PAGES = 20
EASTERN = ZoneInfo("America/New_York")
PAGES_BASE_URL = os.environ.get("PAGES_BASE_URL", "https://superspeedlogisticsllc.github.io/super-speed-weather-monitor").rstrip("/")

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
    """Concise report: regions, states, and major metro reference points only."""
    now = datetime.now(EASTERN)
    counts = Counter(a["tier"] for a in alerts)
    by_state = defaultdict(list)
    for alert in alerts:
        for state in alert["states"]:
            by_state[state].append(alert)
    top_states = Counter({s: len(v) for s, v in by_state.items()}).most_common(10)
    wind_states = sorted({s for a in alerts if any(a["event"].lower().startswith(x) for x in HIGH_WIND_EVENTS) for s in a["states"]})
    snow_states = sorted({s for a in alerts if any(x in a["event"].lower() for x in SNOW_ICE_EVENTS) for s in a["states"]})

    major_cities = {
        "AL":"Birmingham, Montgomery, Mobile", "AK":"Anchorage, Fairbanks, Juneau", "AZ":"Phoenix, Tucson, Flagstaff",
        "AR":"Little Rock, Fort Smith, Fayetteville", "CA":"Los Angeles, San Francisco, San Diego, Sacramento",
        "CO":"Denver, Colorado Springs, Fort Collins", "CT":"Hartford, New Haven, Bridgeport", "DE":"Wilmington, Dover",
        "DC":"Washington, DC", "FL":"Miami, Orlando, Tampa, Jacksonville", "GA":"Atlanta, Savannah, Augusta",
        "HI":"Honolulu, Hilo", "ID":"Boise, Idaho Falls", "IL":"Chicago, Springfield, Peoria",
        "IN":"Indianapolis, Fort Wayne, South Bend", "IA":"Des Moines, Cedar Rapids, Davenport", "KS":"Wichita, Topeka",
        "KY":"Louisville, Lexington, Bowling Green", "LA":"New Orleans, Baton Rouge, Shreveport", "ME":"Portland, Bangor",
        "MD":"Baltimore, Frederick, Salisbury", "MA":"Boston, Worcester, Springfield", "MI":"Detroit, Grand Rapids, Lansing",
        "MN":"Minneapolis–St. Paul, Duluth, Rochester", "MS":"Jackson, Gulfport, Hattiesburg", "MO":"St. Louis, Kansas City, Springfield",
        "MT":"Billings, Missoula, Great Falls", "NE":"Omaha, Lincoln, Grand Island", "NV":"Las Vegas, Reno, Elko",
        "NH":"Manchester, Concord, Portsmouth", "NJ":"Newark, Trenton, Atlantic City", "NM":"Albuquerque, Santa Fe, Las Cruces",
        "NY":"New York City, Buffalo, Rochester, Albany", "NC":"Charlotte, Raleigh, Greensboro, Wilmington", "ND":"Fargo, Bismarck",
        "OH":"Columbus, Cleveland, Cincinnati, Toledo", "OK":"Oklahoma City, Tulsa, Lawton", "OR":"Portland, Salem, Eugene, Bend",
        "PA":"Philadelphia, Pittsburgh, Harrisburg, Allentown", "RI":"Providence, Warwick, Newport", "SC":"Charleston, Columbia, Greenville",
        "SD":"Sioux Falls, Rapid City", "TN":"Nashville, Memphis, Knoxville, Chattanooga", "TX":"Houston, Dallas–Fort Worth, Austin, San Antonio",
        "UT":"Salt Lake City, St. George, Provo", "VT":"Burlington, Rutland, Montpelier", "VA":"Richmond, Virginia Beach, Roanoke, Northern Virginia",
        "WA":"Seattle, Spokane, Tacoma, Yakima", "WV":"Charleston, Morgantown, Huntington", "WI":"Milwaukee, Madison, Green Bay",
        "WY":"Cheyenne, Casper, Gillette"
    }
    colors = {1:"#b91c1c", 2:"#c2410c", 3:"#a16207"}
    labels = {1:"CRITICAL", 2:"HIGH", 3:"MODERATE"}
    region_blocks, region_summary = [], []
    for region, members in REGIONS.items():
        states = sorted(set(by_state) & members)
        if not states:
            continue
        rows = []
        for state in states:
            items = by_state[state]
            unique = {a["id"] or (a["event"], a["area"], a["effective"]): a for a in items}.values()
            tier_counts = Counter(a["tier"] for a in unique)
            hazards = sorted({a["event"] for a in items})
            badges = " ".join(
                f'<a href="{esc(PAGES_BASE_URL)}/#detail-{esc(state)}-tier-{t}" '
                f'style="display:inline-block;background:{colors[t]};color:white;padding:5px 8px;'
                f'border-radius:3px;margin:2px;text-decoration:none;font-weight:bold;">'
                f'{labels[t]} {tier_counts[t]} — click for details</a>'
                for t in (1,2,3) if tier_counts[t]
            )
            hazard_text = "; ".join(hazards[:4]) + (f"; +{len(hazards)-4} more" if len(hazards)>4 else "")
            rows.append(f'<div style="border:1px solid #d1d5db;border-left:5px solid #64748b;padding:10px 12px;margin:8px 0;"><b style="font-size:17px;"><a href="{esc(PAGES_BASE_URL)}/#detail-{esc(state)}" style="color:#1d4ed8;text-decoration:underline;">{esc(state)}</a></b> <span style="color:#4b5563;font-size:12px;">(<a href="{esc(PAGES_BASE_URL)}/#detail-{esc(state)}" style="color:#1d4ed8;">{len(items)} alert-state occurrences — click for dates &amp; times</a>)</span><div style="margin:5px 0;">{badges}</div><p style="margin:5px 0;"><b>Hazards:</b> {esc(hazard_text)}</p><p style="margin:5px 0;"><b>Major cities / metro areas to watch:</b> {esc(major_cities.get(state, "Not listed"))}</p></div>')
        region_blocks.append(f'<h2 style="background:#1f2937;color:white;padding:10px;font-size:17px;margin-top:20px;">{esc(region)}</h2>' + "".join(rows))
        region_summary.append(f"{region}: {len(states)} affected state(s)")

    top_html = "".join(f"<li><b>{esc(s)}</b>: {n} alert-state occurrences</li>" for s,n in top_states) or "<li>No qualifying active alerts.</li>"
    wind_html = ", ".join(wind_states) or "None identified"
    snow_html = ", ".join(snow_states) or "None identified"
    error_html = f'<div style="background:#fee2e2;padding:12px;color:#991b1b;"><b>LIVE DATA FETCH ERROR:</b> {esc(fetch_error)}. This is NOT an all-clear report.</div>' if fetch_error else ""
    quiet_html = '<p style="padding:12px;background:#ecfdf5;">Live NWS request succeeded; no qualifying active land-transport alerts were returned. This is not a guarantee of safe roads.</p>' if not alerts and not fetch_error else ""
    # Click-through detail section: exact NWS alert validity windows and covered areas.
    # City names are shown as regional reference points only; NWS alerts often cover
    # counties/zones, so the script never claims a city is affected without polygon testing.
    state_detail_blocks = []
    for state in sorted(by_state):
        unique_items = {}
        for item in by_state[state]:
            key = item["id"] or (item["event"], item["area"], item["effective"], item["expires"])
            unique_items[key] = item
        detail_rows_by_tier = {1: [], 2: [], 3: []}
        for item in sorted(unique_items.values(), key=lambda x: (x["effective"], x["event"])):
            link = f'<a href="{esc(item["url"])}" style="color:#1d4ed8;">Open official NWS alert</a>' if item["url"] else "Official NWS link unavailable"
            detail_rows_by_tier[item["tier"]].append(
                '<div style="border:1px solid #d1d5db;border-radius:5px;padding:11px;margin:9px 0;">'
                f'<b>{esc(item["event"])}</b> — <span style="color:{colors[item["tier"]]};font-weight:bold;">{labels[item["tier"]]}</span>'
                f'<p style="margin:6px 0;"><b>Begins / effective:</b> {esc(item["effective"])}</p>'
                f'<p style="margin:6px 0;"><b>Alert expires:</b> {esc(item["expires"])}</p>'
                f'<p style="margin:6px 0;"><b>NWS-covered area:</b> {esc(item["area"])}</p>'
                f'<p style="margin:6px 0;"><b>Dispatch impact:</b> {esc(item["impact"][:700])}</p>'
                f'<p style="margin:6px 0;">{link}</p></div>'
            )
        tier_sections = []
        for tier in (1, 2, 3):
            if detail_rows_by_tier[tier]:
                tier_sections.append(
                    f'<div id="detail-{esc(state)}-tier-{tier}" style="padding:10px 0;">'
                    f'<h4 style="margin:8px 0;color:{colors[tier]};">{labels[tier]} ALERTS ({len(detail_rows_by_tier[tier])})</h4>'
                    + "".join(detail_rows_by_tier[tier])
                    + '<p><a href="#top" style="color:#1d4ed8;">Back to report top</a> · '
                    + f'<a href="#detail-{esc(state)}" style="color:#1d4ed8;">Back to {esc(state)} overview</a></p></div>'
                )
        state_detail_blocks.append(
            f'<div id="detail-{esc(state)}" style="padding:12px 0;border-top:2px solid #cbd5e1;">'
            f'<h3 style="margin-bottom:5px;">{esc(state)} — Alert dates &amp; times</h3>'
            f'<p style="font-size:13px;color:#4b5563;">Major metro reference points: {esc(major_cities.get(state, "Not listed"))}</p>'
            + "".join(tier_sections)
            + '<p style="font-size:12px;color:#92400e;background:#fffbeb;padding:9px;">City timing note: the dates and times above apply to each NWS alert and its stated covered area. They do not independently confirm that every major city listed above is inside the alert boundary. Open the NWS notice for the official map and exact local coverage.</p>'
            + '<p><a href="#top" style="color:#1d4ed8;">Back to report top</a></p></div>'
        )
    fetched_text = fetched_at.astimezone(EASTERN).strftime("%A, %B %d, %Y at %I:%M:%S %p ET") if fetched_at else "Unavailable — fetch failed"
    common_header = f'''<div style="background:#0f172a;color:white;padding:18px;border-radius:6px;"><h1 style="margin:0;font-size:23px;">Super Speed Logistics — U.S. Weather Dispatch</h1><p>Generated {esc(now.strftime('%A, %B %d, %Y at %I:%M %p ET'))}</p><p>Live NWS retrieval: {esc(fetched_text)} | Automated 4-hour monitor</p></div>'''
    summary_content = f'''{error_html}<h2>National Alert Summary</h2><table role="presentation" style="width:100%;text-align:center;border-collapse:collapse;"><tr><td style="padding:12px;background:#fee2e2;">CRITICAL<br><b style="font-size:25px;">{counts[1]}</b></td><td style="padding:12px;background:#ffedd5;">HIGH<br><b style="font-size:25px;">{counts[2]}</b></td><td style="padding:12px;background:#fef3c7;">MODERATE<br><b style="font-size:25px;">{counts[3]}</b></td></tr></table>
    <p><b>Unique qualifying alerts:</b> {len(alerts)} | <b>States represented:</b> {len(by_state)} | <b>NWS records retrieved:</b> {raw_nws_count} | <b>API pages:</b> {page_count}</p>{quiet_html}
    <h2>Regional Overview</h2><ul>{''.join(f'<li>{esc(x)}</li>' for x in region_summary) or '<li>No affected regions.</li>'}</ul>
    <h2>Top 10 States to Watch</h2><ol>{top_html}</ol>
    <div style="background:#fee2e2;border-left:5px solid #b91c1c;padding:12px;margin:12px 0;"><b>Wind / Blow-Over Risk States:</b><br>{esc(wind_html)}</div>
    <div style="background:#e0f2fe;border-left:5px solid #0369a1;padding:12px;margin:12px 0;"><b>Snow / Ice Risk States:</b><br>{esc(snow_html)}</div>
    <h2>States and Major Cities by Region</h2>{''.join(region_blocks) or '<p>No state summaries to display.</p>'}'''
    footer = '<hr><p style="font-size:12px;color:#4b5563;">Live source: National Weather Service API (api.weather.gov), active alerts endpoint. All timestamps are Eastern Time. This is an operational screening aid, not a safety determination. Verify road conditions, closures, forecasts, and company stop-work procedures before dispatch. GitHub Actions scheduled runs may be delayed.</p>'
    shell_start = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="margin:0;padding:18px;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;"><div id="top" style="max-width:900px;margin:auto;background:white;padding:20px;border-radius:8px;">'
    email_html = shell_start + common_header + summary_content + f'<p style="padding:12px;background:#eff6ff;border:1px solid #bfdbfe;">For detailed alert dates, times, affected areas, and official NWS notices, use the clickable state and severity links above. Details open on a separate page.</p>' + footer + '</div></body></html>'
    detail_intro = '<h2>Detailed State Weather Alerts</h2><p>Choose the state and alert level from your email to jump directly to that section. Alert effective and expiration times apply to the NWS-covered area shown, not automatically to every city listed.</p>'
    detail_html = shell_start + common_header + detail_intro + (''.join(state_detail_blocks) or '<p>No state alert details are available.</p>') + footer + '</div></body></html>'
    subject = f"Weather Dispatch Overview: {counts[1]} Critical | {counts[2]} High | {counts[3]} Moderate — {now:%b %d %I:%M %p ET}"
    return subject, email_html, detail_html

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
    if len(sys.argv) > 1 and sys.argv[1] == "--send-pending":
        with open(".weather_email_payload.json", encoding="utf-8") as payload_file:
            payload = json.load(payload_file)
        send_email(payload["subject"], payload["html"])
        print("Weather report email sent after the detailed page was published.")
        return

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

    subject, body, detail_page = build_email(alerts, fetched_at, raw_nws_count, page_count, fetch_error)
    os.makedirs("site", exist_ok=True)
    with open(os.path.join("site", "index.html"), "w", encoding="utf-8") as page_file:
        page_file.write(detail_page)
    if os.environ.get("DEFER_EMAIL", "").strip() == "1":
        with open(".weather_email_payload.json", "w", encoding="utf-8") as payload_file:
            json.dump({"subject": subject, "html": body}, payload_file)
        print("Weather report and detail page generated; email deferred until after page publication.")
    else:
        send_email(subject, body)
    print(
        f"Weather report processing complete. NWS records retrieved: {raw_nws_count}; "
        f"qualifying unique alerts: {len(alerts)}; pages: {page_count}; fetch error: {bool(fetch_error)}"
    )
    if fetch_error:
        # Email contains a visible data-failure warning; fail the workflow so the
        # GitHub Actions history does not present an unsuccessful fetch as healthy.
        raise RuntimeError(fetch_error)


if __name__ == "__main__":
    main()
