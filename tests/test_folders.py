"""Every folder but Sent and Drafts is read, so a reply in Junk or moved to your own folder
still shows; an email found in two folders is stored once."""
import db
from fakemail import SENT_ID, message, run, status

LEADBOT_ID = "<176061234567.4242.1234567890123456789@talksasa.co.ke>"


def stored():
    with db.conn() as c:
        return [tuple(r) for r in c.execute("SELECT lead_id, from_email, label, imap_uid FROM replies ORDER BY id")]


def reply(msg_id="<r1@nyerihigh.com>"):
    return message("info@nyerihigh.com", "Re: Fee reminders", "Yes, call me Monday.",
                   in_reply_to=SENT_ID, message_id=msg_id)


def test_reply_in_junk_is_shown_and_alerted(leads, ai, alerts, monkeypatch):
    run(monkeypatch, folders={"Junk": {b"1": reply()}})
    assert stored() == [(leads["info@nyerihigh.com"], "info@nyerihigh.com", "interested", "Junk:1")]
    assert len(alerts) == 1 and status("info@nyerihigh.com") == "replied"


def test_reply_moved_to_your_own_folder_is_shown(leads, ai, alerts, monkeypatch):
    run(monkeypatch, folders={"clients responses": {b"5": reply()}})
    assert [r[3] for r in stored()] == ["clients responses:5"]


def test_sent_and_drafts_are_never_read(leads, ai, alerts, monkeypatch):
    ours = message("florence@talksasa.co.ke", "Re: Fee reminders", "Draft", in_reply_to=SENT_ID)
    run(monkeypatch, folders={"Sent": {b"1": ours}, "Drafts": {b"2": ours}})
    assert stored() == [] and not ai


def test_same_email_in_two_folders_is_stored_once(leads, ai, alerts, monkeypatch):
    run(monkeypatch, reply(), folders={"Archive": {b"9": reply()}})
    assert len(stored()) == 1 and len(alerts) == 1


def test_a_new_mailbox_reuses_uids_without_losing_replies(leads, ai, alerts, monkeypatch):
    import config
    run(monkeypatch, reply())  # read from the first mailbox as INBOX UID 1
    monkeypatch.setattr(config, "IMAP_USER", "sales@talksasa.co.ke")
    run(monkeypatch, reply("<r2@nyerihigh.com>"))  # the new mailbox's own UID 1
    assert [r[3] for r in stored()] == ["1", "sales@talksasa.co.ke/1"]


def test_moved_after_it_was_stored_is_not_stored_again(leads, ai, alerts, monkeypatch):
    run(monkeypatch, reply())  # stored from INBOX
    run(monkeypatch, folders={"clients responses": {b"3": reply()}})  # later moved
    assert len(stored()) == 1 and len(alerts) == 1


def test_reply_stored_before_message_ids_were_kept_is_recognised(leads, ai, alerts, monkeypatch):
    # stored by an older version, without its Message-ID, while it was INBOX uid 1
    db.add_reply(leads["info@nyerihigh.com"], "info@nyerihigh.com", "Re: Fee reminders", "x",
                 "interested", "s", "1")
    run(monkeypatch, reply(), folders={"Archive": {b"7": reply()}})
    assert len(stored()) == 1


def test_same_uid_in_different_folders_are_different_emails(leads, ai, alerts, monkeypatch):
    other = message("info@dazzcode.com", "Re: OTPs", "Send pricing", message_id="<r2@dazzcode.com>")
    run(monkeypatch, reply(), folders={"Junk": {b"1": other}})
    assert sorted(r[3] for r in stored()) == ["1", "Junk:1"]


def test_unlinked_delivery_failure_in_junk_is_shown_without_alert(leads, ai, alerts, monkeypatch):
    dsn = message("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                  "The group you tried to contact (brands) may not exist.")
    run(monkeypatch, folders={"Junk": {b"4": dsn}})
    assert stored() == [(None, "mailer-daemon@googlemail.com", "bounce", "Junk:4")]
    assert not alerts and not ai


def test_delivery_failure_linked_through_the_thread(leads, ai, alerts, monkeypatch):
    dsn = message("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                  "Your message wasn't delivered.", headers={"References": SENT_ID})
    run(monkeypatch, dsn)
    assert stored()[0][:3] == (leads["info@nyerihigh.com"], "mailer-daemon@googlemail.com", "bounce")
    assert status("info@nyerihigh.com") == "bounced"


def test_delayed_warning_is_still_not_a_bounce(leads, ai, alerts, monkeypatch):
    delayed = message("mailer-daemon@mail.talksasa.com", "Delayed Mail (still being retried)",
                      "Final-Recipient: rfc822; info@nyerihigh.com\nAction: delayed")
    run(monkeypatch, delayed)
    assert stored() == [] and status("info@nyerihigh.com") == "sent"
