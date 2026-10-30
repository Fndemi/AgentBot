# LeadBot — bulk SMS outreach

Finds company emails, sends intro emails in small daily batches, reads replies, labels them with AI, and pings you on Telegram when someone is interested.

```
companies.csv → scrape → verify → send → inbox (AI label) → Telegram alert
```

## Setup (30 min)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then fill it in
python main.py init
```

**AI option A — Ollama (free, open source, local):** install from ollama.com, then `ollama pull qwen2.5:7b`. Set `AI_PROVIDER=ollama`.
**AI option B — Groq (free tier, no big PC needed):** get a key at console.groq.com. Set `AI_PROVIDER=groq` and `GROQ_API_KEY`.

**Telegram:** see the steps at the top of `notify.py`, then test with `python notify.py`.

**Test the AI:** `python classifier.py` runs 5 sample replies (including Swahili) and prints the labels.

## Daily use

```bash
python main.py scrape companies.csv   # add leads from your own list
python main.py discover sacco --max 5 # or find leads by web search (see discover.py)
python main.py verify                 # drop dead domains
python main.py send --dry-run         # read 3 sample emails first
python main.py preview                # one email, plain and HTML versions
python main.py send                   # send today's batch (DAILY_LIMIT)
python main.py watch                  # leave running; alerts you on replies
python main.py stats
python main.py export                 # leads.csv, events.csv, replies.csv (join on lead_id)
python main.py auto                   # or leave this running: does all of the above on weekdays at 9am
streamlit run dashboard.py            # optional dashboard
```

To run unattended on Linux, add to crontab:
```
0 9 * * 1-5  cd /path/to/leadbot && venv/bin/python main.py send
*/5 * * * *  cd /path/to/leadbot && venv/bin/python main.py inbox
```

## Templates

`templates/<industry>.txt` is used when a lead's `industry` matches (e.g. `sacco`, `school`). Otherwise `default.txt`. The subject is the template's own fixed `Subject:` line. Only industries marked `approved: true` in `uses.yaml` are emailed; leads in other industries are skipped and stay waiting. With `USE_AI_OPENER=false` in `.env` (the default) emails are templates only: no opener, and `{uses}` is the first three uses for the industry in `uses.yaml`. With `true`, the body has two AI slots: `{opener}` (one sentence, at most 20 words, saying what the lead does in our own words; left out when there is no good one, so the email starts with "I'm Florence from TalkSasa Communications in Nairobi.") and `{uses}` (picked from that industry's approved list in `uses.yaml`, never invented). Other placeholders: `{company} {sender} {our_company} {phone} {website} {profile_line} {attachment_line}` (`{profile_line}` is "Company profile: <PROFILE_URL>", or nothing if `PROFILE_URL` is empty).

### A/B/C rotation (sacco, school, church, fintech, digital_lender, hr_payroll, saas, tech_startup, ecommerce, delivery_app, edtech, healthtech, microfinance)

These niches have three first emails, one per technique: `templates/<niche>_A.txt` (quick question), `_B.txt` (free test) and `_C.txt` (niche angle), each with its own follow-up 1 in `templates/followup/<niche>_<variant>.txt`, and one follow-up 2 for all of them, `templates/followup2/<niche>.txt`. A lead gets the variant sent least so far in its niche (random on a tie) when its first email goes out, and keeps it for its follow-ups; it is saved on the lead and on every sent row (`template_variant`). `variants: [A, B]` in `uses.yaml` switches a variant off for new leads. These first emails carry no PDF: `{profile_line}` links to the profile instead.

Every one of them asks for a 15-minute meeting: `{slot1}` and `{slot2}` are two times from `MEETING_TIMES` on different working days (no weekends or Kenyan public holidays, see `rotation.py`), 2 to 7 working days after the email goes out, picked per lead, e.g. "Tuesday 4 Nov at 10am". `{booking_line}` is "Or pick any time that suits you here: <BOOKING_URL>", or nothing without `BOOKING_URL`. `{meet_options}` offers a visit first to SACCOs, schools and churches, and a video call first to the rest. `{free_sms}`, `{setup_days}` and `{price_from}` are `OFFER_FREE_SMS`, `SETUP_TIME` and `PRICE_FROM`: nothing is sent while any of those three is empty.

Leads emailed before the rotation keep their old wording: `templates/<niche>.txt` and `templates/followup/<niche>.txt` stay as the legacy templates. Their follow-up 1 gets the meeting request added before the signature, and their follow-up 2 is the niche's new one. The old follow-up 2s are kept in `templates/legacy/followup2/` and no longer sent.

### Who the email goes to, and the opener

Just before a lead's first email, LeadBot reads its website (`contacts.py`; no manual step). If the team, about or leadership page names a decision-maker (CEO, founder, CTO, head of product, operations, partnerships, marketing or finance) and that person's exact address is published on the same site, the name, title, address and page are saved on the lead, the email goes to them and `{greeting}` is "Hello Jane,". An address is never guessed or built from a name. Otherwise the email goes to the first of sales@, partnerships@, business@, tech@, ops@, hello@, contact@, info@ the site shows on its own domain, otherwise to the address the lead already had (kept in `source_email` when it changes). Leads already emailed never change address.

With `USE_AI_OPENER=true`, `WRITER_MODEL` writes `{opener}`: one sentence (at most 25 words) about one specific fact on the lead's own site, saved with the exact sentence it comes from and that page's address. It is used only if it passes a check in code (that sentence is on that page; the opener adds no number or name the page doesn't have) and a second check by the AI (`GROQ_MODEL`). If anything fails, the email is the normal template without an opener, and the lead page in the CRM says why. The A/B/C first emails have `{opener}` as the first line after the greeting. The CRM lead page shows the contact, the opener and its source; What's working compares emails with and without an opener, and to a named contact against a role address.

## Getting leads (companies.csv)

Microfinance banks: `python targeting.py import-microfinance` adds every bank in the Central Bank of Kenya's latest directory of licensed microfinance banks (found on centralbank.go.ke/bank-supervision) as a `microfinance` lead, skipping any already there. `python targeting.py import-lenders` does the same for CBK's directory of digital credit providers.

Columns: `company,website,industry,email`. Leave `email` blank to have it scraped. Good public sources: SASRA licensed SACCO list, Ministry of Education school lists, Kenya Yellow Pages, chamber of commerce and trade association member lists. Skip LinkedIn and Google Maps scraping (against their terms).

## Before the first real send

- [ ] Separate outreach domain (not talksasa.co.ke), so spam flags can't hurt the main domain
- [ ] SPF, DKIM and DMARC records set on that domain (check at mail-tester.com, aim for 9/10+)
- [ ] Send a few test emails to your own Gmail and check they land in Inbox, not Spam
- [ ] Start at `DAILY_LIMIT=20–25`, raise by ~10/day each week if bounces stay under 3%
- [ ] Confirm with your boss whether TalkSasa needs ODPC registration for this

## What each label does

| Label | Lead status | Alert you? |
|---|---|---|
| interested | replied | 🔥 yes |
| question | replied | ❓ yes |
| needs_review | replied | no (stays unread in inbox) |
| not_interested | replied | no (stays unread in inbox) |
| out_of_office | replied | no |
| unsubscribe | unsubscribed, never emailed again | no |
| bounce | bounced | no |
