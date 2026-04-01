"""
Browser factory using undetected-chromedriver to avoid Instagram's bot detection.
"""
import random
import subprocess
import re
import logging
import ssl
import os
import time
import ctypes
import undetected_chromedriver as uc

logger = logging.getLogger("model_dm_bot")

# Global SSL Monkey-Patch: heavily bypasses SSL CERTIFICATE_VERIFY_FAILED 
# meaning undetected-chromedriver can freely download its bin on missing-cert servers.
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
]


def _parse_ints(text: str) -> set:
    return {int(x) for x in re.findall(r"\d+", str(text or ""))}


def _chrome_child_pids(chromedriver_pid: int) -> set:
    pids = set()
    if not chromedriver_pid:
        return pids

    # Prefer WMIC first (widely available on older Windows installs), fallback to PowerShell.
    try:
        result = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                f"(ParentProcessId={chromedriver_pid})",
                "get",
                "ProcessId",
                "/value",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            pids.update(_parse_ints(result.stdout))
    except Exception:
        pass

    if pids:
        return pids

    try:
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={chromedriver_pid}\" "
            "| Select-Object -ExpandProperty ProcessId"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if result.returncode == 0:
            pids.update(_parse_ints(result.stdout))
    except Exception:
        pass

    return pids


def _bring_window_to_front_windows(driver):
    if os.name != "nt":
        return

    try:
        user32 = ctypes.windll.user32
    except Exception:
        return

    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040

    title_hint = ""
    try:
        title_hint = str(driver.title or "").strip().lower()
    except Exception:
        pass

    target_pids = set()
    try:
        service_proc = getattr(getattr(driver, "service", None), "process", None)
        if service_proc and getattr(service_proc, "pid", None):
            target_pids.update(_chrome_child_pids(int(service_proc.pid)))
    except Exception:
        pass

    candidates = []
    enum_callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @enum_callback
    def _enum_windows(hwnd, _):
        try:
            hwnd = int(hwnd)
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            title_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value.strip()
            if not title:
                return True

            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            class_name = class_buf.value
            if class_name != "Chrome_WidgetWin_1":
                return True

            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            win_pid = int(pid.value)

            if target_pids and win_pid not in target_pids:
                return True

            title_lower = title.lower()
            score = 1 if "chrome" in title_lower else 0
            if "instagram" in title_lower:
                score += 3
            if title_hint and title_hint in title_lower:
                score += 5
            if "new tab" in title_lower:
                score -= 1

            candidates.append((score, hwnd))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_enum_windows, 0)
    except Exception:
        return

    if not candidates:
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    hwnd = int(candidates[0][1])

    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        user32.SetWindowPos(
            hwnd,
            HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _maximize_and_focus_browser(driver):
    try:
        window_info = driver.execute_cdp_cmd("Browser.getWindowForTarget", {})
        window_id = window_info.get("windowId") if isinstance(window_info, dict) else None
        if window_id is not None:
            driver.execute_cdp_cmd(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "maximized"}},
            )
    except Exception:
        pass

    try:
        driver.maximize_window()
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception:
        pass

    _bring_window_to_front_windows(driver)


def _detect_chrome_version() -> int:
    """
    Auto-detect the installed Chrome major version.
    Falls back to 145 if detection fails.
    """
    try:
        # Windows: query registry for Chrome version
        result = subprocess.run(
            ['reg', 'query', r'HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon', '/v', 'version'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            match = re.search(r'(\d+)\.', result.stdout)
            if match:
                version = int(match.group(1))
                logger.info(f"[Browser] Detected Chrome version: {version}")
                return version
    except Exception:
        pass

    try:
        # Fallback: try running chrome --version
        result = subprocess.run(
            ['chrome', '--version'], capture_output=True, text=True, timeout=5
        )
        match = re.search(r'(\d+)\.', result.stdout)
        if match:
            return int(match.group(1))
    except Exception:
        pass

    logger.info("[Browser] Could not detect Chrome version, defaulting to 145")
    return 145


def _normalize_proxy_server(proxy_value: str) -> str:
    """Normalize proxy strings to a Chrome --proxy-server compatible value."""
    clean = str(proxy_value or "").strip()
    if not clean:
        return ""

    if "://" in clean:
        return clean

    if ":" not in clean:
        raise ValueError(f"Invalid proxy format: {clean}")

    return f"http://{clean}"


def _mask_proxy_for_log(proxy_server: str) -> str:
    clean = str(proxy_server or "").strip()
    if not clean:
        return ""

    if "@" not in clean:
        return clean

    if "://" in clean:
        scheme, rest = clean.split("://", 1)
        prefix = f"{scheme}://"
    else:
        rest = clean
        prefix = ""

    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        username = creds.split(":", 1)[0]
        safe_creds = f"{username}:***"
    else:
        safe_creds = "***"

    return f"{prefix}{safe_creds}@{host}"


def create_driver(headless=False, proxy=None):
    """
    Create an undetected Chrome browser instance with anti-detection measures.
    Auto-detects Chrome version to avoid driver mismatch.
    
    Returns:
        uc.Chrome: Configured undetected Chrome driver
    """
    chrome_version = _detect_chrome_version()

    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")

    proxy_server = _normalize_proxy_server(proxy)
    if proxy_server:
        options.add_argument(f"--proxy-server={proxy_server}")
        logger.info(f"[Browser] Proxy enabled: {_mask_proxy_for_log(proxy_server)}")

    if headless:
        options.add_argument("--headless=new")

    driver = uc.Chrome(
        options=options,
        use_subprocess=True,
        version_main=chrome_version,
    )

    # Additional stealth: override navigator properties
    driver.execute_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        window.chrome = { runtime: {} };
    """)

    if not headless:
        # Give the top-level browser window a moment to fully materialize, then force focus.
        time.sleep(0.35)
        _maximize_and_focus_browser(driver)

    return driver


def close_driver(driver):
    """Safely close the browser driver (handles Windows cleanup errors)."""
    if not driver:
        return
    try:
        driver.close()
    except Exception:
        pass
    try:
        driver.quit()
    except OSError:
        pass  # WinError 6: The handle is invalid — safe to ignore
    except Exception:
        pass
