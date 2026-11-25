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

