---
name: lead-finder
description: Find potential clients who need websites built by searching the web (Reddit, Twitter/X, Facebook, LinkedIn, Craigslist, Google Maps, freelancer platforms) and contact them via WhatsApp with a professional pitch. Use when asked to find leads, prospect for clients, do outreach, or find people who want websites.
metadata: {"nanobot":{"emoji":"🔍","requires":{"bins":["python"],"env":["BRAVE_API_KEY"]}}}
---

# Lead Finder

Autonomous web prospecting for website development clients + WhatsApp outreach.

## Workflow

### 1. Search for Leads

Run multiple targeted searches using `web_search`. Combine platform-specific queries with intent keywords.

**High-intent search queries** (copy-paste ready):

```
"need a website" OR "looking for web developer" site:reddit.com
"need website built" OR "want a website" site:twitter.com
"looking for someone to build" website OR "web developer needed" site:facebook.com
"need a website" OR "website for my business" site:craigslist.org
"hiring web developer" OR "need website" site:linkedin.com
small business "no website" "contact us" whatsapp
"I need a website" OR "build me a website" -tutorial -course
restaurant OR salon OR clinic "whatsapp" -website site:.com
new business "opening soon" "whatsapp" OR "wa.me"
freelance "web developer needed" OR "website project" 2026
```

**Local business queries** (replace CITY):

```
CITY businesses "whatsapp" -website
new restaurant CITY "opening" "contact"
CITY small business directory
```

**Platform-specific scraping** — after search, use `web_fetch` on promising results to extract:
- Phone numbers (regex: `\+?\d{10,15}`)
- WhatsApp links (`wa.me/`, `api.whatsapp.com/send?phone=`)
- Business names and context about what they need

### 2. Extract & Score Leads

From fetched pages, extract leads using these patterns:

| Signal | Score |
|--------|-------|
| Explicitly says "need a website" | ★★★ |
| Business with no website, has WhatsApp | ★★★ |
| Posted in last 7 days | ★★ |
| Has phone number visible | ★★ |
| General "looking for developer" | ★ |

Prioritize ★★★ leads. Skip leads older than 30 days.

### 3. Track Leads

Before contacting anyone, check the tracker to avoid duplicates:

```bash
python nanobot/skills/lead-finder/scripts/lead_tracker.py check --phone "+1234567890"
```

After contacting, log the lead:

```bash
python nanobot/skills/lead-finder/scripts/lead_tracker.py add --phone "+1234567890" --name "John's Bakery" --source "reddit" --message "intro"
```

List all leads:

```bash
python nanobot/skills/lead-finder/scripts/lead_tracker.py list
```

### 4. Contact via WhatsApp

Use `send_whatsapp` tool to message leads. Always personalize based on what you found.

**Intro message template** (adapt to context):

```
Hi [Name]! 👋

I came across your [business/post] and noticed you're looking for a website.

I'm a professional web developer and I'd love to help you get online. I build modern, mobile-friendly websites that help businesses attract more customers.

Would you be interested in a quick chat about what you need? I can share some of my recent work too.

Looking forward to hearing from you! 🚀
```

**Follow-up** (if no response after 2-3 days):

```
Hi [Name], just following up on my earlier message.

I specialize in building websites for [their industry] businesses. Happy to show you some examples if you're interested.

No pressure at all — just wanted to make sure you saw my message! 😊
```

### 5. Schedule Recurring Sweeps

Use `cron` to automate daily lead searches:

```
cron(action="add", message="Run lead finder: search for people who need websites built, extract leads, and contact new ones via WhatsApp. Use the lead-finder skill.", every_seconds=86400)
```

## Rules

1. **No spam** — max 20 new contacts per day
2. **No duplicates** — always check tracker before messaging
3. **Personalize** — never send identical copy-paste messages
4. **Respect opt-outs** — if someone says "not interested" or "stop", never contact again
5. **Rate limit** — wait 30-60 seconds between messages to avoid WhatsApp bans
6. **Business hours only** — send messages between 9 AM–8 PM in the recipient's timezone
7. **Track everything** — log every lead and outcome in the tracker
