from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import ContextLostError, PageDisconnectedError
import argparse
import json
import shutil
import tempfile
import datetime
import logging
import time
import os
import queue
import secrets
import sys
import threading
from typing import Any, Callable

from email_register import get_email_and_token, get_oai_code
from sink_client import push_tokens


def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"run_{ts}.log")

    logger = logging.getLogger("grok_register")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("鏃ュ織鏂囦欢: %s", log_path)
    return logger


run_logger: logging.Logger = None



def ensure_stable_python_runtime():
    # 浼樺厛鑷姩鍒囧埌鏇寸ǔ瀹氱殑 3.12 / 3.13锛岄伩鍏?3.14 涓?Mail.tm 鍋跺彂 TLS/鍏煎闂銆?
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] 妫€娴嬪埌 Python {sys.version.split()[0]}锛岃嚜鍔ㄥ垏鎹㈠埌鏇寸ǔ瀹氱殑瑙ｉ噴鍣? {candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    # 涓枃鎻愮ず锛氶伩鍏嶆妸搴曞眰 TLS 鍏煎闂璇垽鎴愯剼鏈€昏緫閿欒銆?
    if sys.version_info >= (3, 14):
        print("[提示] 当前 Python 为 3.14+，若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。")


ensure_stable_python_runtime()
warn_runtime_compatibility()

# 鏃犲ご鏈嶅姟鍣ㄨ嚜鍔ㄥ惎鐢?Xvfb 铏氭嫙鏄剧ず鍣?
_virtual_display = None
if not os.environ.get("DISPLAY") or os.environ.get("USE_XVFB") == "1":
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=0, size=(1920, 1080))
        _virtual_display.start()
        print(f"[*] Xvfb 铏氭嫙鏄剧ず鍣ㄥ凡鍚姩: {os.environ.get('DISPLAY')}")
    except Exception as e:
        print(f"[Warn] Xvfb 鍚姩澶辫触: {e}锛屽皢灏濊瘯鐩存帴杩愯")

co = ChromiumOptions()
co.auto_port()
co.set_argument("--no-sandbox")
co.set_argument("--disable-gpu")
co.set_argument("--disable-dev-shm-usage")
co.set_argument("--disable-software-rasterizer")
if not os.environ.get("DISPLAY"):
    co.set_argument("--headless=new")

# 浠?config.json 璇诲彇浠ｇ悊閰嶇疆缁欐祻瑙堝櫒
_browser_proxy = ""
try:
    import json as _json_mod
    _cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.isfile(_cfg_path):
        with open(_cfg_path, "r") as _f:
            _cfg = _json_mod.load(_f)
        _browser_proxy = str(_cfg.get("browser_proxy", "") or _cfg.get("proxy", "") or "")
except Exception:
    pass
if _browser_proxy:
    co.set_proxy(_browser_proxy)
    print(f"[*] 娴忚鍣ㄤ唬鐞? {_browser_proxy}")

# Linux 鏈嶅姟鍣ㄨ嚜鍔ㄦ娴?chromium 璺緞
import platform
import shutil
import glob as _glob_mod
_linux_browser_path = ""
if platform.system() == "Linux":
    # 浼樺厛鐢?playwright 瑁呯殑 chromium锛堟棤 AppArmor 闄愬埗锛?
    _pw_chromes = _glob_mod.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    if _pw_chromes:
        _linux_browser_path = _pw_chromes[0]
        co.set_browser_path(_linux_browser_path)
    else:
        for _candidate in ["/usr/bin/chromium-browser", "/usr/bin/chromium", "/usr/bin/google-chrome"]:
            if os.path.isfile(_candidate):
                _linux_browser_path = _candidate
                co.set_browser_path(_linux_browser_path)
                break
    # user_data_path 鍦?start_browser() 姣忚疆鍔ㄦ€佽缃紝姝ゅ涓嶅浐瀹?

co.set_timeouts(base=1)

# 鍔犺浇淇 MouseEvent.screenX / screenY 鐨勬墿灞曘€?
EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))
co.add_extension(EXTENSION_PATH)

_chrome_temp_dir: str = ""
browser = None
page = None

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_sso_dir = os.path.join(os.path.dirname(__file__), "sso")
_sso_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DEFAULT_SSO_FILE = os.path.join(_sso_dir, f"sso_{_sso_ts}.txt")


def start_browser():
    # 姣忚疆浠庡叏鏂版祻瑙堝櫒寮€濮嬶紝浣跨敤鐙珛涓存椂 profile 鐩綍閬垮厤 Cookie/Session 澶嶇敤銆?
    global browser, page, _chrome_temp_dir
    if platform.system() == "Linux" and not _linux_browser_path:
        raise RuntimeError(
            "未找到 Chrome/Chromium。请先安装浏览器后再运行。"
            "宿主机至少需要安装以下依赖："
            "`pip install -r requirements.txt`、`apt install xvfb`、"
            "`apt install chromium-browser` 或 `apt install google-chrome-stable`。"
        )
    _chrome_temp_dir = tempfile.mkdtemp(prefix="chrome_run_")
    co.set_user_data_path(_chrome_temp_dir)
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    return browser, page


def stop_browser():
    # 瀹屾暣鍏抽棴鏁翠釜娴忚鍣ㄥ疄渚嬶紝骞舵竻鐞嗘湰杞复鏃?profile锛屼緵涓嬩竴杞噸鏂版媺璧枫€?
    global browser, page, _chrome_temp_dir
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    if _chrome_temp_dir and os.path.isdir(_chrome_temp_dir):
        shutil.rmtree(_chrome_temp_dir, ignore_errors=True)
    _chrome_temp_dir = ""


def restart_browser():
    # 姣忚疆缁撴潫鍚庡仛涓€娆＄‖閲嶅惎锛岄伩鍏嶆敞鍐屾垚鍔熷悗椤甸潰璺宠浆瀵艰嚧鏃т笂涓嬫枃澶辨晥銆?    global browser, page
    try:
        stop_browser()
    except Exception:
        browser = None
        page = None

    try:
        start_browser()
    except Exception as e:
        print(f"[Warn] 娴忚鍣ㄩ噸鍚け璐ワ紝灏嗗湪涓嬫浣跨敤鏃剁户缁噸璇? {e}")
        browser = None
        page = None


def refresh_active_page():
    # 楠岃瘉鐮佺‘璁ゅ悗椤甸潰浼氳烦杞紝鏃?page 鍙ユ焺鍙兘鏂紑锛岃繖閲岀粺涓€閲嶆柊鑾峰彇褰撳墠娲诲姩鏍囩椤点€?
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
    except (ContextLostError, PageDisconnectedError):
        restart_browser()
    except Exception:
        restart_browser()
    return page


def open_signup_page():
    # 姣忚疆寮€濮嬫椂鎵撳紑娉ㄥ唽椤碉紝骞跺垏鍒扳€滀娇鐢ㄩ偖绠辨敞鍐屸€濇祦绋嬨€?
    global page
    refresh_active_page()
    try:
        page.get(SIGNUP_URL)
    except Exception:
        refresh_active_page()
        page = browser.new_tab(SIGNUP_URL)
    click_email_signup_button()


def close_current_page():
    # 鍏煎鏃ц皟鐢ㄥ悕锛屽疄闄呰涓烘敼涓烘暣杞噸鍚祻瑙堝櫒銆?
    restart_browser()


def has_profile_form():
    # 鏈€缁堟敞鍐岄〉鍙鍑虹幇濮撳悕鍜屽瘑鐮佽緭鍏ユ锛屽氨璁や负宸茬粡鎴愬姛杩涘叆璧勬枡濉啓闃舵銆?
    refresh_active_page()
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def click_email_signup_button(timeout=10):
    # 椤甸潰鎵撳紑鍚庯紝鑷姩鐐瑰嚮鈥滀娇鐢ㄩ偖绠辨敞鍐屸€濇寜閽€?
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('浣跨敤閭娉ㄥ唽') || text.includes('signupwithemail') || text.includes('signupemail') || text.includes('continuewith email') || text.includes('email');
});

if (!target) {
    return false;
}

target.click();
return true;
        """)

        if clicked:
            return True

        time.sleep(0.5)

    raise Exception('未找到“使用邮箱注册”按钮')


def fill_email_and_submit(timeout=15):
    # 澶嶇敤 `email_register.py` 閲岀殑閭鑾峰彇閫昏緫锛屼繚鐣欓偖绠变笌 token 渚涘悗缁獙璇佺爜姝ラ缁х画浣跨敤銆?
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("鑾峰彇閭澶辫触")

    deadline = time.time() + timeout
    while time.time() < deadline:
        filled = page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

// 涓嶈兘鍙啓 `input.value = xxx`锛屽惁鍒?React / 鍙楁帶琛ㄥ崟鍙兘娌℃湁鍚屾鍐呴儴鐘舵€併€?
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == 'not-ready':
            time.sleep(0.25)
            continue

        if filled != 'filled':
            print(f"[Debug] 閭杈撳叆妗嗗凡鍑虹幇锛屼絾鍐欏叆澶辫触: {filled}")
            time.sleep(0.25)
            continue

        if filled == 'filled':
            time.sleep(0.35)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '娉ㄥ唽' || text.includes('娉ㄥ唽') || t === 'signup' || t === 'sign up' || t.includes('sign up');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                print(f"[*] 宸插～鍐欓偖绠卞苟鐐瑰嚮娉ㄥ唽: {email}")
                return email, dev_token

        time.sleep(0.5)

    raise Exception("未找到邮箱输入框或注册按钮")



def fill_code_and_submit(email, dev_token, timeout=60):
    # 轮询邮箱验证码，并在页面上完成 OTP 输入与确认。
    code = get_oai_code(dev_token, email)
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);
    input.blur();

    const merged = String(input.value || '').trim();
    return merged === code ? 'filled' : 'aggregate-mismatch';
}

if (!otpBoxes.length) {
    return 'not-ready';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            time.sleep(0.4)
            continue

        if filled == "not-ready":
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码确认。")
                return code
            time.sleep(0.25)
            continue

        if filled != "filled":
            print(f"[Debug] 楠岃瘉鐮佽緭鍏ユ宸插嚭鐜帮紝浣嗗啓鍏ュけ璐? {filled}")
            time.sleep(0.25)
            continue

        time.sleep(0.5)
        try:
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase();
    return text === '纭閭'
        || text.includes('纭閭')
        || text === '缁х画'
        || text.includes('缁х画')
        || text === '下一步'
        || text.includes('下一步')
        || t.includes('confirm')
        || t.includes('continue')
        || t.includes('next')
        || t.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                """
            )
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            if has_profile_form():
                print("[*] 确认邮箱后页面跳转成功，已进入最终注册页。")
                return code
            time.sleep(0.4)
            continue

        if clicked == "clicked":
            print(f"[*] 已填写验证码并点击确认邮箱: {code}")
            time.sleep(0.8)
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码确认完成，最终注册页已就绪。")
            return code

        if clicked == "no-button":
            current_url = page.url
            if "sign-up" in current_url or "signup" in current_url:
                print(f"[*] 宸插～鍐欓獙璇佺爜锛岄〉闈㈠凡鑷姩璺宠浆鍒颁笅涓€姝? {current_url}")
                return code

        time.sleep(0.25)

    try:
        debug_snapshot = page.run_js(
            r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
            """
        )
        print(f"[Debug] 楠岃瘉鐮侀〉 DOM 鎽樿: {debug_snapshot}")
    except Exception as e:
        print(f"[Debug] 楠岃瘉鐮侀〉 DOM 鎽樿鑾峰彇澶辫触: {e}")
    raise Exception("鏈壘鍒伴獙璇佺爜杈撳叆妗嗘垨纭閭鎸夐挳")


def getTurnstileToken():
    # 澶嶇敤鐜版湁 turnstile 澶勭悊閫昏緫锛屽湪鏈€缁堟敞鍐岄〉闇€瑕佹椂鍐嶈Е鍙戙€?
    page.run_js("try { turnstile.reset() } catch(e) { }")

    turnstileResponse = None

    for i in range(0, 15):
        try:
            turnstileResponse = page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
            if turnstileResponse:
                return turnstileResponse

            challengeSolution = page.ele("@name=cf-turnstile-response")
            challengeWrapper = challengeSolution.parent()
            challengeIframe = challengeWrapper.shadow_root.ele("tag:iframe")

            challengeIframe.run_js("""
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

// 鏃ф柟妗堝湪 4K 灞忎笅涓嶇ǔ瀹氾紝杩欓噷缁欏嚭鏇磋嚜鐒剁殑灞忓箷鍧愭爣銆?
let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);

Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                        """)

            challengeIframeBody = challengeIframe.ele("tag:body").shadow_root
            challengeButton = challengeIframeBody.ele("tag:input")
            challengeButton.click()
        except:
            pass
        time.sleep(1)
    raise Exception("failed to solve turnstile")


def build_profile():
    # 生成一组可重复使用的注册资料，密码至少包含大小写、数字和特殊字符。
    given_names = [
        "Ethan", "Noah", "Liam", "Owen", "Mason",
        "Lucas", "Leo", "Ryan", "Evan", "Aiden",
        "Mia", "Emma", "Lily", "Chloe", "Grace",
        "Nora", "Zoe", "Alice", "Ella", "Ruby",
    ]
    family_names = [
        "Smith", "Johnson", "Brown", "Taylor", "Miller",
        "Davis", "Wilson", "Moore", "Clark", "Walker",
        "Hall", "Young", "King", "Wright", "Scott",
    ]
    given_name = secrets.choice(given_names)
    family_name = secrets.choice(family_names)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=30):
    # 鍦ㄩ獙璇佺爜閫氳繃鍚庯紝鐩存帴閿佸畾鈥滃彲瑙佷笖鍙啓鈥濈殑鐪熷疄杈撳叆妗嗭紝閬垮厤鍛戒腑闅愯棌鑺傜偣鎴?React 鍙楁帶鍓湰銆?
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    turnstile_token = ""

    while time.time() < deadline:
        filled = page.run_js(
            """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'filled-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
            """,
            given_name,
            family_name,
            password,
        )

        if filled == 'not-ready':
            time.sleep(0.25)
            continue

        if filled != 'filled':
            print(f"[Debug] 鏈€缁堟敞鍐岄〉杈撳叆妗嗗凡鍑虹幇锛屼絾濮撳悕/瀵嗙爜鍐欏叆澶辫触: {filled}")
            time.sleep(0.25)
            continue

        values_ok = page.run_js(
            """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
            """,
            given_name,
            family_name,
            password,
        )
        if not values_ok:
            print("[Debug] 最终注册页字段值校验失败，继续重试填写。")
            time.sleep(0.25)
            continue

        turnstile_state = page.run_js(
            """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return 'not-found';
}
const value = String(challengeInput.value || '').trim();
return value ? 'ready' : 'pending';
            """
        )

        if turnstile_state == "pending" and not turnstile_token:
            print("[*] 检测到最终注册页存在 Turnstile，开始使用现有真人化点击逻辑。")
            turnstile_token = getTurnstileToken()
            if turnstile_token:
                synced = page.run_js(
                    """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return false;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                    """,
                    turnstile_token,
                )
                if synced:
                    print("[*] Turnstile 响应已同步到最终注册表单。")

        time.sleep(0.5)

        try:
            submit_button = page.ele('tag:button@@text()=瀹屾垚娉ㄥ唽') or page.ele('tag:button@@text():Create Account') or page.ele('tag:button@@text():Sign up')
        except Exception:
            submit_button = None

        try:
            if not submit_button:
                clicked = page.run_js(
                    r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button'));
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '瀹屾垚娉ㄥ唽' || text.includes('瀹屾垚娉ㄥ唽') || t.includes('create account') || t.includes('sign up') || t.includes('complete');
});
if (!submitButton || submitButton.disabled || submitButton.getAttribute('aria-disabled') === 'true') {
    return false;
}
submitButton.focus();
submitButton.click();
return true;
                    """
                )
            else:
                challenge_value = page.run_js(
                    """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
return challengeInput ? String(challengeInput.value || '').trim() : 'not-found';
                    """
                )
                if challenge_value not in ('not-found', ''):
                    submit_button.click()
                    clicked = True
                else:
                    clicked = False
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            if has_profile_form():
                time.sleep(0.25)
                continue
            print("[*] 最终注册提交后页面已刷新，继续等待 sso cookie。")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        if clicked:
            print(f"[*] 宸插～鍐欐敞鍐岃祫鏂欏苟鐐瑰嚮瀹屾垚娉ㄥ唽: {given_name} {family_name} / {password}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        time.sleep(0.25)

    raise Exception("鏈壘鍒版渶缁堟敞鍐岃〃鍗曟垨瀹屾垚娉ㄥ唽鎸夐挳")


def extract_visible_numbers(timeout=60):
    # 鐧诲綍/娉ㄥ唽瀹屾垚鍚庯紝鎻愬彇椤甸潰涓婂彲瑙佺殑鏅€氭暟瀛楁枃鏈紝涓嶅鐞嗕换浣曟晱鎰?Cookie銆?
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] 椤甸潰鍙鏁板瓧鏂囨湰鎻愬彇缁撴灉:")
            for item in result:
                try:
                    print(f"    - 鏁板瓧: {item['value']} | 涓婁笅鏂? {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise Exception("登录后未提取到可见数字文本")


def wait_for_sso_cookie(timeout=30):
    # 蹇呴』鍦ㄦ敞鍐屽畬鎴愬悗鍐嶅彇 sso锛屼紭鍏堟姄鍙栫簿纭殑 sso cookie銆?
    deadline = time.time() + timeout
    last_seen_names = set()

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(0.4)
                continue

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value

        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
        except Exception:
            pass

        time.sleep(0.4)

    raise Exception(f"娉ㄥ唽瀹屾垚鍚庢湭鑾峰彇鍒?sso cookie锛屽綋鍓嶅凡瑙?cookie: {sorted(last_seen_names)}")


def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    # 鎸夌敤鎴疯姹傦紝涓€琛屽啓涓€涓?sso 鍊硷紝鎸佺画杩藉姞銆?
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("寰呭啓鍏ョ殑 sso 涓虹┖")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(normalized + "\n")

    print(f"[*] 宸茶拷鍔犲啓鍏?sso 鍒版枃浠? {output_path}")


def push_sso_to_api(new_tokens: list):
    # 鍙帴鏀朵富 host锛屽唴閮ㄥ浐瀹氭嫾鏂扮増 grok2api token 鎺ュ彛銆?    import json
    from urllib.parse import urlparse
    import urllib3
    import requests

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def normalize_api_host(value: str) -> str:
        raw = str(value or "").strip().rstrip("/")
        if not raw:
            return ""
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return raw

    def build_api_url(api_host: str, path: str) -> str:
        host = normalize_api_host(api_host)
        if not host:
            return ""
        return f"{host}{path if path.startswith('/') else '/' + path}"

    def extract_existing_tokens(payload) -> list[str] | None:
        if isinstance(payload, list):
            source = payload
        elif isinstance(payload, dict) and isinstance(payload.get("tokens"), list):
            source = payload.get("tokens", [])
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            source = payload.get("data", [])
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            source = payload.get("items", [])
        elif isinstance(payload, dict) and isinstance(payload.get("tokens"), dict):
            source = payload["tokens"].get("ssoBasic", [])
        elif isinstance(payload, dict) and isinstance(payload.get("ssoBasic"), list):
            source = payload.get("ssoBasic", [])
        else:
            return None
        return [
            item["token"] if isinstance(item, dict) else str(item)
            for item in source if item
        ]

    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception as e:
        print(f"[Warn] 璇诲彇 config.json 澶辫触锛岃烦杩囨帹閫? {e}")
        return

    api_conf = conf.get("api", {})
    api_host = str(api_conf.get("endpoint", "")).strip()
    api_token = str(api_conf.get("token", "")).strip()
    if not api_host or not api_token:
        return

    add_url = build_api_url(api_host, "/admin/api/tokens/add")
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    tokens_to_push = [str(t).strip() for t in new_tokens if str(t or "").strip()]
    if not tokens_to_push:
        return

    if not tokens_to_push:
        print("[*] 本次没有新增 token 需要推送到 API。")
        return

    try:
        resp = requests.post(
            add_url,
            json={"pool": "auto", "tokens": tokens_to_push},
            headers=headers,
            timeout=60,
            verify=False,
        )
        if resp.status_code in {200, 201, 204}:
            print(f"[*] SSO token 宸叉帹閫佸埌 API锛堟柊澧?{len(tokens_to_push)} 涓級: {add_url}")
        else:
            print(f"[Warn] 鎺ㄩ€?API 杩斿洖寮傚父: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        print(f"[Warn] 鎺ㄩ€?API 澶辫触: {type(e).__name__}: {e} | url={add_url}")


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    # 鍗曡疆娴佺▼锛氭墦寮€娉ㄥ唽椤?-> 瀹屾垚娉ㄥ唽 -> 鑾峰彇 sso -> 鍐?txt銆?
    open_signup_page()
    email, dev_token = fill_email_and_submit()
    fill_code_and_submit(email, dev_token)
    profile = fill_profile_and_submit()
    sso_value = wait_for_sso_cookie()
    append_sso_to_txt(sso_value, output_path)

    if extract_numbers:
        extract_visible_numbers()

    result = {
        "email": email,
        "sso": sso_value,
        **profile,
    }

    if run_logger:
        run_logger.info(
            "娉ㄥ唽鎴愬姛 | email=%s | password=%s | given=%s | family=%s",
            email,
            profile.get("password", ""),
            profile.get("given_name", ""),
            profile.get("family_name", ""),
        )

    print(f"[*] 鏈疆娉ㄥ唽瀹屾垚锛岄偖绠? {email}")
    return result


def load_run_count() -> int:
    # 浠?config.json 璇诲彇榛樿鎵ц杞暟锛岄厤缃笉瀛樺湪鏃惰繑鍥?10銆?
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        v = conf.get("run", {}).get("count")
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    return 10


def main():
    # 榛樿寰幆鎵ц锛涙瘡杞畬鎴愬悗鍏抽棴褰撳墠椤碉紝鍐嶈嚜鍔ㄨ繘鍏ヤ笅涓€杞€?
    global run_logger
    run_logger = setup_run_logger()

    config_count = load_run_count()

    parser = argparse.ArgumentParser(description="xAI 自动注册并采集 sso")
    parser.add_argument("--count", type=int, default=config_count, help=f"执行轮数，0 表示无限循环（默认读取 config.json run.count，当前 {config_count}）")
    parser.add_argument("--output", default=DEFAULT_SSO_FILE, help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本")
    args = parser.parse_args()

    current_round = 0
    success_count = 0
    try:
        start_browser()
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            print(f"\n[*] 开始第 {current_round} 轮注册")
            round_succeeded = False

            try:
                result = run_single_registration(args.output, extract_numbers=args.extract_numbers)
                success_count += 1
                round_succeeded = True
                print(f"[*] 第 {current_round} 轮注册成功，立即推送当前 token 到 API...")
                try:
                    push_sso_to_api([result["sso"]])
                except Exception as push_error:
                    print(f"[Warn] 当前 token 立即推送失败，但不影响后续任务: {push_error}")
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {current_round} 轮失败: {error}")
            finally:
                if args.count == 0 or current_round < args.count:
                    stop_browser()

            if args.count == 0 or current_round < args.count:
                time.sleep(0.5)

    finally:
        if success_count:
            print(f"\n[*] 注册结束，本次共成功 {success_count} 轮，token 已按成功轮次即时推送。")
        stop_browser()


def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"run_{ts}.log")

    logger = logging.getLogger("grok_register")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("鏃ュ織鏂囦欢: %s", log_path)
    return logger


def wait_for_condition(
    predicate: Callable[[], Any],
    timeout: float,
    interval: float = 0.15,
    recover: Callable[[], Any] | None = None,
) -> Any | None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (ContextLostError, PageDisconnectedError):
            if recover:
                recover()
        except Exception:
            pass
        time.sleep(interval)
    return None


def otp_form_visible() -> bool:
    refresh_active_page()
    try:
        return bool(
            page.run_js(
                """
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

return !!(aggregateInput || otpBoxes.length);
                """
            )
        )
    except Exception:
        return False


def read_turnstile_token() -> str:
    refresh_active_page()
    try:
        token = page.run_js(
            """
try {
    const input = document.querySelector('input[name="cf-turnstile-response"]');
    const direct = input ? String(input.value || '').trim() : '';
    if (direct) {
        return direct;
    }
    if (typeof turnstile !== 'undefined' && turnstile && typeof turnstile.getResponse === 'function') {
        return String(turnstile.getResponse() || '').trim();
    }
} catch (e) {}
return '';
            """
        )
        return str(token or "").strip()
    except Exception:
        return ""


def _turnstile_poll_interval(started_at: float) -> float:
    elapsed = time.perf_counter() - started_at
    if elapsed < 2:
        return 0.15
    if elapsed < 6:
        return 0.25
    if elapsed < 12:
        return 0.4
    return 0.6


def _turnstile_log(stage: str, started_at: float, detail: str = "") -> None:
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    message = f"[Turnstile][{elapsed_ms:>5}ms] {stage}"
    if detail:
        message = f"{message} | {detail}"
    print(message)


def fill_email_and_submit(timeout=15):
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("鑾峰彇閭澶辫触")

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            filled = page.run_js(
                """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return 'fill-failed';
}

input.blur();
return 'filled';
                """,
                email,
            )
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            continue

        if filled == "not-ready":
            time.sleep(0.15)
            continue

        if filled != "filled":
            print(f"[Debug] 閭杈撳叆妗嗗凡鍑虹幇锛屼絾鍐欏叆澶辫触: {filled}")
            time.sleep(0.15)
            continue

        clicked = wait_for_condition(
            lambda: page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text === '娉ㄥ唽' || text.includes('娉ㄥ唽') || text === 'signup' || text === 'sign up' || text.includes('sign up');
});

if (!submitButton) {
    return false;
}

submitButton.focus();
submitButton.click();
return true;
                """
            ),
            timeout=3.5,
            interval=0.1,
            recover=refresh_active_page,
        )
        if clicked:
            print(f"[*] 宸插～鍐欓偖绠卞苟鐐瑰嚮娉ㄥ唽: {email}")
            return email, dev_token

    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(email, dev_token, timeout=60):
    code = get_oai_code(dev_token, email)
    if not code:
        raise Exception("获取验证码失败")

    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);
    input.blur();
    return String(input.value || '').trim() === code ? 'filled' : 'aggregate-mismatch';
}

if (!otpBoxes.length) {
    return 'not-ready';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            if has_profile_form():
                print("[*] 验证码提交后已跳转到最终注册页。")
                return code
            continue

        if filled == "not-ready":
            if has_profile_form():
                print("[*] 已直接进入最终注册页，跳过验证码确认。")
                return code
            time.sleep(0.15)
            continue

        if filled != "filled":
            print(f"[Debug] 楠岃瘉鐮佽緭鍏ユ宸插嚭鐜帮紝浣嗗啓鍏ュけ璐? {filled}")
            time.sleep(0.15)
            continue

        click_result = wait_for_condition(
            lambda: page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text === '纭閭'
        || text.includes('纭閭')
        || text === '缁х画'
        || text.includes('缁х画')
        || text === '下一步'
        || text.includes('下一步')
        || text.includes('confirm')
        || text.includes('continue')
        || text.includes('next')
        || text.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                """
            ),
            timeout=4,
            interval=0.1,
            recover=refresh_active_page,
        )

        if click_result == "no-button":
            refresh_active_page()
            if has_profile_form() or not otp_form_visible():
                print(f"[*] 已填写验证码，页面已自动进入下一步: {page.url}")
                return code
            time.sleep(0.15)
            continue

        if click_result == "clicked":
            print(f"[*] 已填写验证码并点击确认邮箱: {code}")
            advanced = wait_for_condition(
                lambda: has_profile_form() or not otp_form_visible(),
                timeout=6,
                interval=0.15,
                recover=refresh_active_page,
            )
            if advanced:
                refresh_active_page()
                if has_profile_form():
                    print("[*] 验证码确认完成，最终注册页已就绪。")
                else:
                    print(f"[*] 已填写验证码，页面已自动进入下一步: {page.url}")
                return code

    raise Exception("鏈壘鍒伴獙璇佺爜杈撳叆妗嗘垨纭閭鎸夐挳")


def getTurnstileToken(timeout: float = 20.0):
    started_at = time.perf_counter()
    deadline = started_at + timeout
    last_error = ""
    last_click_at = 0.0
    click_count = 0
    attempt = 0

    _turnstile_log("start", started_at, "寮€濮嬭幏鍙?Turnstile token")
    try:
        page.run_js("try { turnstile.reset() } catch (e) {}")
        _turnstile_log("reset", started_at, "宸茶皟鐢?turnstile.reset()")
    except Exception as exc:
        _turnstile_log("reset-skip", started_at, f"{type(exc).__name__}: {exc}")

    while time.perf_counter() < deadline:
        attempt += 1

        token = read_turnstile_token()
        if token:
            _turnstile_log("token-ready", started_at, f"attempt={attempt} len={len(token)}")
            return token

        probe_started_at = time.perf_counter()
        try:
            probe = page.run_js(
                """
const input = document.querySelector('input[name="cf-turnstile-response"]');
const inputValue = input ? String(input.value || '').trim() : '';
const iframeCount = document.querySelectorAll('iframe').length;
const widgetCount = document.querySelectorAll('[name="cf-turnstile-response"], iframe[src*="turnstile"], iframe[title*="Widget"]').length;
return {
    hasInput: !!input,
    inputReady: !!inputValue,
    iframeCount,
    widgetCount,
};
                """
            ) or {}
            probe_cost_ms = int((time.perf_counter() - probe_started_at) * 1000)
            _turnstile_log("probe", started_at, f"attempt={attempt} cost={probe_cost_ms}ms state={probe}")
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            _turnstile_log("context-refresh", started_at, f"attempt={attempt} 椤甸潰涓婁笅鏂囦涪澶憋紝宸插埛鏂版椿鍔ㄩ〉")
            time.sleep(0.15)
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _turnstile_log("probe-failed", started_at, f"attempt={attempt} {last_error}")

        now = time.perf_counter()
        if now - last_click_at < 0.9:
            time.sleep(_turnstile_poll_interval(started_at))
            continue

        try:
            locate_started_at = time.perf_counter()
            challenge_solution = page.ele("@name=cf-turnstile-response")
            challenge_wrapper = challenge_solution.parent() if challenge_solution else None
            challenge_iframe = None
            if challenge_wrapper and challenge_wrapper.shadow_root:
                challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")
            locate_cost_ms = int((time.perf_counter() - locate_started_at) * 1000)
            _turnstile_log(
                "locate",
                started_at,
                f"attempt={attempt} cost={locate_cost_ms}ms iframe={'yes' if challenge_iframe else 'no'}",
            )

            if not challenge_iframe:
                time.sleep(_turnstile_poll_interval(started_at))
                continue

            patch_started_at = time.perf_counter()
            challenge_iframe.run_js(
                """
window.dtp = 1;
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}
const screenX = getRandomInt(800, 1200);
const screenY = getRandomInt(400, 600);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                """
            )
            patch_cost_ms = int((time.perf_counter() - patch_started_at) * 1000)
            _turnstile_log("patch", started_at, f"attempt={attempt} cost={patch_cost_ms}ms")

            click_started_at = time.perf_counter()
            challenge_body = challenge_iframe.ele("tag:body").shadow_root
            challenge_button = challenge_body.ele("tag:input") if challenge_body else None
            if not challenge_button:
                _turnstile_log("button-missing", started_at, f"attempt={attempt}")
                time.sleep(_turnstile_poll_interval(started_at))
                continue

            challenge_button.click()
            click_count += 1
            last_click_at = time.perf_counter()
            click_cost_ms = int((time.perf_counter() - click_started_at) * 1000)
            _turnstile_log(
                "click",
                started_at,
                f"attempt={attempt} click_count={click_count} cost={click_cost_ms}ms",
            )

            token = wait_for_condition(
                read_turnstile_token,
                timeout=1.8,
                interval=0.12,
                recover=refresh_active_page,
            )
            if token:
                _turnstile_log("token-after-click", started_at, f"attempt={attempt} len={len(token)}")
                return token
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            _turnstile_log("context-refresh", started_at, f"attempt={attempt} 点击后上下文丢失，已刷新活动页")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _turnstile_log("click-failed", started_at, f"attempt={attempt} {last_error}")

        time.sleep(_turnstile_poll_interval(started_at))

    raise Exception(f"Turnstile 澶勭悊瓒呮椂锛坽timeout:.1f}s锛夛紝鏈€鍚庨敊璇? {last_error or 'none'}")


def fill_profile_and_submit(timeout=30):
    given_name, family_name, password = build_profile()
    deadline = time.perf_counter() + timeout
    turnstile_token = ""

    while time.perf_counter() < deadline:
        try:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) {
        return false;
    }
    input.focus();
    input.click();

    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }

    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }

    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));

    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return 'not-ready';
}

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);

if (!givenOk || !familyOk || !passwordOk) {
    return 'fill-failed';
}

return [
    String(givenInput.value || '').trim() === String(givenName || '').trim(),
    String(familyInput.value || '').trim() === String(familyName || '').trim(),
    String(passwordInput.value || '') === String(password || ''),
].every(Boolean) ? 'filled' : 'verify-failed';
                """,
                given_name,
                family_name,
                password,
            )
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            continue

        if filled == "not-ready":
            time.sleep(0.15)
            continue

        if filled != "filled":
            print(f"[Debug] 鏈€缁堟敞鍐岄〉杈撳叆妗嗗凡鍑虹幇锛屼絾濮撳悕/瀵嗙爜鍐欏叆澶辫触: {filled}")
            time.sleep(0.15)
            continue

        values_ok = page.run_js(
            """
const expectedGiven = arguments[0];
const expectedFamily = arguments[1];
const expectedPassword = arguments[2];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');

if (!givenInput || !familyInput || !passwordInput) {
    return false;
}

return String(givenInput.value || '').trim() === String(expectedGiven || '').trim()
    && String(familyInput.value || '').trim() === String(expectedFamily || '').trim()
    && String(passwordInput.value || '') === String(expectedPassword || '');
            """,
            given_name,
            family_name,
            password,
        )
        if not values_ok:
            print("[Debug] 最终注册页字段校验失败，继续重试填写。")
            time.sleep(0.15)
            continue

        turnstile_state = page.run_js(
            """
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return { state: 'not-found', token: '' };
}
const token = String(challengeInput.value || '').trim();
return { state: token ? 'ready' : 'pending', token };
            """
        ) or {"state": "not-found", "token": ""}

        if turnstile_state.get("state") == "pending" and not turnstile_token:
            print("[*] 检测到最终注册页存在 Turnstile，开始获取 token。")
            turnstile_token = getTurnstileToken()

        if turnstile_token and turnstile_state.get("token") != turnstile_token:
            synced = page.run_js(
                """
const token = arguments[0];
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!challengeInput) {
    return false;
}
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) {
    nativeSetter.call(challengeInput, token);
} else {
    challengeInput.value = token;
}
challengeInput.dispatchEvent(new Event('input', { bubbles: true }));
challengeInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(challengeInput.value || '').trim() === String(token || '').trim();
                """,
                turnstile_token,
            )
            if synced:
                print("[*] Turnstile 响应已同步到最终注册表单。")

        clicked = wait_for_condition(
            lambda: page.run_js(
                r"""
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
if (challengeInput && !String(challengeInput.value || '').trim()) {
    return false;
}

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text === '瀹屾垚娉ㄥ唽' || text.includes('瀹屾垚娉ㄥ唽') || text.includes('create account') || text.includes('sign up') || text.includes('complete');
});

if (!submitButton) {
    return false;
}

submitButton.focus();
submitButton.click();
return true;
                """
            ),
            timeout=4.5,
            interval=0.1,
            recover=refresh_active_page,
        )
        if clicked:
            print(f"[*] 宸插～鍐欐敞鍐岃祫鏂欏苟鐐瑰嚮瀹屾垚娉ㄥ唽: {given_name} {family_name} / {password}")
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

    raise Exception("鏈壘鍒版渶缁堟敞鍐岃〃鍗曟垨瀹屾垚娉ㄥ唽鎸夐挳")


def wait_for_sso_cookie(timeout=30):
    deadline = time.perf_counter() + timeout
    last_seen_names = set()

    while time.perf_counter() < deadline:
        try:
            if page is None:
                refresh_active_page()

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    print("[*] 注册完成后已获取到 sso cookie。")
                    return value
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
        except Exception:
            refresh_active_page()

        time.sleep(0.2)

    raise Exception(f"娉ㄥ唽瀹屾垚鍚庢湭鑾峰彇鍒?sso cookie锛屽綋鍓嶅凡瑙?cookie: {sorted(last_seen_names)}")


def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("寰呭啓鍏ョ殑 sso 涓虹┖")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(normalized + "\n")

    print(f"[*] 宸茶拷鍔犲啓鍏?sso 鍒版枃浠? {output_path}")


def push_sso_to_api(new_tokens: list):
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            conf = json.load(file)
    except Exception as exc:
        print(f"[Warn] 璇诲彇 config.json 澶辫触锛岃烦杩囨帹閫? {exc}")
        return

    api_conf = conf.get("api", {}) or {}
    api_host = str(api_conf.get("endpoint", "") or "").strip()
    api_token = str(api_conf.get("token", "") or "").strip()
    if not api_host or not api_token:
        return

    ok, message = push_tokens(
        api_host=api_host,
        api_token=api_token,
        tokens=new_tokens,
        timeout=60,
        verify=False,
    )
    if ok:
        if message != "No tokens to push.":
            print(f"[*] {message}")
    else:
        print(f"[Warn] {message}")


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    open_signup_page()
    email, dev_token = fill_email_and_submit()
    fill_code_and_submit(email, dev_token)
    profile = fill_profile_and_submit()
    sso_value = wait_for_sso_cookie()
    append_sso_to_txt(sso_value, output_path)

    if extract_numbers:
        extract_visible_numbers()

    result = {
        "email": email,
        "sso": sso_value,
        **profile,
    }

    if run_logger:
        run_logger.info(
            "娉ㄥ唽鎴愬姛 | email=%s | password=%s | given=%s | family=%s",
            email,
            profile.get("password", ""),
            profile.get("given_name", ""),
            profile.get("family_name", ""),
        )

    print(f"[*] 鏈疆娉ㄥ唽瀹屾垚锛岄偖绠? {email}")
    return result


def main():
    global run_logger
    run_logger = setup_run_logger()

    config_count = load_run_count()

    parser = argparse.ArgumentParser(description="xAI 自动注册并采集 sso")
    parser.add_argument("--count", type=int, default=config_count, help=f"执行轮数，0 表示无限循环（默认读取 config.json run.count，当前 {config_count}）")
    parser.add_argument("--output", default=DEFAULT_SSO_FILE, help="sso 输出 txt 路径")
    parser.add_argument("--extract-numbers", action="store_true", help="注册完成后额外提取页面数字文本")
    args = parser.parse_args()

    current_round = 0
    success_count = 0
    try:
        start_browser()
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            print(f"\n[*] 开始第 {current_round} 轮注册")

            try:
                result = run_single_registration(args.output, extract_numbers=args.extract_numbers)
                success_count += 1
                print(f"[*] 第 {current_round} 轮注册成功，立即推送当前 token 到 API...")
                try:
                    push_sso_to_api([result["sso"]])
                except Exception as push_error:
                    print(f"[Warn] 当前 token 立即推送失败，但不影响后续任务: {push_error}")
            except KeyboardInterrupt:
                print("\n[Info] 收到中断信号，停止后续轮次。")
                break
            except Exception as error:
                print(f"[Error] 第 {current_round} 轮失败: {error}")
            finally:
                if args.count == 0 or current_round < args.count:
                    stop_browser()

            if args.count == 0 or current_round < args.count:
                time.sleep(0.2)
    finally:
        if success_count:
            print(f"\n[*] 注册结束，本次共成功 {success_count} 轮，token 已按成功轮次即时推送。")
        stop_browser()


def getTurnstileToken(timeout: float = 20.0):
    started_at = time.perf_counter()
    deadline = started_at + timeout
    last_error = ""
    click_count = 0
    attempt = 0

    _turnstile_log("start", started_at, "寮€濮嬭幏鍙?Turnstile token")
    try:
        page.run_js("try { turnstile.reset() } catch (e) {}")
        _turnstile_log("reset", started_at, "宸茶皟鐢?turnstile.reset()")
    except Exception as exc:
        _turnstile_log("reset-skip", started_at, f"{type(exc).__name__}: {exc}")

    while time.perf_counter() < deadline:
        attempt += 1

        token = read_turnstile_token()
        if token:
            _turnstile_log("token-ready", started_at, f"attempt={attempt} len={len(token)}")
            return token

        try:
            probe_started_at = time.perf_counter()
            probe = page.run_js(
                """
const input = document.querySelector('input[name="cf-turnstile-response"]');
const inputValue = input ? String(input.value || '').trim() : '';
const iframeCount = document.querySelectorAll('iframe').length;
const widgetCount = document.querySelectorAll('[name="cf-turnstile-response"], iframe[src*="turnstile"], iframe[title*="Widget"]').length;
return {
    hasInput: !!input,
    inputReady: !!inputValue,
    iframeCount,
    widgetCount,
};
                """
            ) or {}
            probe_cost_ms = int((time.perf_counter() - probe_started_at) * 1000)
            _turnstile_log("probe", started_at, f"attempt={attempt} cost={probe_cost_ms}ms state={probe}")
        except (ContextLostError, PageDisconnectedError):
            refresh_active_page()
            _turnstile_log("context-refresh", started_at, f"attempt={attempt} 椤甸潰涓婁笅鏂囦涪澶憋紝宸插埛鏂版椿鍔ㄩ〉")
            time.sleep(0.15)
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            _turnstile_log("probe-failed", started_at, f"attempt={attempt} {last_error}")

        local_try = 0
        while time.perf_counter() < deadline and local_try < 4:
            local_try += 1

            token = read_turnstile_token()
            if token:
                _turnstile_log("token-ready", started_at, f"attempt={attempt} local_try={local_try} len={len(token)}")
                return token

            try:
                locate_started_at = time.perf_counter()
                challenge_solution = page.ele("@name=cf-turnstile-response")
                challenge_wrapper = challenge_solution.parent() if challenge_solution else None
                challenge_iframe = None
                if challenge_wrapper and challenge_wrapper.shadow_root:
                    challenge_iframe = challenge_wrapper.shadow_root.ele("tag:iframe")
                locate_cost_ms = int((time.perf_counter() - locate_started_at) * 1000)
                _turnstile_log(
                    "locate",
                    started_at,
                    f"attempt={attempt} local_try={local_try} cost={locate_cost_ms}ms iframe={'yes' if challenge_iframe else 'no'}",
                )

                if not challenge_iframe:
                    time.sleep(0.2)
                    continue

                patch_started_at = time.perf_counter()
                challenge_iframe.run_js(
                    """
window.dtp = 1;
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}
const screenX = getRandomInt(800, 1200);
const screenY = getRandomInt(400, 600);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                    """
                )
                patch_cost_ms = int((time.perf_counter() - patch_started_at) * 1000)
                _turnstile_log("patch", started_at, f"attempt={attempt} local_try={local_try} cost={patch_cost_ms}ms")

                click_started_at = time.perf_counter()
                challenge_body = challenge_iframe.ele("tag:body").shadow_root
                challenge_button = challenge_body.ele("tag:input") if challenge_body else None
                if not challenge_button:
                    _turnstile_log("button-missing", started_at, f"attempt={attempt} local_try={local_try}")
                    time.sleep(0.2)
                    continue

                challenge_button.click()
                click_count += 1
                click_cost_ms = int((time.perf_counter() - click_started_at) * 1000)
                _turnstile_log(
                    "click",
                    started_at,
                    f"attempt={attempt} local_try={local_try} click_count={click_count} cost={click_cost_ms}ms",
                )

                settle_deadline = min(deadline, time.perf_counter() + 3.2)
                settle_round = 0
                while time.perf_counter() < settle_deadline:
                    settle_round += 1
                    token = read_turnstile_token()
                    if token:
                        _turnstile_log(
                            "token-after-click",
                            started_at,
                            f"attempt={attempt} local_try={local_try} settle_round={settle_round} len={len(token)}",
                        )
                        return token
                    time.sleep(0.18)

                _turnstile_log(
                    "click-no-token",
                    started_at,
                    f"attempt={attempt} local_try={local_try} 本次点击后仍未拿到 token，继续当前轮重试",
                )
                time.sleep(0.25)
            except (ContextLostError, PageDisconnectedError):
                refresh_active_page()
                _turnstile_log(
                    "context-refresh",
                    started_at,
                    f"attempt={attempt} local_try={local_try} 点击过程中上下文丢失，已刷新活动页",
                )
                time.sleep(0.15)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                _turnstile_log(
                    "click-failed",
                    started_at,
                    f"attempt={attempt} local_try={local_try} {last_error}",
                )
                time.sleep(0.2)

        time.sleep(_turnstile_poll_interval(started_at))

    raise Exception(f"Turnstile 处理超时（{timeout:.1f}s），最后错误: {last_error or 'none'}")


class RoundTimeoutError(Exception):
    pass


TURNSTILE_HARD_TIMEOUT = 45.0


def _call_with_hard_timeout(func: Callable[[], Any], timeout: float, label: str) -> Any:
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def runner() -> None:
        try:
            result_queue.put((True, func()))
        except BaseException as exc:
            result_queue.put((False, exc))

    worker = threading.Thread(target=runner, daemon=True, name=f"{label}_guard")
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        raise RoundTimeoutError(f"{label} 超过 {timeout:.0f}s，已放弃当前轮并准备进入下一轮")

    if result_queue.empty():
        raise RoundTimeoutError(f"{label} 提前退出且没有返回结果，已放弃当前轮")

    ok, payload = result_queue.get()
    if ok:
        return payload
    raise payload


_REAL_GET_TURNSTILE_TOKEN = getTurnstileToken


def getTurnstileToken(timeout: float = 20.0):
    hard_timeout = max(TURNSTILE_HARD_TIMEOUT, float(timeout) + 10.0)
    return _call_with_hard_timeout(
        lambda: _REAL_GET_TURNSTILE_TOKEN(timeout=timeout),
        timeout=hard_timeout,
        label="Turnstile",
    )


if __name__ == "__main__":
    main()

