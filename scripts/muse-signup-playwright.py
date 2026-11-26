#!/usr/bin/env python3
"""Create a Muse account on muse.ai in desktop Chrome via Playwright.

Runs on a GitHub Actions runner (ubuntu-latest) under xvfb-run, headed
Chromium -- no emulator, no adb, no screen coordinates. All interaction is
through DOM selectors.

Flow:
  1. temp.tf: keep ONLY the gmail.com provider checked (uncheck outlook.com,
     hotmail.com, high.edu.pl), click NEW ADDRESS, read the generated
     gmail.com address. Screenshot step1-temp.png.
  2. muse.ai in a new tab: fill "Mobile number or email", click Continue.
     Screenshots step2-form.png (before) and step3-after-continue.png.
  3. Wait up to 60 s for the next step. If a verification code is asked
     for, fetch it from the temp.tf inbox (FETCH INBOX): check for up to
     5 minutes, every ~15 s, accepting mail from any sender, scanning
     only actual message rows for a 6-digit code. If a code is found,
     enter it without clicking the hidden field; if a verification link
     is found instead, open it in the muse.ai tab. If a name is asked
     for, enter "Muse". If no mail arrives after 5 minutes, generate a
     fresh gmail.com address and resubmit once (max 2 addresses).
  4. STOP (screenshot step4-verify.png, log the reason, do not proceed) if
     a phone number, a payment card, a captcha/Turnstile, or a rejected
     temp email is asked for.

Run #2 change: the inbox wait is 5 minutes, every ~15 s (was 90 s), and
mail from any sender is accepted -- run #1 only looked for "Muse", so a
code from Meta or noreply would have been missed.

Run #3 changes: the inbox scanner ignores the page chrome (run #2 matched
the TOTAL RECEIVED counter as a code) and scans only message rows for a
6-digit code; the code is entered without clicking the hidden sr-only
input; a fresh address is tried once if no mail arrives after 5 minutes.

Outputs (all under /tmp, uploaded as the muse-signup-desktop artifact):
  muse-account.txt, step1-temp.png, step2-form.png,
  step3-after-continue.png, step4-verify.png

The account is for Muse (the assistant): the name entered is "Muse".

The script never raises out of main: every failure is logged, a screenshot
is taken, and it exits 0 so the workflow still uploads the artifacts.
"""

import re
import time

from playwright.sync_api import sync_playwright

TMP = "/tmp"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CODE6_RE = re.compile(r"(?<![0-9])([0-9]{6})(?![0-9])")

PHONE_WORDS = ("phone verification", "verify your phone", "enter your phone",
               "sms code", "texted you", "phone number")
CARD_WORDS = ("payment", "card number", "credit card", "debit card",
              "add a card")
CAPTCHA_WORDS = ("captcha", "turnstile", "verify you are human",
                 "not a robot")
REJECTED_WORDS = ("disposable", "not accepted", "try a different email",
                  "invalid email", "cannot use this email")
CODE_WORDS = ("verification code", "enter the code", "check your email",
              "check your inbox", "we sent", "sent you a code")
SUCCESS_WORDS = ("welcome", "account created", "get started",
                 "your account is ready", "dashboard")


def log(msg):
    print("[muse-signup-desktop] " + str(msg), flush=True)


def shot(page, name):
    path = TMP + "/" + name
    log("screenshot -> " + path)
    try:
        page.screenshot(path=path)
    except Exception as exc:
        log("screenshot EXC: %r" % (exc,))


def body_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def find_email(text, domain=None):
    for m in EMAIL_RE.finditer(text):
        if domain is None or m.group(0).lower().endswith("@" + domain):
            return m.group(0)
    return None


def strip_chrome(text):
    """Remove temp.tf page-chrome counters so a site-wide number can never
    pass as a verification code (run #2 matched "TOTAL RECEIVED 8064064")."""
    text = re.sub(r"total received\s+[0-9,]+\s+messages", "", text, flags=re.I)
    text = re.sub(r"total available.*?addresses", "", text, flags=re.I)
    return text


def find_code6(text):
    """A 6-digit verification code only. The 4-8 digit fallback matched
    the site's TOTAL RECEIVED counter in run #2, so it is not used."""
    m = CODE6_RE.search(text)
    return m.group(1) if m else None


def inbox_rows_text(page):
    """Text of actual inbox message rows only (no page chrome).

    Returns (text, count): the joined subject/sender text of the message
    rows found, and how many rows there were. Rows that only say the
    inbox is empty are not counted.
    """
    for sel in ("tbody tr", "[class*='inbox-item' i]", "[class*='mail-item' i]",
                "[class*='message-item' i]", "[class*='email-item' i]",
                "[role='row']"):
        try:
            rows = page.locator(sel).all()
        except Exception:
            continue
        texts = []
        for row in rows:
            try:
                t = row.inner_text() or ""
            except Exception:
                continue
            low = t.lower()
            if not t.strip():
                continue
            if "no mail found" in low or "inbox is empty" in low:
                continue
            texts.append(t)
        if texts:
            return "\n".join(texts), len(texts)
    return "", 0


def locate_in_frames(page, make):
    """Try make(frame) on the page and each child frame; return the first
    locator with matches, else None."""
    for frame in page.frames:
        try:
            loc = make(frame)
            if loc.count():
                return loc
        except Exception:
            continue
    return None


def provider_checkbox(page, domain):
    """The checkbox for one temp.tf provider domain, or None.

    Tries: a label containing the domain with a checkbox inside it; a
    checkbox whose accessible name holds the domain; a checkbox whose
    parent element mentions the domain.
    """
    try:
        lab = page.locator("label", has_text=domain).first
        if lab.count():
            box = lab.locator("input[type='checkbox']").first
            if box.count():
                return box
    except Exception:
        pass
    try:
        box = page.get_by_role("checkbox", name=domain).first
        if box.count():
            return box
    except Exception:
        pass
    try:
        for box in page.locator("input[type='checkbox']").all():
            try:
                parent = box.locator("xpath=..")
                if domain in (parent.inner_text() or ""):
                    return box
            except Exception:
                continue
    except Exception:
        pass
    return None


def set_providers_gmail_only(page):
    """Uncheck every temp.tf provider except gmail.com. Returns True when
    gmail.com is present on the page."""
    for name in ("outlook.com", "hotmail.com", "high.edu.pl"):
        box = provider_checkbox(page, name)
        if box is None:
            log("provider %s: no checkbox found" % name)
            continue
        try:
            checked = box.is_checked()
        except Exception as exc:
            log("provider %s: is_checked EXC: %r" % (name, exc))
            continue
        log("provider %s: checked=%s" % (name, checked))
        if not checked:
            continue
        try:
            box.uncheck(timeout=10000)
        except Exception as exc:
            log("provider %s: uncheck EXC: %r, trying the label" % (name, exc))
            try:
                page.locator("label", has_text=name).first.click(timeout=10000)
            except Exception as exc2:
                log("provider %s: label click EXC: %r" % (name, exc2))
        try:
            log("provider %s: checked=%s after uncheck" % (name, box.is_checked()))
        except Exception:
            pass
    present = provider_checkbox(page, "gmail.com") is not None
    log("provider gmail.com: present=%s (left checked)" % present)
    return present


def click_new_address(page):
    loc = page.get_by_role("button", name="NEW ADDRESS", exact=True).first
    if loc.count():
        try:
            loc.click(timeout=10000)
            return True
        except Exception as exc:
            log("NEW ADDRESS click EXC: %r" % (exc,))
    try:
        el = page.get_by_text("NEW ADDRESS").first
        if el.count():
            el.click(timeout=10000)
            return True
    except Exception as exc:
        log("NEW ADDRESS text click EXC: %r" % (exc,))
    return False


def read_temp_email(page, domain="gmail.com"):
    email = find_email(body_text(page), domain)
    if email:
        return email
    # the address may sit in a readonly input instead of page text
    try:
        for inp in page.locator("input").all():
            try:
                val = inp.input_value() or ""
            except Exception:
                continue
            m = EMAIL_RE.search(val)
            if m and m.group(0).lower().endswith("@" + domain):
                return m.group(0)
    except Exception:
        pass
    return None


def temp_email(page):
    """Get a gmail.com address from temp.tf (up to 25 NEW ADDRESS taps)."""
    page.goto("https://temp.tf", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    log("temp.tf title: " + page.title())
    set_providers_gmail_only(page)
    email = None
    for attempt in range(25):
        if not click_new_address(page):
            log("NEW ADDRESS button not found (attempt %d)" % (attempt + 1))
            page.wait_for_timeout(2000)
            continue
        page.wait_for_timeout(3000)
        email = read_temp_email(page)
        if email:
            log("gmail.com address found on NEW ADDRESS attempt %d" % (attempt + 1))
            break
        log("attempt %d: no gmail.com address yet" % (attempt + 1))
    return email


def click_button(page, names):
    """Click the first matching button (exact accessible name)."""
    for name in names:
        loc = locate_in_frames(page, lambda f, n=name: f.get_by_role("button", name=n, exact=True))
        if loc is not None:
            try:
                loc.first.click(timeout=15000)
                log("clicked button %r" % name)
                return True
            except Exception as exc:
                log("button %r click EXC: %r" % (name, exc))
    log("none of the buttons found: %s" % (names,))
    return False


def fill_email(page, email):
    loc = locate_in_frames(page, lambda f: f.get_by_label("Mobile number or email"))
    if loc is None:
        loc = locate_in_frames(page, lambda f: f.locator("input[type='email']"))
    if loc is None:
        loc = locate_in_frames(page, lambda f: f.locator("input"))
    if loc is None:
        log("email field not found")
        return False
    try:
        loc.first.click(timeout=10000)
        loc.first.fill(email, timeout=10000)
    except Exception as exc:
        log("email fill EXC: %r" % (exc,))
        return False
    log("email field filled with " + email)
    return True


def handle_name(page):
    """Fill a name field with Muse, if one is showing. Returns True when
    a name was entered."""
    loc = locate_in_frames(page, lambda f: f.get_by_label(re.compile("name", re.I)))
    if loc is None:
        loc = locate_in_frames(page, lambda f: f.locator("input[placeholder*='name' i]"))
    if loc is None:
        return False
    try:
        loc.first.fill("Muse", timeout=10000)
    except Exception as exc:
        log("name fill EXC: %r" % (exc,))
        return False
    log("name field filled with Muse")
    return True


def classify(text):
    if any(w in text for w in PHONE_WORDS):
        return "phone"
    if any(w in text for w in CARD_WORDS):
        return "card"
    if any(w in text for w in CAPTCHA_WORDS):
        return "captcha"
    if any(w in text for w in REJECTED_WORDS):
        return "rejected"
    if any(w in text for w in CODE_WORDS):
        return "code"
    if any(w in text for w in SUCCESS_WORDS):
        return "success"
    return "unknown"


def wait_next_step(page, seconds=60):
    """Wait up to `seconds` for the page to leave the email form or show
    an outcome. Returns the lower-cased body text."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = body_text(page).lower()
        if "mobile number or email" not in text:
            return text
        if any(w in text for w in CODE_WORDS + PHONE_WORDS + CARD_WORDS +
               CAPTCHA_WORDS + REJECTED_WORDS + SUCCESS_WORDS):
            return text
        page.wait_for_timeout(2000)
    return body_text(page).lower()


def find_link(page, text):
    """An absolute verification/confirm link: first from anchor hrefs,
    then from any URL printed in the given text. None when there is none."""
    try:
        for a in page.locator("a").all():
            try:
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            low = href.lower()
            if href.startswith("http") and any(w in low for w in ("verif", "confirm", "activate", "muse")):
                return href
    except Exception:
        pass
    for m in re.finditer(r"https?://[^\s\"'<>]+", text):
        url = m.group(0)
        low = url.lower()
        if any(w in low for w in ("verif", "confirm", "activate", "muse")):
            return url
    return None


def open_newest_message(page):
    """Click the first plausible inbox row, to open the newest message.
    Returns True when a row was clicked."""
    for sel in ("tbody tr", "[class*='inbox-item' i]", "[class*='mail-item' i]",
                "[class*='message-item' i]", "[class*='email-item' i]"):
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def fetch_code(temp_page):
    """Fetch the Muse verification code (or link) from the temp.tf inbox.

    Checks for up to 5 minutes, every ~15 s. Accepts mail from ANY sender.
    Only actual message rows (subject/sender/body) are scanned for a
    6-digit code -- the page chrome is never scanned, and an empty inbox
    ("Inbox is empty" / "No mail found") is logged as empty and waited
    out. Returns (value, kind) where kind is "code", "link", or None when
    nothing arrived.
    """
    temp_page.bring_to_front()
    temp_page.wait_for_timeout(2000)
    deadline = time.time() + 300
    check = 0
    while time.time() < deadline:
        check += 1
        loc = temp_page.get_by_role("button", name="FETCH INBOX", exact=True).first
        try:
            if loc.count():
                loc.click(timeout=10000)
            else:
                temp_page.get_by_text("FETCH INBOX").first.click(timeout=10000)
        except Exception as exc:
            log("FETCH INBOX click EXC: %r" % (exc,))
        temp_page.wait_for_timeout(4000)
        text = body_text(temp_page)
        rows_text, n = inbox_rows_text(temp_page)
        if n:
            log("inbox check %d: %d message row(s) present" % (check, n))
            scanned = strip_chrome(rows_text)
        elif "inbox is empty" in text.lower() or "no mail found" in text.lower():
            log("inbox check %d: empty, no mail for this address" % (check,))
            temp_page.wait_for_timeout(11000)
            continue
        else:
            log("inbox check %d: no message rows found, scanning cleaned page text" % check)
            scanned = strip_chrome(text)
        code = find_code6(scanned)
        if code:
            log("code found in the inbox list")
            return code, "code"
        link = find_link(temp_page, scanned)
        if link:
            log("verification link found in the inbox list: " + link)
            return link, "link"
        # No code/link in the list text: open the newest message and scan it.
        if open_newest_message(temp_page):
            temp_page.wait_for_timeout(3000)
            opened = strip_chrome(body_text(temp_page))
            code = find_code6(opened)
            if code:
                log("code found in the opened message")
                return code, "code"
            link = find_link(temp_page, opened)
            if link:
                log("verification link found in the opened message: " + link)
                return link, "link"
            log("opened message has no code or link")
        temp_page.wait_for_timeout(11000)
    log("no verification code or link arrived in 5 minutes")
    return None, None


def enter_code(page, code):
    """Enter the verification code on muse.ai. Returns True on success.

    The code field is a visually-hidden (sr-only) input covered by a div
    that intercepts pointer events, so it is never clicked: fill it
    directly, fall back to a force-fill, then to setting the value via
    JavaScript, then to typing into the visible OTP boxes.
    """
    loc = locate_in_frames(page, lambda f: f.get_by_label(re.compile("code", re.I)))
    if loc is None:
        loc = locate_in_frames(page, lambda f: f.locator("input[placeholder*='code' i]"))
    if loc is None:
        loc = locate_in_frames(page, lambda f: f.locator("input[inputmode='numeric']"))
    if loc is None:
        log("code field not found")
        return False
    field = loc.first
    filled = False
    for name, step in (("fill", 1), ("force-fill", 2), ("js-set", 3)):
        try:
            if step == 1:
                field.fill(code, timeout=10000)
            elif step == 2:
                field.fill(code, timeout=10000, force=True)
            else:
                field.evaluate(
                    "(el, v) => {"
                    " const setter = Object.getOwnPropertyDescriptor("
                    " window.HTMLInputElement.prototype, 'value').set;"
                    " setter.call(el, v);"
                    " el.dispatchEvent(new Event('input', {bubbles: true}));"
                    " el.dispatchEvent(new Event('change', {bubbles: true}));"
                    " }", code)
        except Exception as exc:
            log("code %s EXC: %r" % (name, exc))
            continue
        try:
            if field.input_value(timeout=5000) == code:
                filled = True
                log("code field filled via %s" % name)
                break
        except Exception:
            pass
        log("code %s did not stick" % name)
    if not filled:
        # Last resort: click the visible OTP container and type the code.
        try:
            field.locator("xpath=ancestor::div[1]").click(timeout=5000)
            page.keyboard.type(code, delay=80)
            if field.input_value(timeout=5000) == code:
                filled = True
                log("code field filled by typing into the visible OTP boxes")
        except Exception as exc:
            log("code typing EXC: %r" % (exc,))
    if not filled:
        log("STOP: could not fill the code field")
        return False
    if not click_button(page, ["Verify", "Continue"]):
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    return True


def save_account(email, outcome):
    with open(TMP + "/muse-account.txt", "w") as fh:
        fh.write("email: " + (email or "none") + "\n")
        fh.write("name: Muse\n")
        fh.write("outcome: " + outcome + "\n")
    log("saved " + TMP + "/muse-account.txt")


def run_signup():
    outcome = "unknown"
    email = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ])
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        temp_page = context.new_page()
        page = context.new_page()
        try:
            # Up to 2 gmail.com addresses: if no mail arrives after the
            # full 5-minute wait for the first one, generate a fresh
            # address and resubmit the muse.ai form once.
            for email_attempt in range(1, 3):
                # (1) temp.tf: a gmail.com address (fresh on each attempt)
                email = temp_email(temp_page)
                shot(temp_page, "step1-temp.png")
                if not email:
                    log("STOP: no gmail.com temp email found on temp.tf")
                    save_account(None, "no gmail.com temp email")
                    return
                log("TEMP EMAIL (attempt %d): %s" % (email_attempt, email))
                save_account(email, "in progress")

                # (2) muse.ai: fill the email form and submit
                page.goto("https://muse.ai", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                log("muse.ai title: " + page.title() + " url: " + page.url)
                shot(page, "step2-form.png")
                if not fill_email(page, email):
                    log("STOP: could not fill the email field on muse.ai")
                    shot(page, "step4-verify.png")
                    save_account(email, "could not fill the email field")
                    return
                if not click_button(page, ["Continue"]):
                    log("Continue click failed, pressing Enter instead")
                    try:
                        page.keyboard.press("Enter")
                    except Exception:
                        pass
                page.wait_for_timeout(3000)
                shot(page, "step3-after-continue.png")

                # (3) what comes next? up to 3 rounds of code/name handling
                retry = False
                for round_no in range(1, 4):
                    text = wait_next_step(page, seconds=60)
                    state = classify(text)
                    log("after Continue, round %d: state=%s" % (round_no, state))
                    if state in ("phone", "card", "captcha", "rejected"):
                        log("STOP: " + state)
                        outcome = state
                        break
                    if state == "success":
                        log("ACCOUNT CREATED (apparent)")
                        outcome = "account created (apparent)"
                        break
                    if state == "code":
                        log("verification code requested, fetching it from the temp inbox")
                        value, kind = fetch_code(temp_page)
                        if kind is None:
                            log("no mail arrived after 5 minutes for %s" % email)
                            outcome = "verification code never arrived"
                            retry = (email_attempt == 1)
                            break
                        page.bring_to_front()
                        page.wait_for_timeout(1000)
                        if kind == "link":
                            log("verification link found, opening it in the muse.ai tab")
                            try:
                                page.goto(value, wait_until="domcontentloaded", timeout=60000)
                            except Exception as exc:
                                log("verification link open EXC: %r" % (exc,))
                            page.wait_for_timeout(5000)
                            continue
                        log("VERIFICATION CODE: " + value)
                        if not enter_code(page, value):
                            log("STOP: could not enter the verification code")
                            outcome = "could not enter the verification code"
                            break
                        page.wait_for_timeout(5000)
                        continue
                    if handle_name(page):
                        click_button(page, ["Continue", "Create account"])
                        page.wait_for_timeout(5000)
                        continue
                    log("final state unclear")
                    outcome = "final state unclear"
                    break
                else:
                    outcome = "final state unclear"
                if not retry:
                    break
                log("trying a fresh gmail.com address (attempt 2 of 2)")
            shot(page, "step4-verify.png")
            save_account(email, outcome)
        finally:
            try:
                browser.close()
            except Exception:
                pass


def main():
    log("=== muse-signup-desktop start ===")
    try:
        run_signup()
    except Exception as exc:
        log("UNCAUGHT EXC: %r" % (exc,))
    log("=== muse-signup-desktop end ===")


if __name__ == "__main__":
    main()
