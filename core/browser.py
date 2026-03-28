"""
Browser factory using undetected-chromedriver to avoid Instagram's bot detection.
"""
import random
import subprocess
import re
import logging
import ssl
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


def create_driver(headless=False):
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
