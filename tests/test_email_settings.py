"""The profile link and the attached PDF are set by an admin in the CRM: what is saved there is in
the next email the bot builds, with no restart, and .env decides only until something is saved."""
import base64

import pytest
from fastapi import HTTPException

import config
import crm
import db
import sender

PDF = b"%PDF-1.4\n% a tiny test file\n"


@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(crm, "ATTACH_DIR", tmp_path / "attachments")
    monkeypatch.setattr(config, "PROFILE_URL", "https://env.example/profile")
    env_pdf = tmp_path / "Env_Overview.pdf"
    env_pdf.write_bytes(PDF)
    monkeypatch.setattr(config, "ATTACH_PDF", str(env_pdf))
    db.init()
    crm.ensure_schema()
    with crm.conn() as c:
        uid = c.execute("INSERT INTO users (name, email, role, password_hash, must_change) VALUES (?,?,?,?,0)"
                        " RETURNING id", ("Joyce", "joyce@example.com", "admin", "x")).fetchone()[0]
    return {"id": uid, "role": "admin"}


def email():
    msg, _ = sender.build({"company": "Umoja SACCO", "industry": "default", "email": "info@umoja.example"})
    return sender.plain_of(msg), [p.get_filename() for p in msg.iter_attachments()]


def upload(admin, name, data=PDF):
    return crm.upload_pdf(crm.PdfIn(filename=name, data=base64.b64encode(data).decode()), admin)


def test_until_an_admin_saves_anything_env_decides(admin):
    text, files = email()
    assert "Company profile: https://env.example/profile" in text and files == ["Env_Overview.pdf"]
    out = crm.get_settings(admin)
    assert out["profile_from"] == ".env" and out["attachment_from"] == ".env"


def test_a_saved_profile_link_is_in_the_next_email(admin):
    crm.save_profile(crm.ProfileIn(profile_url="https://talksasa.example/profile", profile_label="Our profile"), admin)
    text, _ = email()
    assert "Our profile: https://talksasa.example/profile" in text and "env.example" not in text


def test_an_empty_link_means_no_profile_line_even_with_one_in_env(admin):
    crm.save_profile(crm.ProfileIn(profile_url=""), admin)
    text, _ = email()
    assert "profile" not in text.lower()


@pytest.mark.parametrize("url", ["talksasa.example", "javascript:alert(1)", "https://a b.example", "https://x.example/\"><b>"])
def test_a_link_that_isnt_a_web_address_is_refused(admin, url):
    with pytest.raises(HTTPException) as e:
        crm.save_profile(crm.ProfileIn(profile_url=url), admin)
    assert e.value.status_code == 400
    assert db.get_setting("profile_url") is None


def test_an_uploaded_pdf_is_attached_from_the_next_email(admin):
    out = upload(admin, "TalkSasa Profile 2026.pdf")
    assert out["attachment"]["name"] == "TalkSasa Profile 2026.pdf"
    text, files = email()
    assert files == ["TalkSasa Profile 2026.pdf"] and sender.ATTACHMENT_LINE in text


def test_the_upload_name_can_never_be_a_path(admin):
    out = upload(admin, "..\\..\\..\\Windows\\evil<>.pdf")
    assert out["attachment"]["name"] == "evil.pdf"
    assert (crm.ATTACH_DIR / "evil.pdf").read_bytes() == PDF


@pytest.mark.parametrize("data, why", [(b"MZ not a pdf", "isn't a PDF"), (b"%PDF-" + b"x" * (5 * 1024 * 1024), "under 5 MB")],
                         ids=["not-a-pdf", "too-big"])
def test_a_file_that_isnt_a_small_pdf_is_refused(admin, data, why):
    with pytest.raises(HTTPException) as e:
        upload(admin, "file.pdf", data)
    assert why in e.value.detail
    assert email()[1] == ["Env_Overview.pdf"]  # unchanged


def test_broken_upload_data_is_refused(admin):
    with pytest.raises(HTTPException) as e:
        crm.upload_pdf(crm.PdfIn(filename="a.pdf", data="not base64!!"), admin)
    assert e.value.status_code == 400


def test_switching_the_attachment_off_drops_the_pdf_and_its_line(admin):
    crm.remove_pdf(admin)
    text, files = email()
    assert files == [] and sender.ATTACHMENT_LINE not in text
    assert crm.get_settings(admin)["attachment"] is None
