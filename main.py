"""LeadBot command line.

  python main.py init                    create the database
  python main.py scrape companies.csv    find emails on company websites
  python main.py discover [category] [--max N]   search the web for new leads (DISCOVER_PER_DAY)
  python main.py discover-log [days]     what discovery did per niche each day (default 7)
  python main.py clean-leads [--apply]   leads to exclude (directories, events, associations, media...)
  python main.py names [--recent N] [--sent DAY]   company names before/after (waiting, N emailed last)
  python main.py names --apply [--batch N]   check and save one batch of names (the bot does this itself)
  python main.py verify                  check email domains
  python main.py personalise [--limit N] write a personal opening for verified leads from their websites
  python main.py rank                    top 20 waiting leads in sending order, with start year and score
  python main.py send --dry-run          preview emails without sending
  python main.py preview [industry] [--limit N]   show emails (plain and HTML) with their opener and use case
  python main.py send                    send today's batch
  python main.py sent [--days N]         list what was sent (date, company, email, step, subject)
  python main.py sent --show a@b.com     print the full email sent to that address
  python main.py inbox                   check replies once
  python main.py inbox --catch-up        re-read mail earlier checks skipped; matches are held for you
  python main.py watch                   check replies every few minutes (leave running)
  python main.py auto                    weekdays: send at 9am (topped up until 5pm), then discover; prepare from 5pm
  python main.py prepare                 get waiting leads ready for their first email (score, niche, contact, opener)
  python main.py send-group FILE [--dry-run]   a named-contact campaign: check addresses, send entries not sent yet
  python main.py stats                   counts by status and reply label
  python main.py contacts [--recent N]   how many leads got a named contact, by title and confidence
  python main.py contacts --reread [--limit N]   read waiting leads' sites again for contacts (best first; resumable)
  python main.py list-addresses [--limit N]   imported leads (CBK...): use an address their site publishes today
  python main.py import-list SOURCE FILE_OR_URL --niche NICHE [--dry-run]   a regulator's list (imports.py)
  python main.py import-all [--dry-run]  the latest list of every source: sasra, cbk_psp, cbk_mfb, wasreb, ira, earb, cma_sandbox
  python main.py bounces [days]          bounces in the last N days (default 30): by email type, lead source, step; top reasons
  python main.py score-report [days]     emailed, replied, interested, meetings by score band, scoring reason,
                                         niche, source, variant, contact type, company age (default all time)
  python main.py unsubscribe a@b.com     manually opt someone out
  python main.py export [folder]         write leads.csv, events.csv and replies.csv (joined on lead_id)
"""
import sys

import db


def take_number(args, flag):
    """Remove `flag N` from args and return N, or None if the flag isn't there."""
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args) or not args[i + 1].isdigit() or int(args[i + 1]) < 1:
        sys.exit(f"{flag} needs a number, e.g. {flag} 5")
    n = int(args[i + 1])
    del args[i:i + 2]
    return n


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    db.init()

    if cmd == "init":
        print("Database ready.")
    elif cmd == "scrape":
        import scraper
        scraper.run(args[0] if args else "companies.csv")
    elif cmd == "discover":
        import discover
        max_new = take_number(args, "--max")
        discover.run(args[0] if args else None, max_new)
    elif cmd == "send-group":
        import campaigns
        campaigns.main(args)
    elif cmd == "prepare":
        import prepare
        print(prepare.run())
    elif cmd == "names":
        import names
        day = None
        if "--sent" in args:
            i = args.index("--sent")
            day = args[i + 1] if i + 1 < len(args) else sys.exit("--sent needs a day, e.g. --sent 2026-11-02")
        recent = take_number(args, "--recent") or 30
        batch = take_number(args, "--batch") or names.BATCH
        names.run(apply="--apply" in args, day=day, recent=recent, batch=batch)
    elif cmd == "clean-leads":
        import quality
        quality.run(apply="--apply" in args)
    elif cmd == "discover-log":
        import discover
        discover.show_log(int(args[0]) if args and args[0].isdigit() else 7)
    elif cmd == "auto":
        import auto
        auto.run()
    elif cmd == "verify":
        import verify
        verify.run()
    elif cmd == "send":
        import sender
        sender.run(dry_run="--dry-run" in args)
    elif cmd == "sent":
        import sender
        if "--show" in args:
            i = args.index("--show")
            if i + 1 >= len(args):
                print("Usage: python main.py sent --show a@b.com")
                return
            sender.show_sent(args[i + 1])
        else:
            days = None
            if "--days" in args:
                i = args.index("--days")
                if i + 1 >= len(args) or not args[i + 1].isdigit() or int(args[i + 1]) < 1:
                    print("Usage: python main.py sent --days 7")
                    return
                days = int(args[i + 1])
            sender.sent_log(days)
    elif cmd == "personalise":
        import config
        import personalise
        limit = take_number(args, "--limit")
        if not config.USE_AI_OPENER:
            print("AI openers are off (USE_AI_OPENER=false in .env): emails use the templates only.")
        elif not personalise.run(limit=limit) and not db.leads_to_personalise(1):
            print("No verified leads waiting for a personal opening.")
    elif cmd == "rank":
        import ranking
        ranking.show()
    elif cmd == "preview":
        import sender
        limit = take_number(args, "--limit") or 1
        sender.preview(args[0] if args else None, limit)
    elif cmd == "inbox":
        import inbox
        if "--catch-up" in args:
            print(f"Caught up {inbox.check_once(catch_up=True)} messages an earlier check had skipped.")
        else:
            print(f"Handled {inbox.check_once()} replies.")
    elif cmd == "watch":
        import inbox
        inbox.watch()
    elif cmd == "contacts":
        import contacts
        recent = take_number(args, "--recent")
        limit = take_number(args, "--limit")
        reread = "--reread" in args
        if reread:
            args.remove("--reread")
        if args or (limit and not reread) or (recent and reread):
            sys.exit("Usage: python main.py contacts [--recent N] | contacts --reread [--limit N]")
        if reread:
            contacts.reread(limit)
        else:
            contacts.report(recent)
    elif cmd == "list-addresses":
        import contacts
        limit = take_number(args, "--limit")
        if args:
            sys.exit("Usage: python main.py list-addresses [--limit N]")
        contacts.check_listed(limit)
    elif cmd == "import-list":
        import imports
        imports.main(args)
    elif cmd == "import-all":
        import imports
        imports.main(["all"] + args)
    elif cmd == "bounces":
        import bounces
        if args and not args[0].isdigit():
            sys.exit("Usage: python main.py bounces [days]")
        bounces.report(int(args[0]) if args else bounces.DAYS)
    elif cmd == "score-report":
        import score_report
        if len(args) > 1 or (args and (not args[0].isdigit() or int(args[0]) < 1)):
            sys.exit("Usage: python main.py score-report [days]")
        score_report.report(int(args[0]) if args else None)
    elif cmd == "stats":
        leads, replies = db.stats()
        print("Leads:", leads or "none")
        print("Replies:", replies or "none")
        by_source = db.list_source_stats()
        if by_source:
            print("By list source (leads / emailed / replied / bounced):")
            for source, n in by_source.items():
                print(f"  {source:12} {n['leads']:>5} {n['emailed']:>8} {n['replied']:>8} {n['bounced']:>8}")
        import config
        import sender
        emailed, bounced = db.bounce_stats(config.BOUNCE_WINDOW_DAYS)
        rate = f"{100 * bounced / emailed:.1f}%" if emailed else "n/a"
        print(f"Bounces: {bounced} of {emailed} companies emailed in the last "
              f"{config.BOUNCE_WINDOW_DAYS} days ({rate}, limit "
              f"{config.BOUNCE_LIMIT_PERCENT:g}%)" + (" - SENDING PAUSED" if sender.bounce_gate() else ""))
    elif cmd == "unsubscribe" and args:
        db.unsubscribe(args[0])
        print(f"{args[0]} unsubscribed.")
    elif cmd == "export":
        for name, n in db.export(args[0] if args else ".").items():
            print(f"{name}: {n} rows")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
