#!/usr/bin/env python3
"""Create a Muse account on muse.ai in desktop Chrome via Playwright.

Runs on a GitHub Actions runner (ubuntu-latest) under xvfb-run, headed
Chromium -- no emulator, no adb, no screen coordinates. All interaction is
through DOM selectors.

This run uses Daniel's REAL email (MUSE_EMAIL) and gets the verification
code from a secret gist (CODE_GIST_URL): the gist file holds the word
"waiting" until Daniel sends the code in chat, when it is replaced by the
6-digit code. The script polls the gist raw URL every 30 s for up to
25 minutes. No temp mail is used.

Flow:
  1. muse.ai: fill "Mobile number or email" with MUSE_EMAIL, click
     Continue. Screenshots step2-form.png (before) and
     step3-after-continue.png.
  2. Wait up to 60 s for the next step. If a verification code is asked
     for, poll CODE_GIST_URL every 30 s for up to 25 minutes. When the
     gist holds a 6-digit code, enter it without clicking the hidden field
     (plain fill, then force fill, then JavaScript value-set with native
     input/change events), press Enter, and wait. If a name is asked for,
     enter "Muse". If the code is not accepted, keep polling for a new
     code until the deadline.
  3. After the code is accepted, the page asks "What's your birthday?"
     with Month / Day / Year dropdowns and a Confirm button: set
     Month=April, Day=12, Year=2005 (select_option by visible label),
     then click Confirm. Then WAIT up to 90 s for the page to leave the
     birthday state -- a "Creating account..." button means the account
     is still being created, so keep waiting; do NOT re-run the birthday
     handler. Then if a name is asked, enter "Muse".
  4. STOP (screenshot step4-verify.png, log the reason, do not proceed) if
     a phone number, a payment card, a captcha/Turnstile, or a rejected
     email is asked for -- or if the page says the account already exists
     or asks for a password / sign-in instead of a code (run #6's account
     may already have been created server-side).

Run #7 change: after Confirm on the birthday step, wait up to 90 s for the
page to leave the birthday state instead of re-running the handler (run #6
bailed out mid-transition when Confirm became a "Creating account..."
loading button). Also stop if the account already exists or a password /
sign-in is asked for instead of a code.

Outputs (all under /tmp, uploaded as the muse-signup-desktop artifact):
  muse-account.txt, step2-form.png, step3-after-continue.png,
  step4-verify.png

The account is for Muse (the assistant): the name entered is "Muse".
The code itself is never logged or saved -- only that one was received.

The script never raises out of main: every failure is logged, a screenshot
is taken, and it exits 0 so the workflow still uploads the artifacts.
"""

import os
import re
import time
import urllib.error
import urllib.request
from playwright.sync_api import sync_playwright

TMP = "/tmp"
GIST_WAIT_SECONDS = 25 * 60
GIST_POLL_SECONDS = 30
PHONE_WORDS = ("phone verification", "verify your phone", "enter your phone",
                "sms code", "texted you", "phone number")
CARD_WORDS = ("payment", "card number", "credit card", "debit card",
              "add a card")
CAPTCHA_WORDS = ("captcha", "turnstile", "verify you are human",
                 "not a robot")
REJECTED_WORDS = ("disposable", "not accepted", "try a different email",
                  "invalid email", "cannot use this email")
EXISTS_WORDS = ("already exists", "already registered",
                "already have an account", "email already in use",
                "account already exists")
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


def click_button(page, names):
    """Click the first visible, enabled button whose label matches one of
    the given names (exact first, then substring). Returns True if one was
    clicked."""
    for name in names:
        try:
            btn = page.get_by_role("button", name=name, exact=True).first
            if btn.count():
                btn.click(timeout=10000)
                log("clicked button %r" % name)
                return True
        except Exception:
            continue
    for name in names:
        try:
            btn = page.get_by_role("button", name=name, exact=False).first
            if btn.count():
                btn.click(timeout=10000)
                log("clicked button (substring) %r" % name)
                return True
        except Exception:
            continue
    return False


def fill_email(muse_page, email):
    """Fill the 'Mobile number or email' field on muse.ai. Tries the
    aria label first, then other selectors. Returns True when filled."""
    loc = locate_in_frames(
        muse_page,
        lambda f: f.locator(
            "input[aria-label*='Mobile number or email' i], "
            "input[aria-label*='email' i], "
            "input[placeholder*='email' i], "
            "input[type='email'], "
            "input[type='tel'], "
            "input[inputmode='email']"))
    if loc is None:
        return False
    for frame in muse_page.frames:
        try:
            loc.fill(email, timeout=10000)
            return True
        except Exception:
            continue
    try:
        loc.fill(email, force=True, timeout=10000)
        return True
    except Exception as exc:
        log("fill email EXC: %r" % (exc,))
        return False


def handle_name(page):
    """If a name is asked for, enter 'Muse'. Returns True when a name
    field was filled."""
    loc = locate_in_frames(
        page,
        lambda f: f.locator(
            "input[aria-label*='name' i], "
            "input[placeholder*='name' i], "
            "input[name*='name' i]"))
    if loc is None:
        return False
    try:
        loc.fill("Muse", timeout=10000)
        log("name field filled with 'Muse'")
        return True
    except Exception:
        return False


def find_select(page, word):
    """The <select> whose aria-label mentions word, or None."""
    return locate_in_frames(
        page,
        lambda f: f.locator("select[aria-label*='%s' i]" % word))


def handle_birthday(page):
    """Fill the 'What's your birthday?' step: Month=April, Day=12,
    Year=2005, then click Confirm. Uses select_option on the native
    dropdowns (by visible label). Returns True when the birthday was set
    and Confirm was clicked."""
    month = find_select(page, "month")
    day = find_select(page, "day")
    year = find_select(page, "year")
    if month is None or day is None or year is None:
        all_sel = locate_in_frames(page, lambda f: f.locator("select"))
        if all_sel is None or all_sel.count() < 3:
            log("birthday dropdowns not found")
            return False
        log("birthday dropdowns found by order (Month, Day, Year)")
        month, day, year = all_sel.nth(0), all_sel.nth(1), all_sel.nth(2)
    try:
        month.select_option(label="April", timeout=10000)
        day.select_option(label="12", timeout=10000)
        year.select_option(label="2005", timeout=10000)
    except Exception as exc:
        log("birthday select EXC: %r" % (exc,))
        return False
    log("birthday set to April 12, 2005")
    if click_button(page, ("Confirm", "Continue", "Next")):
        return True
    log("Confirm button not found on the birthday step")
    return False


def classify(page):
    """Classify the current muse.ai screen. Returns one of: code, phone,
    card, captcha, rejected, exists, password, birthday, success, name,
    code-ok, other."""
    low = body_text(page).lower()
    if any(w in low for w in PHONE_WORDS):
        return "phone"
    if any(w in low for w in CARD_WORDS):
        return "card"
    if any(w in low for w in CAPTCHA_WORDS):
        return "captcha"
    if any(w in low for w in REJECTED_WORDS):
        return "rejected"
    if any(w in low for w in EXISTS_WORDS):
        return "exists"
    pw = locate_in_frames(page, lambda f: f.locator("input[type='password']"))
    if pw is not None:
        try:
            if pw.first.is_visible():
                return "password"
        except Exception:
            pass
    if "welcome back" in low or "enter your password" in low:
        return "password"
    if "birthday" in low:
        return "birthday"
    loc = locate_in_frames(
        page,
        lambda f: f.locator("input[aria-label*='security code' i], "
                            "input[autocomplete='one-time-code']"))
    if loc is not None:
        return "code"
    if any(w in low for w in CODE_WORDS):
        return "code"
    if any(w in low for w in SUCCESS_WORDS):
        return "success"
    if ("name" in low and "enter" in low) or "what should we call you" in low:
        return "name"
    if "code" in low and "verified" in low:
        return "code-ok"
    return "other"


def wait_next_step(page, timeout_s=60):
    """Wait for the page to reach a recognisable next step. Returns the
    state, or 'other' on timeout."""
    waited = 0
    while waited < timeout_s:
        state = classify(page)
        if state != "other":
            return state, waited
        page.wait_for_timeout(2000)
        waited += 2
    state = classify(page)
    log("wait_next_step timed out, state=%s" % state)
    return state, waited


def page_has_text(page, needle):
    """True when any frame's body text contains needle (case-insensitive)."""
    needle = needle.lower()
    for frame in page.frames:
        try:
            if needle in (frame.inner_text("body") or "").lower():
                return True
        except Exception:
            continue
    return False


def wait_leave_birthday(page, timeout_s=90):
    """After Confirm on the birthday step, wait up to timeout_s for the
    page to leave the birthday state. A 'Creating account...' indicator
    means the account is still being created, so keep waiting. Transient
    'other' states are waited through. Returns (state, waited_seconds):
    a recognised non-birthday state, or 'birthday' if the page never left
    it within the timeout."""
    waited = 0
    while waited < timeout_s:
        state = classify(page)
        if state not in ("birthday", "other"):
            return state, waited
        if page_has_text(page, "creating account"):
            if waited % 15 == 0:
                log("birthday: 'Creating account...' visible, still working (%ds)" % waited)
        page.wait_for_timeout(5000)
        waited += 5
    state = classify(page)
    log("wait_leave_birthday timed out after %ds, state=%s" % (timeout_s, state))
    return state, waited


def enter_code(page, code):
    """Enter the 6-digit code into the hidden code field WITHOUT clicking
    it first (a div intercepts clicks). Plain fill, then force fill, then
    a JavaScript value-set with native input/change events. Returns True
    when the field's value reads back as the code."""
    loc = locate_in_frames(
        page,
        lambda f: f.locator("input[aria-label*='security code' i], "
                            "input[autocomplete='one-time-code']"))
    if loc is None:
        log("code field not found")
        return False
    for frame in page.frames:
        try:
            loc.fill(code, timeout=10000)
            if loc.input_value() == code:
                return True
        except Exception:
            continue
    try:
        loc.fill(code, force=True, timeout=10000)
        if loc.input_value() == code:
            return True
    except Exception:
        pass
    try:
        loc.evaluate(
            "(el, val) => { "
            "const setter = Object.getOwnPropertyDescriptor("
            "window.HTMLInputElement.prototype, 'value').set; "
            "setter.call(el, val); "
            "el.dispatchEvent(new Event('input', {bubbles: true})); "
            "el.dispatchEvent(new Event('change', {bubbles: true})); }",
            code)
        if loc.input_value() == code:
            return True
    except Exception as exc:
        log("code JS set EXC: %r" % (exc,))
    return False


def poll_gist_code(page, gist_url):
    """Poll the gist raw URL every 30 s for up to 25 minutes. Returns the
    6-digit code when the gist holds one, or None on timeout."""
    deadline = time.time() + GIST_WAIT_SECONDS
    n = 0
    while time.time() < deadline:
        n += 1
        try:
            req = urllib.request.Request(
                gist_url + "?t=%d" % int(time.time()))
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8", "replace").strip()
        except urllib.error.URLError as exc:
            log("gist poll %d: fetch failed: %r" % (n, exc))
            content = ""
        if re.fullmatch(r"\d{6}", content):
            log("gist poll %d: code received" % n)
            return content
        if n == 1 or n % 20 == 0:
            log("gist poll %d: still 'waiting' (content=%r)" % (n, content[:20]))
        time.sleep(GIST_POLL_SECONDS)
    log("gist poll: deadline reached after %d polls, no code" % n)
    return None


def save_account(email, outcome):
    with open(TMP + "/muse-account.txt", "w") as f:
        f.write("email=%s\n" % email)
        f.write("outcome=%s\n" % outcome)
    log("saved muse-account.txt: " + outcome)


def run_signup():
    """Run the signup. Returns the outcome string."""
    email = os.environ.get("MUSE_EMAIL")
    gist_url = os.environ.get("CODE_GIST_URL")
    if not email:
        return "STOP: MUSE_EMAIL env var not set"
    if not gist_url:
        return "STOP: CODE_GIST_URL env var not set"
    log("email=%s (from env)" % email)
    log("gist=%s" % gist_url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        log("opening muse.ai")
        page.goto("https://muse.ai", timeout=60000)
        page.wait_for_timeout(4000)
        shot(page, "step2-form.png")
        log("title=%r url=%s" % (page.title(), page.url))
        if not fill_email(page, email):
            shot(page, "step4-verify.png")
            return "STOP: could not find the email field on muse.ai"
        log("email filled")
        if not click_button(page, ("Continue",)):
            shot(page, "step4-verify.png")
            return "STOP: could not find the Continue button"
        shot(page, "step3-after-continue.png")
        state, waited = wait_next_step(page)
        log("after Continue: state=%s (waited %ds)" % (state, waited))
        attempts = 0
        while True:
            if state == "phone":
                shot(page, "step4-verify.png")
                return "STOP: phone verification asked for"
            if state == "card":
                shot(page, "step4-verify.png")
                return "STOP: payment card asked for"
            if state == "captcha":
                shot(page, "step4-verify.png")
                return "STOP: captcha / human verification asked for"
            if state == "rejected":
                shot(page, "step4-verify.png")
                return "STOP: email rejected by muse.ai"
            if state == "exists":
                shot(page, "step4-verify.png")
                return "STOP: account already exists for this email"
            if state == "password":
                shot(page, "step4-verify.png")
                return "STOP: password / sign-in asked for instead of a code"
            if state == "success":
                shot(page, "step4-verify.png")
                return "ACCOUNT CREATED (apparent): success state reached"
            if state == "name":
                if not handle_name(page):
                    shot(page, "step4-verify.png")
                    return "STOP: name step shown but no name field found"
                if not click_button(page, ("Continue", "Next", "Confirm")):
                    shot(page, "step4-verify.png")
                    return "STOP: name entered but no button to continue"
                page.wait_for_timeout(4000)
                state, _ = wait_next_step(page)
                shot(page, "step4-verify.png")
                continue
            if state == "birthday":
                if not handle_birthday(page):
                    shot(page, "step4-verify.png")
                    return "STOP: birthday step could not be completed"
                state, waited = wait_leave_birthday(page)
                shot(page, "step4-verify.png")
                log("birthday step left after %ds, state=%s" % (waited, state))
                if state == "birthday":
                    return "STOP: birthday step did not complete after 90s"
                if state == "other":
                    return "STOP: unrecognised state after birthday Confirm: other"
                continue
            if state == "code":
                attempts += 1
                log("verification code step reached (round %d)" % attempts)
                code = poll_gist_code(page, gist_url)
                if code is None:
                    shot(page, "step4-verify.png")
                    return "STOP: no verification code in the gist after 25 minutes"
                if not enter_code(page, code):
                    shot(page, "step4-verify.png")
                    return "STOP: could not enter the verification code"
                log("code entered")
                page.keyboard.press("Enter")
                state, waited = wait_next_step(page)
                log("after code: state=%s (waited %ds)" % (state, waited))
                shot(page, "step4-verify.png")
                continue
            shot(page, "step4-verify.png")
            return "STOP: unrecognised state after Continue: " + state


def main():
    try:
        outcome = run_signup()
    except Exception as exc:
        log("EXC: %r" % (exc,))
        outcome = "STOP: unexpected error: %r" % (exc,)
    log(outcome)
    email = os.environ.get("MUSE_EMAIL", "")
    try:
        save_account(email, outcome)
    except Exception as exc:
        log("save account EXC: %r" % (exc,))


if __name__ == "__main__":
    main()
