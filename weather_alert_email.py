if fetch_error else ""
    )
    quiet_html = (
        '<p style="padding:12px;background:#ecfdf5;border:1px solid #10b981;">No qualifying active land-transport alerts were returned for the configured states at this check. This is a monitor result, not a guarantee of safe roads.</p>'
        if not alerts and not fetch_error else ""
    )
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
    <b>{esc(wind_html)}</b><p style="margin:6px 0 0;">High-roof cargo vans have increased crosswind exposure. Review actual gust forecasts, vehicle limits, bridges, exposed roads, and company stop-work policy before dispatch.</p>
  </div>
  <div style="background:#e0f2fe;border-left:6px solid #0369a1;padding:12px;margin:12px 0;">
    <h3 style="margin:0 0 6px;color:#075985;">Active Snow Squall / Ice / Black Ice States</h3>
    <b>{esc(snow_html)}</b><p style="margin:6px 0 0;">Expect rapidly changing traction and visibility where applicable. Check state 511 road conditions and local restrictions before moving.</p>
  </div>
  {quiet_html}
  <h2>Detailed Alerts by Region and State</h2>
  {''.join(body_sections) if body_sections else '<p>No alert details to display.</p>'}
  <hr style="border:0;border-top:1px solid #d1d5db;margin-top:25px;">
  <p style="font-size:12px;color:#4b5563;">Source: National Weather Service API (api.weather.gov). Times shown in U.S. Eastern Time. Multi-state alerts appear under each affected state, so state-level entries may exceed the unique alert total. Classification is an operational screening aid, not a safety determination. Always verify live road conditions, closures, local forecasts, and carrier stop-work procedures.</p>
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
        "Your email client does not support HTML. The weather report is available in the HTML version.",
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
    try:
        features = fetch_alerts()
        for feature in features:
            alert = normalize_alert(feature)
            if alert:
                alerts.append(alert)
        # Deduplicate by NWS feature ID, retaining each alert once before state grouping.
        unique = {}
        for alert in alerts:
            unique[alert["id"] or (
                alert["event"], alert["area"], alert["effective"], alert["expires"]
            )] = alert
        alerts = list(unique.values())
    except Exception as exc:
        fetch_error = str(exc)

    subject, body = build_email(alerts, fetch_error)
    send_email(subject, body)
    print(f"Weather report email sent. Qualifying alerts: {len(alerts)}. Fetch error: {bool(fetch_error)}")
    if fetch_error:
        # Email was sent, but fail the workflow visibly so GitHub records the data issue.
        raise RuntimeError(fetch_error)


if __name__ == "__main__":
    main()
