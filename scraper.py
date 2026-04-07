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
from datetime import date, datetime, time
from typing import Optional
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
HAIRCUT_KEYWORDS = ["haircut", "hair cut"]

MANHATTAN_NEIGHBORHOODS = {
    "manhattan", "upper east side", "upper west side", "midtown", "downtown",
    "chelsea", "tribeca", "soho", "noho", "nolita", "lower east side",
    "east village", "west village", "greenwich village", "hell's kitchen",
    "harlem", "washington heights", "inwood", "murray hill", "gramercy",
    "flatiron", "financial district", "battery park", "kips bay",
    "sutton place", "yorkville", "morningside heights", "hamilton heights",
    "midtown east", "midtown west", "times square", "theater district",
    "carnegie hill", "lenox hill", "hudson yards", "nomad", "two bridges",
    "chinatown", "little italy", "bowery", "alphabet city", "ues", "uws",
}

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


def parse_date(text: str) -> Optional[date]:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text.strip())
    if m:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    return None


# ── Availability filter ────────────────────────────────────────────────────

TIME_PATTERNS = [
    # "2:30 PM", "2:30PM", "2:30 pm"
    r"\b(\d{1,2}):(\d{2})\s*(am|pm)\b",
    # "2 PM", "2pm"
    r"\b(\d{1,2})\s*(am|pm)\b",
    # 24h: "14:30"
    r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
]


def parse_time_from_text(text: str) -> Optional[time]:
    """Extract the earliest time mention from a block of text."""
    text_lower = text.lower()
    for pattern in TIME_PATTERNS[:2]:  # 12h formats first
        for m in re.finditer(pattern, text_lower):
            groups = m.groups()
            hour = int(groups[0])
            minute = int(groups[1]) if len(groups) == 3 else 0
            meridiem = groups[-1]
            if meridiem == "pm" and hour != 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23:
                return time(hour, minute)
    # Try 24h
    for m in re.finditer(TIME_PATTERNS[2], text_lower):
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23:
            return time(hour, minute)
    return None


def fetch_listing_time(link: str, session: requests.Session) -> Optional[time]:
    """Fetch a listing detail page and extract the service time."""
    if not link:
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; salon-apprentice-bot/1.0)"}
        resp = session.get(link, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Try description / notes fields first, then full page text
        for selector in [
            {"data-field": "notes"},
            {"data-field": "description"},
            {"data-field": "service_time"},
            {"class": "listing-description"},
            {"class": "listing-notes"},
        ]:
            tag = soup.find(attrs=selector)
            if tag:
                t = parse_time_from_text(tag.get_text(" ", strip=True))
                if t:
                    return t
        # Fallback: scan all visible text
        return parse_time_from_text(soup.get_text(" ", strip=True))
    except Exception:
        return None


def is_manhattan(neighborhood: str, salon: str) -> bool:
    text = (neighborhood + " " + salon).lower()
    return any(nb in text for nb in MANHATTAN_NEIGHBORHOODS)


def is_womens_medium_haircut(combined: str) -> bool:
    """Return False only if the listing explicitly targets men or a non-medium length."""
    male_terms = ["men's", "mens ", "for men", "male only", "boy's haircut", "boys haircut"]
    female_terms = ["women", "woman", "female", "ladies", "lady", "girl"]
    if any(t in combined for t in male_terms) and not any(t in combined for t in female_terms):
        return False
    # Exclude explicitly short or long if length is mentioned but not medium
    non_medium = ["short haircut", "short hair cut", "short cut", "long haircut", "long hair cut"]
    medium = ["medium", "med length", "mid length"]
    if any(t in combined for t in non_medium) and not any(t in combined for t in medium):
        return False
    return True


def is_available_slot(service_date: Optional[date], service_time: Optional[time]) -> bool:
    """Return True if the slot fits the user's schedule.

    Rules:
    - Weekends (Sat/Sun): always available.
    - Weekdays (Mon–Fri): only available at 5pm or later.
    - If we couldn't parse a time on a weekday, include it (don't miss opportunities).
    """
    if service_date is None:
        return True
    weekday = service_date.weekday()  # 0=Mon … 6=Sun
    if weekday >= 5:  # weekend
        return True
    if service_time is None:
        return True  # unknown time — include to avoid missing it
    return service_time >= time(17, 0)


# ── Scraping ───────────────────────────────────────────────────────────────

def scrape_listings() -> list:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; salon-apprentice-bot/1.0)"}
    session = requests.Session()
    resp = session.get(LISTINGS_URL, headers=headers, timeout=30)
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
        is_free = fee.lower() != "yes"

        # Determine which category this listing qualifies under
        is_balayage_nails = any(kw in combined for kw in KEYWORDS)
        # Balayage must be free
        if is_balayage_nails and "balayage" in combined and not is_free:
            is_balayage_nails = False

        is_haircut = (
            any(kw in combined for kw in HAIRCUT_KEYWORDS)
            and is_free
            and is_manhattan(neighborhood, salon)
            and is_womens_medium_haircut(combined)
        )

        if not is_balayage_nails and not is_haircut:
            continue

        service_date = parse_date(service_date_raw)
        if service_date and not is_target_date(service_date):
            continue

        # Fetch detail page to find service time, then apply availability filter
        listing_time = fetch_listing_time(link, session)
        if not is_available_slot(service_date, listing_time):
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
            "service_time": listing_time.strftime("%-I:%M %p") if listing_time else "",
        })

    return results


# ── Email ──────────────────────────────────────────────────────────────────

def listing_rows_html(listings: list) -> str:
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
          <td style="padding:12px 8px;color:#555;white-space:nowrap;">{l['service_date']}{(' @ ' + l['service_time']) if l.get('service_time') else ''}</td>
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


def build_morning_html(listings: list) -> str:
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


def build_update_html(new_listings: list, now: datetime) -> str:
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
