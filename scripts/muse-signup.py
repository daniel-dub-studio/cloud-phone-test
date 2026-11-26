#!/usr/bin/env python3
"""Create a Muse account on muse.ai in the emulator's Chrome, using a temp.tf email.

Runs on the GitHub Actions runner with the emulator already booted. Drives the
emulator with adb via subprocess. Plain Python only -- no heredoc anywhere.

Outputs (all under /tmp, uploaded as the muse-signup artifact):
  screen.png, step1.png, step2.png, step3.png, step4-continue.png,
  step4-verify.png or step4-blocked.png, step5-inbox.png,
  muse-account.txt, dump-*.xml

The account is for Muse (the assistant), not for the repo owner: if any signup
step asks for a name, enter "Muse".

Fixes over the first version (from run #2):
- muse.ai renders in a WebView whose inner elements have no bounds in the
  uiautomator dump, so dump-based taps cannot focus the email field or press
  Continue. The form is driven with fixed coordinates on a 1080x2340 screen:
  email field centre (287,1710), Continue button centre (120,1955).
- temp.tf pre-checks every email provider, so the address came out as
  hotmail.com/outlook.com. A gmail.com address is obtained with the
  NEW ADDRESS loop described in the run #4 fix below.
- A password is only typed when a fresh screen actually shows a real password
  input field (EditText with usable bounds). The word "password" merely
  appearing in page text no longer triggers any typing.

Fixes from run #3 (kept):
- Wait for temp.tf to render before generating an address: re-dump in a loop
  until the dump has more than 100 nodes or "gmail.com" is present. Acting on
  the first 43-node dump is what broke run #3.
- The dump gives the NEW ADDRESS button no usable bounds, so it is tapped at
  fixed coordinates (322,1031) on a 1080x2340 screen.

Fixes from run #4 (kept):
- Drop the checkbox unchecking entirely: fixed-coordinate taps on the
  provider boxes are unreliable (run #4: only hotmail.com actually toggled;
  outlook.com and high.edu.pl stayed checked). All four providers stay
  checked, so NEW ADDRESS picks a random provider each time: tap it in a
  loop up to 15 times, re-dumping after each tap, until the generated
  address ends with @gmail.com.

Fixes in this version (from run #5):
- Raise the NEW ADDRESS loop cap from 15 to 25 attempts: the gmail.com
  address only appeared on attempt 15 last time, too close to the cap.
- The tap at (287,1710) never focused the email field in run #5 (step3.png
  shows the field empty, no keyboard, orange focus ring around the whole
  WebView), so the typed text went nowhere. Now the field is tapped in a
  focus loop of up to 5 attempts -- (287,1710), (200,1710), a 300ms
  long-press at (287,1710), then (287,1710) and (200,1710) again -- and
  the email is only typed after `adb shell dumpsys input_method`
  confirms the soft keyboard is visible. If the keyboard never appears,
  stop at step4-blocked instead of typing blindly.
- Never press Back blindly: after Enter, Back is pressed only when
  dumpsys still shows the keyboard up. In run #5 the keyboard was already
  down, so Back exited Chrome to the home screen.

The script never raises out of main: every failure is logged, a screenshot is
taken, and it exits 0 so the workflow still uploads the artifacts.
"""

import random
import re
import string
import subprocess
import time

TMP = "/tmp"
NL = chr(10)
EMAIL_RE = re.compile("[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}")
BOUNDS_RE = re.compile('bounds="[(]([0-9]+),([0-9]+)[)][(]([0-9]+),([0-9]+)[)]"')
CODE6_RE = re.compile("(?<![0-9])([0-9]{6})(?![0-9])")
CODE_ANY_RE = re.compile("(?<![0-9])([0-9]{4,8})(?![0-9])")
CHECKED_RE = re.compile('checked="(true|false)"')
SUBMITTED_WORDS = ("verification code", "enter the code", "check your email",
                   "check your inbox", "we sent", "sent you a code",
                   "verify your phone", "phone verification", "enter your phone",
                   "sms code", "texted you", "captcha", "not a robot",
                   "verify you are human", "welcome", "account created",
                   "get started", "your account is ready", "dashboard")


def log(msg):
    print("[muse-signup] " + str(msg), flush=True)


def run(cmd, timeout=120):
    log("RUN: " + " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.stdout.strip():
            log("OUT: " + p.stdout.strip()[:1500])
        if p.stderr.strip():
            log("ERR: " + p.stderr.strip()[:1500])
        log("RC: %d" % p.returncode)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:
        log("EXC: %r" % (exc,))
        return 1, "", str(exc)


def adb(*args, timeout=120):
    return run(["adb"] + [str(a) for a in args], timeout=timeout)


def adb_shell(*args, timeout=120):
    return run(["adb", "shell"] + [str(a) for a in args], timeout=timeout)


def sleep(seconds):
    log("sleep %d" % seconds)
    time.sleep(seconds)


def screencap(name):
    path = TMP + "/" + name
    log("screencap -> " + path)
    try:
        with open(path, "wb") as fh:
            p = subprocess.run(["adb", "exec-out", "screencap", "-p"],
                               stdout=fh, stderr=subprocess.PIPE, timeout=120)
        log("screencap rc=%d" % p.returncode)
    except Exception as exc:
        log("screencap EXC: %r" % (exc,))


def open_url(url):
    log("open URL: " + url)
    adb_shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url)


def tap(x, y):
    log("TAP (%d,%d)" % (x, y))
    adb_shell("input", "tap", str(x), str(y))


def node_centre(line):
    m = BOUNDS_RE.search(line)
    if not m:
        return None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def tap_line(line):
    centre = node_centre(line)
    if centre is None:
        log("tap_line: no usable bounds: " + line[:300])
        return False
    tap(centre[0], centre[1])
    return True


def node_checked(line):
    m = CHECKED_RE.search(line)
    if not m:
        return None
    return m.group(1) == "true"


def keyboard_visible():
    """True when the soft keyboard is up, judged from dumpsys input_method.

    The input method window is listed with a shown/visible state; when the
    keyboard is down the window is hidden. The matching lines are logged
    so the run log shows what was seen.
    """
    rc, out, err = adb_shell("dumpsys", "input_method")
    blob = out + err
    low = blob.lower()
    for ln in blob.splitlines():
        if "inputmethodwindow" in ln.lower():
            log("IME: " + ln.strip()[:200])
    if "minputmethodwindow" not in low:
        log("keyboard check: no input method window in dumpsys")
        return False
    visible = ("mshown=true" in low or "isvisible=true" in low
               or "mvisible=true" in low or "mhasSurface=true" in low)
    log("keyboard check: visible=%s" % visible)
    return visible


def focus_email_field():
    """Tap the muse.ai email field until the soft keyboard is visible.

    Returns True once the keyboard is confirmed up. The tap at (287,1710)
    never focused the input in run #5, so alternate the taps: (287,1710),
    (200,1710), a 300ms long-press at (287,1710), then (287,1710) and
    (200,1710) again. Returns False after 5 attempts without a keyboard.
    """
    attempts = [(287, 1710), (200, 1710), "longpress", (287, 1710), (200, 1710)]
    for i, action in enumerate(attempts, start=1):
        if action == "longpress":
            log("FOCUS attempt %d: long-press (287,1710) 300ms" % i)
            adb_shell("input", "swipe", "287", "1710", "287", "1710", "300")
        else:
            log("FOCUS attempt %d" % i)
            tap(action[0], action[1])
        sleep(2)
        if keyboard_visible():
            log("keyboard visible after focus attempt %d" % i)
            return True
        log("keyboard not visible after focus attempt %d" % i)
    return False


def dump(name):
    """Dump the screen; the raw dump is one line, so split it first.

    Saves /tmp/<name>.xml and /tmp/<name>-lines.xml. Returns node lines.
    """
    xml = TMP + "/" + name + ".xml"
    lines_path = TMP + "/" + name + "-lines.xml"
    adb_shell("uiautomator", "dump", "/sdcard/window_dump.xml")
    adb("pull", "/sdcard/window_dump.xml", xml)
    try:
        with open(xml, "r", errors="replace") as fh:
            raw = fh.read()
    except Exception as exc:
        log("dump read EXC: %r" % (exc,))
        return []
    split = raw.replace("><", ">" + NL + "<")
    try:
        with open(lines_path, "w") as fh:
            fh.write(split)
    except Exception as exc:
        log("dump write EXC: %r" % (exc,))
    lines = [ln for ln in split.splitlines() if ln.strip()]
    log("dump %s: %d nodes" % (name, len(lines)))
    for ln in lines:
        if 'text=""' not in ln:
            log("NODE: " + ln[:400])
    return lines


def find_lines(lines, *needles, clickable=None):
    out = []
    for ln in lines:
        low = ln.lower()
        if all(n.lower() in low for n in needles):
            if clickable is True and 'clickable="true"' not in ln:
                continue
            out.append(ln)
    return out


def find_button(lines, label):
    exact = [ln for ln in lines if ('text="' + label + '"') in ln]
    if exact:
        return exact
    found = find_lines(lines, label, clickable=True)
    if found:
        return found
    return find_lines(lines, label)


def extract_email(lines, domain=None):
    if domain:
        for ln in lines:
            m = EMAIL_RE.search(ln)
            if m and m.group(0).lower().endswith("@" + domain.lower()):
                return m.group(0)
        return None
    for ln in lines:
        m = EMAIL_RE.search(ln)
        if m:
            return m.group(0)
    return None


def extract_code(lines):
    for ln in lines:
        if "code" in ln.lower():
            m = CODE6_RE.search(ln)
            if m:
                return m.group(1)
    for ln in lines:
        m = CODE6_RE.search(ln)
        if m:
            return m.group(1)
    squashed = "".join(NL.join(lines).split())
    m = CODE6_RE.search(squashed)
    if m:
        return m.group(1)
    for ln in lines:
        m = CODE_ANY_RE.search(ln)
        if m:
            return m.group(1)
    return None


def type_text(text):
    esc = text.replace("@", "%40").replace(" ", "%s")
    log("TYPE: " + text)
    adb_shell("input", "text", esc)


def save_account(email, password=None):
    with open(TMP + "/muse-account.txt", "w") as fh:
        fh.write("email: " + email + NL)
        fh.write("name: Muse" + NL)
        if password:
            fh.write("password: " + password + NL)
    log("saved " + TMP + "/muse-account.txt")


def random_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def provider_checked(lines, name):
    """Checked state of a temp.tf provider checkbox, read from the dump.
    True, False, or None when the provider is not in the dump."""
    nodes = find_lines(lines, name)
    boxes = [ln for ln in nodes if "CheckBox" in ln]
    target = boxes[0] if boxes else (nodes[0] if nodes else None)
    if target is None:
        return None
    return node_checked(target)


def still_on_form(lines):
    """True when the dump still shows the muse.ai email form and nothing
    that would mean the form was already submitted."""
    blob = NL.join(lines).lower()
    if "mobile number or email" not in blob:
        return False
    if any(w in blob for w in SUBMITTED_WORDS):
        return False
    return True


def fetch_code_from_inbox():
    """Open temp.tf, find the Muse verification email. Returns (code, mode).

    mode is "code", "link" (a verify link in the email was tapped) or None.
    """
    open_url("https://temp.tf")
    sleep(10)
    screencap("step5-inbox.png")
    lines = dump("dump-inbox")
    mail = find_lines(lines, "muse")
    for attempt in range(3):
        if mail:
            break
        log("no Muse email yet, waiting (attempt %d)" % (attempt + 1))
        sleep(15)
        lines = dump("dump-inbox-retry%d" % (attempt + 1))
        mail = find_lines(lines, "muse")
    if not mail:
        log("STOP: no Muse verification email arrived in the temp inbox")
        return None, None
    log("Muse email found, opening it")
    tap_line(mail[0])
    sleep(5)
    lines = dump("dump-inbox-open")
    link = find_lines(lines, "verif", clickable=True)
    if not link:
        link = find_lines(lines, "continue", clickable=True)
    if link:
        log("verify link/button found in the email, tapping it")
        tap_line(link[0])
        sleep(8)
        screencap("step4-verify.png")
        dump("dump-step4-after-link")
        return None, "link"
    code = extract_code(lines)
    if code:
        log("VERIFICATION CODE: " + code)
    else:
        log("no code readable in the opened email")
    return code, ("code" if code else None)


def enter_code_on_muse(email, code):
    """Return to muse.ai and enter the verification code. Returns dump lines."""
    open_url("https://muse.ai")
    sleep(10)
    lines = dump("dump-step4")
    if find_lines(lines, "mobile number or email"):
        log("muse.ai shows the email screen again, re-entering the email")
        if not focus_email_field():
            log("STOP: keyboard never appeared after tapping the email field")
            screencap("step4-blocked.png")
            return lines
        type_text(email)
        sleep(2)
        adb_shell("input", "keyevent", "66")  # Enter
        sleep(8)
        lines = dump("dump-step4b")
    code_field = find_lines(lines, "code")
    edit = [ln for ln in code_field if "EditText" in ln]
    target = edit[0] if edit else (code_field[0] if code_field else None)
    if target is None:
        edits = [ln for ln in lines if "EditText" in ln]
        target = edits[0] if edits else None
    if target is None:
        log("STOP: no code field found on muse.ai")
        screencap("step4-blocked.png")
        return lines
    if not tap_line(target):
        log("STOP: code field has no usable bounds, cannot enter the code")
        screencap("step4-blocked.png")
        return lines
    sleep(2)
    type_text(code)
    sleep(2)
    verify = find_button(lines, "Verify") or find_button(lines, "Continue")
    if verify:
        tap_line(verify[0])
    else:
        adb_shell("input", "keyevent", "66")
    sleep(8)
    screencap("step4-verify.png")
    return dump("dump-step4-after")


def handle_profile_setup(lines, email):
    """Fill a name field with Muse and/or a generated password, but only
    when the field is a real, tappable input (EditText with usable bounds).
    Never type into a field the script could not focus."""
    acted = False
    pw_edit = [ln for ln in find_lines(lines, "password") if "EditText" in ln]
    pw_target = None
    for ln in pw_edit:
        if node_centre(ln) is not None:
            pw_target = ln
            break
    if pw_target is not None:
        pw = random_password()
        log("password field found, filling a generated password")
        if tap_line(pw_target):
            sleep(2)
            type_text(pw)
            save_account(email, pw)
            acted = True
            sleep(2)
    elif pw_edit:
        log("password field visible but has no usable bounds, cannot fill it")
    name_edit = [ln for ln in find_lines(lines, "name") if "EditText" in ln]
    name_target = None
    for ln in name_edit:
        if node_centre(ln) is not None:
            name_target = ln
            break
    if name_target is not None:
        log("name field found, entering Muse")
        if tap_line(name_target):
            sleep(2)
            type_text("Muse")
            acted = True
            sleep(2)
    elif name_edit:
        log("name field visible but has no usable bounds, cannot fill it")
    if acted:
        cont = find_button(lines, "Continue") or find_button(lines, "Create account")
        if cont:
            tap_line(cont[0])
        sleep(8)
        screencap("step4-verify.png")
        return dump("dump-step4-profile")
    return lines


def finish(lines, email):
    blob = NL.join(lines).lower()
    if any(w in blob for w in ("verify your phone", "phone verification",
                               "enter your phone", "sms code")):
        log("STOP: phone verification requested")
        screencap("step4-blocked.png")
        return
    if any(w in blob for w in ("captcha", "not a robot", "verify you are human")):
        log("STOP: captcha screen, not solving it")
        screencap("step4-blocked.png")
        return
    if any(w in blob for w in ("welcome", "account created", "get started",
                               "your account is ready", "dashboard")):
        log("ACCOUNT CREATED (apparent): muse.ai shows a success/welcome screen")
        screencap("step4-verify.png")
        return
    # Only handle a password/name step when a fresh screen actually shows a
    # real input field for it. The word alone in page text is not a field.
    screencap("step4-profile.png")
    fresh = dump("dump-step4-profile")
    name_edit = [ln for ln in find_lines(fresh, "name") if "EditText" in ln]
    pw_edit = [ln for ln in find_lines(fresh, "password") if "EditText" in ln]
    if name_edit or pw_edit:
        lines = handle_profile_setup(fresh, email)
        blob = NL.join(lines).lower()
        if any(w in blob for w in ("welcome", "account created", "get started",
                                   "your account is ready", "dashboard")):
            log("ACCOUNT CREATED (apparent) after profile setup")
        else:
            log("profile step done, final state unclear - see step4-verify.png")
        return
    log("final state unclear - see step4-verify.png and the dumps")
    screencap("step4-verify.png")


def run_signup():
    # (a) temp.tf in Chrome, dismiss Chrome first-run
    open_url("https://temp.tf")
    sleep(12)
    tap(540, 2114)  # Accept & continue
    sleep(5)
    tap(168, 2098)  # No thanks on "Turn on sync?"
    sleep(5)
    screencap("step1.png")
    lines = dump("dump-step1")

    # (b) temp email, gmail.com only. First wait for temp.tf to render: the
    # first dump is taken before the page loads (43 nodes) and has no
    # providers in it. Re-dump until the dump has more than 100 nodes or
    # "gmail.com" is present.
    for attempt in range(6):
        if len(lines) > 100 or any("gmail.com" in ln for ln in lines):
            break
        log("temp.tf not rendered yet (only %d nodes), waiting (attempt %d)" % (len(lines), attempt + 1))
        sleep(5)
        lines = dump("dump-step1-wait%d" % (attempt + 1))
    # No checkbox unchecking: fixed-coordinate taps on the provider boxes
    # are unreliable (run #4: only hotmail.com actually toggled). All four
    # providers stay checked, so NEW ADDRESS picks a random one each time.
    # Tap NEW ADDRESS up to 25 times, re-dumping after each tap, until the
    # address ends with @gmail.com.
    email = None
    for attempt in range(25):
        tap(322, 1031)  # NEW ADDRESS
        sleep(3)
        lines = dump("dump-step1-try%d" % (attempt + 1))
        email = extract_email(lines, domain="gmail.com")
        if email:
            log("gmail.com address found on NEW ADDRESS attempt %d" % (attempt + 1))
            break
        log("attempt %d: no gmail.com address yet" % (attempt + 1))
    if not email:
        log("STOP: no gmail.com temp email found on temp.tf")
        screencap("step4-blocked.png")
        return
    log("TEMP EMAIL: " + email)
    save_account(email)

    # (c) muse.ai signup: fixed-coordinate form interaction. The page is a
    # WebView whose inner elements have no bounds in the dump, so dump-based
    # taps cannot focus the field or press Continue. Coordinates are the field
    # and button centres on a 1080x2340 screen, read from step2.png of run #2.
    # The field is only typed into once the soft keyboard is confirmed
    # visible (focus_email_field); in run #5 the tap never focused the
    # input and the typed text went nowhere.
    open_url("https://muse.ai")
    sleep(15)
    screencap("step2.png")
    lines = dump("dump-step2")
    if not focus_email_field():
        log("STOP: keyboard never appeared after tapping the email field")
        screencap("step4-blocked.png")
        return
    type_text(email)
    sleep(2)
    adb_shell("input", "keyevent", "66")  # Enter: submit the form
    sleep(3)
    screencap("step3.png")
    lines = dump("dump-step3")
    if still_on_form(lines):
        log("still on the email form after Enter")
        if keyboard_visible():
            log("keyboard still visible, pressing Back to hide it")
            adb_shell("input", "keyevent", "4")  # Back: hide the keyboard
            sleep(2)
        else:
            log("keyboard already down, skipping Back (Back would exit Chrome)")
        tap(120, 1955)  # Continue button centre
        sleep(8)
        screencap("step4-continue.png")
        lines = dump("dump-step4")
    blob = NL.join(lines).lower()

    # (d) what happened after submitting?
    if any(w in blob for w in ("verify your phone", "phone verification",
                               "enter your phone", "sms code", "texted you")):
        log("STOP: phone verification requested")
        screencap("step4-blocked.png")
        return
    if any(w in blob for w in ("captcha", "not a robot", "verify you are human")):
        log("STOP: captcha screen, not solving it")
        screencap("step4-blocked.png")
        return
    if any(w in blob for w in ("disposable", "not accepted", "try a different email",
                               "invalid email", "cannot use this email")):
        log("STOP: temp email rejected by muse.ai")
        screencap("step4-blocked.png")
        return
    if any(w in blob for w in ("verification code", "enter the code", "check your email",
                               "check your inbox", "we sent", "sent you a code")):
        log("verification requested, fetching the code from the temp inbox")
        code, mode = fetch_code_from_inbox()
        if mode == "link":
            log("verification link opened, checking the result")
            lines = dump("dump-step4-after-link")
            finish(lines, email)
            return
        if not code:
            log("STOP: no verification code arrived")
            screencap("step4-blocked.png")
            return
        lines = enter_code_on_muse(email, code)
        finish(lines, email)
        return
    log("no verification screen detected after submitting, checking the result")
    finish(lines, email)


def main():
    log("=== muse-signup start ===")
    try:
        run_signup()
    except Exception as exc:
        log("UNCAUGHT EXC: %r" % (exc,))
        try:
            screencap("step4-blocked.png")
        except Exception:
            pass
    log("=== muse-signup end ===")


if __name__ == "__main__":
    main()
