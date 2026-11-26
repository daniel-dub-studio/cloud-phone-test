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
     then click Confirm. Then if a name is asked, enter "Muse".
  4. STOP (screenshot step4-verify.png, log the reason, do not proceed) if
     a phone number, a payment card, a captcha/Turnstile, or a rejected
     email is asked for.

Run #6 change: handle the birthday step run #5 reached (it stopped on an
unrecognised screen, which was the birthday form).

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
    card, captcha, rejected, birthday, success, name, code-ok, other."""
    low = body_text(page).lower()
    if any(w in low for w in PHONE_WORDS):
        return "phone"
    if any(w in low for w in CARD_WORDS):
        return "card"
    if any(w in low for w in CAPTCHA_WORDS):
        return "captcha"
    if any(w in low for w in REJECTED_WORDS):
        return "rejected"
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
    """Wait up to timeout_s for the form to give way to the next step, then
    classify the new screen. Returns (state, waited_seconds)."""
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


def enter_code(page, code):
    """Enter the code WITHOUT clicking the hidden field first. Tries, in
    order: force fill, plain fill, JavaScript value-set with native
    input/change events, typing into the visible OTP container. Verifies
    via input_value after each attempt. Returns True when the value stuck.
    The code digits are never logged."""
    for frame in page.frames:
        try:
            inp = frame.locator(
                "input[aria-label*='security code' i], "
                "input[autocomplete='one-time-code']").first
            if not inp.count():
                continue
            for method in ("fill", "force-fill", "js-set"):
                try:
                    if method == "fill":
                        inp.fill(code, timeout=5000)
                    elif method == "force-fill":
                        inp.fill(code, force=True, timeout=5000)
                    else:
                        handle = inp.element_handle(timeout=5000)
                        frame.evaluate(
                            "(el, val) => {"
                            " const setter = Object.getOwnPropertyDescriptor("
                            " window.HTMLInputElement.prototype, 'value').set;"
                            " setter.call(el, val);"
                            " el.dispatchEvent(new Event('input', {bubbles: true}));"
                            " el.dispatchEvent(new Event('change', {bubbles: true}));"
                            " }", handle, code)
                    val = inp.input_value() or ""
                    if val == code:
                        log("code field filled via %s" % method)
                        return True
                    log("code field filled via %s but the value did not stick" % method)
                except Exception as exc:
                    log("code fill %s EXC: %r" % (method, exc,))
            try:
                inp.click(timeout=5000, force=True)
                inp.type(code, delay=100)
                val = inp.input_value() or ""
                if val == code:
                    log("code typed into the OTP container")
                    return True
                log("code typed into the OTP container but the value did not stick")
            except Exception as exc:
                log("code type EXC: %r" % (exc,))
        except Exception:
            continue
    log("could not fill the code field by any method")
    return False


def poll_gist_code(url, tried, deadline):
    """Poll the gist raw URL every 30 s until its content is a 6-digit code
    not already tried, or the deadline passes. Returns the code, or None.
    The word 'waiting' means the code has not been put in yet. A cache
    buster is added to each request so a cached 'waiting' is never served.
    The code digits are never logged."""
    check = 0
    while time.time() < deadline:
        check += 1
        cache_url = url + ("&" if "?" in url else "?") + "t=%d" % int(time.time())
        try:
            req = urllib.request.Request(
                cache_url, headers={"User-Agent": "muse-signup-desktop"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read().decode("utf-8", "replace").strip()
        except Exception as exc:
            log("gist poll %d EXC: %r" % (check, exc))
            content = ""
        if content.lower() == "waiting":
            log("gist poll %d: still 'waiting'" % check)
        elif re.fullmatch(r"[0-9]{6}", content or ""):
            if content in tried:
                log("gist poll %d: code already tried, waiting for a new one" % check)
            else:
                log("gist poll %d: code received" % check)
                return content
        elif content:
            log("gist poll %d: unexpected content %r, waiting" % (check, content[:40]))
        else:
            log("gist poll %d: no content, waiting" % check)
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(GIST_POLL_SECONDS, remaining))
    log("gist poll deadline reached (25 minutes) with no new code")
    return None


def save_account(email, outcome):
    with open(TMP + "/muse-account.txt", "w") as f:
        f.write("email: " + email + "\n")
        f.write("outcome: " + outcome + "\n")
    log("account file written")


def run_signup(muse_page):
    """One signup attempt with Daniel's real email; the code comes from
    the gist. Returns the outcome string."""
    email = os.environ.get("MUSE_EMAIL", "").strip()
    gist_url = os.environ.get("CODE_GIST_URL", "").strip()
    if not email:
        return "STOP: MUSE_EMAIL env var is not set"
    if not gist_url:
        return "STOP: CODE_GIST_URL env var is not set"
    log("MUSE_EMAIL: " + email)
    log("CODE_GIST_URL: " + gist_url)
    try:
        muse_page.goto("https://muse.ai/", wait_until="domcontentloaded",
                        timeout=60000)
        muse_page.wait_for_timeout(5000)
    except Exception as exc:
        log("muse.ai load EXC: %r" % (exc,))
        shot(muse_page, "step2-form.png")
        return "STOP: muse.ai did not load"
    log("muse.ai title: " + muse_page.title())
    log("muse.ai url: " + muse_page.url)
    if not fill_email(muse_page, email):
        shot(muse_page, "step2-form.png")
        return "STOP: email field not found on muse.ai"
    log("email field filled")
    shot(muse_page, "step2-form.png")
    if not click_button(muse_page, ("Continue", "Next")):
        shot(muse_page, "step3-after-continue.png")
        return "STOP: Continue button not found on muse.ai"
    log("Continue clicked")
    muse_page.wait_for_timeout(2000)
    shot(muse_page, "step3-after-continue.png")
    state, waited = wait_next_step(muse_page)
    log("next step after %ds: %s" % (waited, state))
    shot(muse_page, "step4-verify.png")
    tried = set()
    deadline = None
    while True:
        log("state=%s" % state)
        if state == "phone":
            return "STOP: phone number asked for"
        if state == "card":
            return "STOP: payment card asked for"
        if state == "captcha":
            return "STOP: captcha/Turnstile shown"
        if state == "rejected":
            return "STOP: email rejected"
        if state in ("success", "code-ok"):
            save_account(email, "ACCOUNT CREATED (apparent)")
            return "ACCOUNT CREATED (apparent)"
        if state == "name":
            if handle_name(muse_page):
                click_button(muse_page, ("Continue", "Next", "Save"))
                muse_page.wait_for_timeout(3000)
                state, _ = wait_next_step(muse_page)
                shot(muse_page, "step4-verify.png")
                continue
            return "STOP: name step could not be completed"
        if state == "birthday":
            if handle_birthday(muse_page):
                muse_page.wait_for_timeout(3000)
                state, _ = wait_next_step(muse_page)
                shot(muse_page, "step4-verify.png")
                continue
            return "STOP: birthday step could not be completed"
        if state == "code":
            if deadline is None:
                deadline = time.time() + GIST_WAIT_SECONDS
                log("polling the gist for the code for up to 25 minutes")
            code = poll_gist_code(gist_url, tried, deadline)
            if not code:
                return "STOP: no code in the gist after 25 minutes"
            if not enter_code(muse_page, code):
                shot(muse_page, "step4-verify.png")
                return "STOP: could not enter the verification code"
            log("code entered, pressing Enter")
            try:
                muse_page.keyboard.press("Enter")
            except Exception as exc:
                log("Enter press EXC: %r" % (exc,))
            muse_page.wait_for_timeout(3000)
            state, _ = wait_next_step(muse_page)
            shot(muse_page, "step4-verify.png")
            if state == "code":
                if click_button(muse_page, ("Verify", "Continue", "Next", "Submit")):
                    muse_page.wait_for_timeout(3000)
                    state, _ = wait_next_step(muse_page)
                    shot(muse_page, "step4-verify.png")
            if state == "code":
                log("code was not accepted, waiting for a new code in the gist")
                tried.add(code)
                continue
            continue
        log("unrecognised state %r, stopping" % state)
        return "STOP: unrecognised state after Continue: " + state


def main():
    log("=== muse-signup-desktop start ===")
    outcome = "STOP: playwright did not start"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            muse_page = context.new_page()
            try:
                outcome = run_signup(muse_page)
            except Exception as exc:
                log("run EXC: %r" % (exc,))
                outcome = "STOP: script error: %r" % (exc,)
                try:
                    shot(muse_page, "step4-verify.png")
                except Exception:
                    pass
            browser.close()
    except Exception as exc:
        log("playwright EXC: %r" % (exc,))
        outcome = "STOP: playwright error: %r" % (exc,)
    log("OUTCOME: " + outcome)
    log("=== muse-signup-desktop end ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
