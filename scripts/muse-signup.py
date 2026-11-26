#!/usr/bin/env python3
"""Create a Muse account on muse.ai in the emulator's Chrome, using a temp.tf email.

Runs on the GitHub Actions runner with the emulator already booted. Drives the
emulator with adb via subprocess. Plain Python only -- no heredoc anywhere.

Outputs (all under /tmp, uploaded as the muse-signup artifact):
  screen.png, step1.png, step2.png, step3.png, step4-verify.png or
  step4-blocked.png, step5-inbox.png, muse-account.txt, dump-*.xml

The account is for Muse (the assistant), not for the repo owner: if any signup
step asks for a name, enter "Muse".

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


def extract_email(lines):
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
        field = find_lines(lines, "mobile number or email")
        edit = [ln for ln in field if "EditText" in ln]
        target = edit[0] if edit else field[0]
        tap_line(target)
        sleep(2)
        type_text(email)
        sleep(2)
        cont = find_button(lines, "Continue")
        if cont:
            tap_line(cont[0])
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
    tap_line(target)
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
    """Fill a name field with Muse and/or a generated password if asked."""
    acted = False
    pw_fields = find_lines(lines, "password")
    pw_edit = [ln for ln in pw_fields if "EditText" in ln]
    if pw_edit:
        pw = random_password()
        log("password field found, filling a generated password")
        tap_line(pw_edit[0])
        sleep(2)
        type_text(pw)
        save_account(email, pw)
        acted = True
        sleep(2)
    name_fields = find_lines(lines, "name")
    name_edit = [ln for ln in name_fields if "EditText" in ln]
    if name_edit:
        log("name field found, entering Muse")
        tap_line(name_edit[0])
        sleep(2)
        type_text("Muse")
        acted = True
        sleep(2)
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
    name_edit = [ln for ln in find_lines(lines, "name") if "EditText" in ln]
    pw_edit = [ln for ln in find_lines(lines, "password") if "EditText" in ln]
    if name_edit or pw_edit:
        lines = handle_profile_setup(lines, email)
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

    # (b) read the temp email (pick the gmail.com provider first if offered)
    email = extract_email(lines)
    if not email:
        provider = find_lines(lines, "gmail.com", clickable=True)
        if not provider:
            provider = find_lines(lines, "gmail.com")
        if provider:
            log("provider choice offered, tapping gmail.com")
            tap_line(provider[0])
            sleep(5)
            lines = dump("dump-step1b")
            email = extract_email(lines)
    for attempt in range(3):
        if email:
            break
        log("email not found yet, retry %d" % (attempt + 1))
        sleep(8)
        lines = dump("dump-step1-retry%d" % (attempt + 1))
        email = extract_email(lines)
    if not email:
        log("STOP: no temp email found on temp.tf")
        screencap("step4-blocked.png")
        return
    log("TEMP EMAIL: " + email)
    save_account(email)

    # (c) muse.ai signup: enter the email, tap Continue
    open_url("https://muse.ai")
    sleep(15)
    screencap("step2.png")
    lines = dump("dump-step2")
    field = find_lines(lines, "mobile number or email")
    if not field:
        field = find_lines(lines, "email")
    edit = [ln for ln in field if "EditText" in ln]
    target = edit[0] if edit else (field[0] if field else None)
    if target is None:
        log("STOP: could not find the email field on muse.ai")
        screencap("step4-blocked.png")
        return
    tap_line(target)
    sleep(2)
    type_text(email)
    sleep(2)
    cont = find_button(lines, "Continue")
    if cont:
        tap_line(cont[0])
    else:
        log("no Continue button found, pressing Enter")
        adb_shell("input", "keyevent", "66")
    sleep(8)
    screencap("step3.png")
    lines = dump("dump-step3")
    blob = NL.join(lines).lower()

    # (d) what happened after Continue?
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
    log("no verification screen detected after Continue, checking the result")
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
