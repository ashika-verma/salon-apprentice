#!/usr/bin/env python3
"""
Salon Apprentice scraper.

Morning run (9am ET): clears state, emails full daily summary.
Hourly runs (10am–9pm ET): emails only new listings since last check.
Same subject each day so Gmail threads everything together.
"""

import json
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
import resend
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

LISTINGS_URL = (
    "https://www.salonapprentice.com/free-salon-services/"
    "SalonApprentice_list.php?q=%28city~equals~New+York%29"
    "&orderby=dlast_login&pagecount=300"
)
BASE_URL = "https://www.salonapprentice.com/free-salon-services/"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

KEYWORDS = ["balayage", "nail", "nails", "manicure", "pedicure"]

ET = ZoneInfo("America/New_York")


# ── State ──────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_ids": [], "date": ""}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Date helpers ───────────────────────────────────────────────────────────

def is_target_date(service_date: date) -> bool:
    return service_date >= date.today()


def parse_date(text: str) -> date | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text.strip())
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return None


# ── Scraping ───────────────────────────────────────────────────────────────

def scrape_listings() -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; salon-apprentice-bot/1.0)"}
    resp = requests.get(LISTINGS_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for row in soup.find_all("tr"):
        title_td = row.find("td", {"data-field": "ad_title"})
        if not title_td:
            continue

        def cell(field):
            td = row.find("td", {"data-field": field})
            return td.get_text(strip=True) if td else ""

        title = title_td.get_text(strip=True)
        record_id = title_td.get("data-record-id", "")
        view_tag = row.find("a", id=f"viewLink{record_id}")
        if view_tag and view_tag.get("href"):
            link = (BASE_URL + view_tag["href"].replace("&amp;", "")).rstrip("&")
        else:
            link = ""

        service_type = cell("service_type")
        fee = cell("fee")
        salon = cell("salon")
        neighborhood = cell("neighborhood")
        service_date_raw = cell("service_date")

        combined = (title + " " + service_type).lower()
        if not any(kw in combined for kw in KEYWORDS):
            continue

        service_date = parse_date(service_date_raw)
        if service_date and not is_target_date(service_date):
            continue

        listing_id = link.split("editid1=")[-1] if "editid1=" in link else record_id

        results.append({
            "id": listing_id,
            "title": title,
            "link": link,
            "service_type": service_type,
            "fee": fee,
            "salon": salon,
            "neighborhood": neighborhood,
            "service_date": service_date_raw,
        })

    return results


# ── Email ──────────────────────────────────────────────────────────────────

def listing_rows_html(listings: list[dict]) -> str:
    rows = ""
    for l in listings:
        fee_badge = (
            '<span style="color:#888;font-size:12px;">(fee)</span>'
            if l["fee"].lower() == "yes"
            else '<span style="color:#5a9e5a;font-size:12px;font-weight:600;">FREE</span>'
        )
        rows += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:12px 8px;">
            <a href="{l['link']}" style="color:#7c4dff;font-weight:600;text-decoration:underline;">{l['title']}</a>
            <div style="color:#888;font-size:13px;margin-top:3px;">{l['service_type']} &nbsp;{fee_badge}</div>
          </td>
          <td style="padding:12px 8px;color:#555;">{l['neighborhood'] or l['salon'] or '—'}</td>
          <td style="padding:12px 8px;color:#555;white-space:nowrap;">{l['service_date']}</td>
          <td style="padding:12px 8px;">
            <a href="{l['link']}" style="display:inline-block;padding:5px 12px;background:#7c4dff;color:#fff;border-radius:4px;font-size:12px;text-decoration:none;white-space:nowrap;">View →</a>
          </td>
        </tr>"""
    return rows


def table_html(rows: str) -> str:
    return f"""
    <table style="width:100%;border-collapse:collapse;font-family:sans-serif;font-size:15px;">
      <thead>
        <tr style="background:#f5f0ff;color:#555;font-size:12px;text-transform:uppercase;letter-spacing:.05em;">
          <th style="padding:10px 8px;text-align:left;">Listing</th>
          <th style="padding:10px 8px;text-align:left;">Neighborhood</th>
          <th style="padding:10px 8px;text-align:left;">Date</th>
          <th style="padding:10px 8px;"></th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>"""


def build_morning_html(listings: list[dict]) -> str:
    today = date.today()
    if listings:
        body = table_html(listing_rows_html(listings))
    else:
        body = "<p style='color:#888;'>No matching listings found this morning. Check back later!</p>"

    return f"""
    <div style="font-family:sans-serif;max-width:640px;margin:auto;padding:24px;">
      <h2 style="color:#7c4dff;margin-bottom:4px;">Good morning ☀️ — your salon picks</h2>
      <p style="color:#888;font-size:13px;margin-top:0;">
        {today.strftime('%A, %B %-d %Y')} &nbsp;·&nbsp;
        Balayage &amp; nails available from today onwards
      </p>
      {body}
      <p style="margin-top:24px;font-size:12px;color:#bbb;">
        I'll update this thread hourly with any new listings.
        &nbsp;<a href="{LISTINGS_URL}" style="color:#bbb;">View all NYC listings →</a>
      </p>
    </div>"""


def build_update_html(new_listings: list[dict], now: datetime) -> str:
    time_str = now.strftime("%-I:%M %p")
    rows = listing_rows_html(new_listings)
    return f"""
    <div style="font-family:sans-serif;max-width:640px;margin:auto;padding:24px;">
      <p style="color:#888;font-size:13px;margin-top:0;margin-bottom:12px;">
        🆕 <strong>{len(new_listings)} new listing{'s' if len(new_listings) != 1 else ''}</strong>
        as of {time_str}
      </p>
      {table_html(rows)}
      <p style="margin-top:16px;font-size:12px;color:#bbb;">
        <a href="{LISTINGS_URL}" style="color:#bbb;">View all NYC listings →</a>
      </p>
    </div>"""


def day_subject(today: date) -> str:
    return f"💇 Salon picks — {today.strftime('%b %-d')}"


def send_email(subject: str, html: str):
    resend.api_key = os.environ["RESEND_API_KEY"]
    resend.Emails.send({
        "from": os.environ["FROM_EMAIL"],
        "to": [os.environ["TO_EMAIL"]],
        "subject": subject,
        "html": html,
    })


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(ET)
    today = now.date()
    is_morning = now.hour == 9

    state = load_state()
    today_str = today.isoformat()

    # Reset state each new day (morning run)
    if is_morning or state.get("date") != today_str:
        state = {"seen_ids": [], "date": today_str}

    print(f"Running at {now.strftime('%Y-%m-%d %H:%M %Z')} — {'morning summary' if is_morning else 'hourly update'}")

    all_listings = scrape_listings()
    print(f"Scraped {len(all_listings)} matching listings")

    seen = set(state["seen_ids"])

    if is_morning:
        html = build_morning_html(all_listings)
        subject = day_subject(today)
        send_email(subject, html)
        print(f"Morning email sent: {subject}")
        state["seen_ids"] = [l["id"] for l in all_listings]
    else:
        new_listings = [l for l in all_listings if l["id"] not in seen]
        if new_listings:
            html = build_update_html(new_listings, now)
            subject = day_subject(today)
            send_email(subject, html)
            print(f"Update email sent with {len(new_listings)} new listings")
            state["seen_ids"] = list(seen | {l["id"] for l in new_listings})
        else:
            print("No new listings — skipping email")

    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
