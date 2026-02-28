#!/usr/bin/env python3
"""
Google PAA (People Also Ask) Parser v2.

Собирает вопросы и ответы из блока "Люди также спрашивают" Google.
Читает ответ сразу после клика (не в конце), переиспользует драйвер,
поддерживает гео/язык, чекпоинты, headless, дедупликацию, авто-решение капч.

Использование:
  python3 google_paa_parser.py                        # интерактивный режим
  python3 google_paa_parser.py -i my_queries.txt      # свой файл
  python3 google_paa_parser.py --hl ru --gl ru        # русский Google
  python3 google_paa_parser.py --clicks 20            # больше вопросов
  python3 google_paa_parser.py --headless             # без окна браузера
  python3 google_paa_parser.py --resume               # продолжить после сбоя
  python3 google_paa_parser.py --captcha-key YOUR_KEY # авто-решение капч
"""
import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, urlencode

from openpyxl import Workbook
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("paa")

# --- CSS selectors (Google PAA DOM, актуально на 2026-02) ---
PAA_CONTAINER = "div[jsname='N760b']"
PAIR_CONTAINER = "div[jsname='yEVEwb']"
QUESTION_SEL = "div[jsname='tJHJj']"
ANSWER_SEL = "div[jsname='NRdf4c']"
QUESTION_BTN = "div[jsname='pcRaIe']"
COOKIE_BTN = "div.QS5gu.sy4vM"

# Fallback selectors (если Google сменит jsname)
PAA_CONTAINER_ALT = [
    "div[data-initq]",
    "div[jscontroller='PoEVuc']",
]
COOKIE_BTN_ALT = [
    "button#L2AGLb",
    "button[jsname='b3VHJd']",
]

SCRIPT_DIR = Path(__file__).parent
CHECKPOINT_FILE = SCRIPT_DIR / ".checkpoint.json"

# Captcha API base URLs (все совместимы с 2captcha протоколом)
CAPTCHA_SERVICES = {
    "2captcha": "http://2captcha.com",
    "rucaptcha": "http://rucaptcha.com",
    "capguru": "http://api.cap.guru",
}


# ============================================================
# Interactive mode
# ============================================================

POPULAR_LOCALES = {
    "ru": ("ru", "ru", "Русский (Россия)"),
    "en": ("en", "us", "English (US)"),
    "en-gb": ("en", "gb", "English (UK)"),
    "de": ("de", "de", "Deutsch (Германия)"),
    "fr": ("fr", "fr", "Français (Франция)"),
    "es": ("es", "es", "Español (Испания)"),
    "it": ("it", "it", "Italiano (Италия)"),
    "pt": ("pt", "br", "Português (Бразилия)"),
    "tr": ("tr", "tr", "Türkçe (Турция)"),
    "pl": ("pl", "pl", "Polski (Польша)"),
    "uk": ("uk", "ua", "Українська (Украина)"),
    "kk": ("kk", "kz", "Қазақша (Казахстан)"),
}


def interactive_setup() -> dict:
    """Интерактивный ввод запросов и настроек."""
    print("\n" + "=" * 55)
    print("  Google PAA Parser — интерактивный режим")
    print("=" * 55)

    # --- Запросы ---
    print("\nВведите поисковые запросы (один на строку).")
    print("Или укажите путь к файлу (например: queries.txt)")
    print("Пустая строка — конец ввода.\n")

    first_line = input(">> ").strip()
    queries = []
    input_file = None

    if first_line and Path(first_line).is_file():
        input_file = first_line
        raw = Path(first_line).read_text(encoding="utf-8").splitlines()
        queries = [q.strip() for q in raw if q.strip() and not q.startswith("#")]
        print(f"  Загружено {len(queries)} запросов из {first_line}")
    else:
        if first_line:
            queries.append(first_line)
        while True:
            line = input(">> ").strip()
            if not line:
                break
            queries.append(line)

    if not queries:
        print("Нет запросов — выход.")
        sys.exit(0)

    # --- Язык + регион ---
    print(f"\nЯзык и регион Google:")
    print("  Популярные:")
    for key, (hl, gl, label) in POPULAR_LOCALES.items():
        print(f"    {key:6s} — {label} (hl={hl}, gl={gl})")
    print("  Или введите вручную: hl=xx gl=yy")

    locale_input = input(f"\nВыбор [ru]: ").strip().lower() or "ru"

    if locale_input in POPULAR_LOCALES:
        hl, gl, label = POPULAR_LOCALES[locale_input]
        print(f"  → {label}")
    elif "hl=" in locale_input or "gl=" in locale_input:
        # Парсим hl=xx gl=yy
        parts = locale_input.split()
        hl, gl = "en", "us"
        for p in parts:
            if p.startswith("hl="):
                hl = p[3:]
            elif p.startswith("gl="):
                gl = p[3:]
        print(f"  → hl={hl}, gl={gl}")
    else:
        # Пробуем как код языка
        hl = locale_input[:2]
        gl = locale_input[:2]
        print(f"  → hl={hl}, gl={gl}")

    # --- Вывод ---
    print(f"\n  Запросов: {len(queries)}")
    print(f"  Язык: {hl}, регион: {gl}")
    print()

    return {
        "queries": queries,
        "input_file": input_file,
        "hl": hl,
        "gl": gl,
    }


# ============================================================
# Captcha API solver (2captcha/rucaptcha/capguru protocol)
# ============================================================

def solve_captcha_via_api(driver, api_key: str, service: str = "2captcha") -> bool:
    """Решает reCAPTCHA v2 через API сервиса."""
    try:
        import requests
    except ImportError:
        log.error("pip install requests — для авто-решения капч")
        return False

    base = CAPTCHA_SERVICES.get(service, CAPTCHA_SERVICES["2captcha"])
    page_url = driver.current_url

    # Ищем sitekey reCAPTCHA
    sitekey = None
    try:
        el = driver.find_element(By.CSS_SELECTOR, "[data-sitekey]")
        sitekey = el.get_attribute("data-sitekey")
    except Exception:
        pass
    if not sitekey:
        try:
            src = driver.page_source
            import re
            m = re.search(r'data-sitekey="([^"]+)"', src)
            if m:
                sitekey = m.group(1)
            else:
                m = re.search(r"sitekey['\"]?\s*[:=]\s*['\"]([^'\"]+)", src)
                if m:
                    sitekey = m.group(1)
        except Exception:
            pass

    if not sitekey:
        log.warning("Не удалось найти sitekey reCAPTCHA — ручное решение")
        return False

    log.info(f"Отправляю капчу в {service} (sitekey={sitekey[:20]}...)")

    # Шаг 1: отправить задачу
    try:
        resp = requests.post(f"{base}/in.php", data={
            "key": api_key,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        }, timeout=30)
        data = resp.json()
        if data.get("status") != 1:
            log.error(f"Captcha API ошибка: {data.get('request', data)}")
            return False
        task_id = data["request"]
        log.info(f"Задача создана: {task_id}")
    except Exception as e:
        log.error(f"Captcha API ошибка: {e}")
        return False

    # Шаг 2: ждём решение (макс. 180с)
    for attempt in range(36):
        time.sleep(5)
        try:
            resp = requests.get(f"{base}/res.php", params={
                "key": api_key,
                "action": "get",
                "id": task_id,
                "json": 1,
            }, timeout=15)
            data = resp.json()
            if data.get("status") == 1:
                token = data["request"]
                log.info("Капча решена через API!")
                break
            if data.get("request") == "CAPCHA_NOT_READY":
                continue
            log.error(f"Captcha API: {data.get('request', data)}")
            return False
        except Exception as e:
            log.warning(f"Captcha poll ошибка: {e}")
            continue
    else:
        log.error("Таймаут решения капчи (180с)")
        return False

    # Шаг 3: вставить токен в форму
    try:
        driver.execute_script(f"""
            var el = document.getElementById('g-recaptcha-response');
            if (!el) {{
                el = document.querySelector('[name="g-recaptcha-response"]');
            }}
            if (el) {{
                el.style.display = 'block';
                el.value = '{token}';
            }}
            // Попробовать callback
            try {{
                var cb = document.querySelector('[data-callback]');
                if (cb) {{
                    var fn = cb.getAttribute('data-callback');
                    if (fn && window[fn]) window[fn]('{token}');
                }}
            }} catch(e) {{}}
        """)
        # Отправить форму
        try:
            form = driver.find_element(By.CSS_SELECTOR, "form[action*='sorry']")
            form.submit()
        except Exception:
            driver.execute_script("document.querySelector('form').submit();")

        time.sleep(3)

        if not is_captcha(driver):
            log.info("Капча пройдена, продолжаю парсинг.")
            return True
        else:
            log.warning("Капча всё ещё на странице после вставки токена.")
            return False
    except Exception as e:
        log.error(f"Ошибка вставки токена: {e}")
        return False


# ============================================================
# Driver
# ============================================================

def create_driver(headless: bool = False, lang: str = "en") -> webdriver.Chrome:
    """Создаёт Chrome driver с anti-detection."""
    options = webdriver.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument(f"--lang={lang}")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Accept-Language header
    prefs = {"intl.accept_languages": f"{lang},{lang[:2]}"}
    options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    driver.set_page_load_timeout(30)
    return driver


# ============================================================
# Cookie consent
# ============================================================

def accept_cookies(driver) -> bool:
    """Принимает Google cookie consent (EU)."""
    for selector in [COOKIE_BTN] + COOKIE_BTN_ALT:
        try:
            btn = WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            btn.click()
            time.sleep(1)
            log.info("Cookie consent принят")
            return True
        except Exception:
            continue
    log.debug("Cookie consent не обнаружен")
    return False


# ============================================================
# Captcha detection
# ============================================================

def is_captcha(driver) -> bool:
    """Проверяет наличие капчи на странице."""
    url = driver.current_url.lower()
    if "sorry/index" in url or "/recaptcha/" in url:
        return True
    try:
        src = driver.page_source[:5000].lower()
        return "unusual traffic" in src or "captcha" in src
    except Exception:
        return False


def wait_for_captcha_resolution(
    driver, timeout: int = 300,
    captcha_api_key: str = "", captcha_service: str = "2captcha",
) -> bool:
    """Решает капчу: через API (если ключ есть) или ждёт ручного решения."""
    # Попробовать авто-решение через API
    if captcha_api_key:
        log.info("Пробую авто-решение через API...")
        if solve_captcha_via_api(driver, captcha_api_key, captcha_service):
            return True
        log.warning("API не помогло, жду ручное решение...")

    log.warning("=" * 50)
    log.warning("CAPTCHA! Реши вручную в браузере.")
    log.warning(f"Жду до {timeout} секунд...")
    log.warning("=" * 50)

    start = time.time()
    while time.time() - start < timeout:
        time.sleep(5)
        if not is_captcha(driver):
            log.info("Капча решена, продолжаю.")
            return True
    log.error("Таймаут ожидания капчи.")
    return False


# ============================================================
# PAA extraction — ядро
# ============================================================

def find_paa_container(driver):
    """Находит контейнер PAA (с fallback-селекторами)."""
    for selector in [PAA_CONTAINER] + PAA_CONTAINER_ALT:
        try:
            return WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
        except Exception:
            continue
    return None


def extract_single_answer(pair_element) -> dict:
    """Извлекает вопрос и ответ из одного pair-контейнера."""
    q_elems = pair_element.find_elements(By.CSS_SELECTOR, QUESTION_SEL)
    a_elems = pair_element.find_elements(By.CSS_SELECTOR, ANSWER_SEL)
    question = q_elems[0].text.strip() if q_elems else ""
    answer = a_elems[0].text.strip() if a_elems else ""
    return {"question": question, "answer": answer}


def click_and_extract(driver, paa, max_clicks: int) -> list[dict]:
    """
    Кликает по вопросам PAA и СРАЗУ читает ответ после каждого клика.
    Текст вопроса берём из pair-контейнера (yEVEwb), не из кнопки (pcRaIe — пустая).
    """
    results = []
    seen_questions = set()
    no_new = 0
    clicked = 0

    for i in range(max_clicks + 15):  # запас на пропуски
        if clicked >= max_clicks:
            break

        buttons = paa.find_elements(By.CSS_SELECTOR, QUESTION_BTN)
        if i >= len(buttons):
            no_new += 1
            if no_new > 3:
                break
            time.sleep(1)
            continue
        no_new = 0

        # Читаем текст вопроса из pair-контейнера (не из кнопки — она пустая)
        pairs_before = paa.find_elements(By.CSS_SELECTOR, PAIR_CONTAINER)
        q_text = ""
        if i < len(pairs_before):
            try:
                q_elems = pairs_before[i].find_elements(By.CSS_SELECTOR, QUESTION_SEL)
                q_text = q_elems[0].text.strip() if q_elems else ""
            except Exception:
                pass
            if not q_text:
                try:
                    q_text = pairs_before[i].text.strip().split("\n")[0]
                except Exception:
                    pass

        if q_text in seen_questions:
            continue

        # Кликаем
        btn = buttons[i]
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", btn
            )
            time.sleep(0.2)
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            try:
                ActionChains(driver).move_to_element(btn).click().perform()
            except Exception:
                continue

        clicked += 1
        time.sleep(random.uniform(1.2, 2.2))

        # Читаем ответ СРАЗУ после клика из всех pairs
        pairs_after = paa.find_elements(By.CSS_SELECTOR, PAIR_CONTAINER)

        # Ищем pair у которого появился ответ (последний кликнутый)
        best_q = q_text
        best_a = ""
        for pair in pairs_after:
            qa = extract_single_answer(pair)
            if qa["answer"] and qa["question"]:
                # Если это новый вопрос с ответом и совпадает (или мы не знали текст)
                if qa["question"] == q_text or not q_text:
                    best_q = qa["question"]
                    best_a = qa["answer"]
                    break
                # Или это вопрос, которого мы ещё не видели
                if qa["question"] not in seen_questions:
                    best_q = qa["question"]
                    best_a = qa["answer"]

        if best_q and best_q not in seen_questions:
            seen_questions.add(best_q)
            results.append({"question": best_q, "answer": best_a})

    return results


def parse_query(
    driver, query: str, hl: str, gl: str, max_clicks: int,
    captcha_api_key: str = "", captcha_service: str = "2captcha",
) -> list[dict]:
    """Парсит PAA для одного запроса."""
    url = f"https://www.google.com/search?q={quote_plus(query)}&hl={hl}&gl={gl}"
    driver.get(url)
    time.sleep(random.uniform(2, 3))

    # Captcha check
    if is_captcha(driver):
        resolved = wait_for_captcha_resolution(
            driver, captcha_api_key=captcha_api_key, captcha_service=captcha_service
        )
        if not resolved:
            return []

    # Find PAA
    paa = find_paa_container(driver)
    if not paa:
        log.warning(f"PAA не найден для '{query}'")
        return []

    # Click & extract
    results = click_and_extract(driver, paa, max_clicks)
    return results


# ============================================================
# Checkpoint (save/resume)
# ============================================================

def save_checkpoint(done_queries: list[str], all_results: list[dict]):
    """Сохраняет прогресс для --resume."""
    data = {
        "done": done_queries,
        "results": all_results,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    CHECKPOINT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_checkpoint() -> tuple[set[str], list[dict]]:
    """Загружает чекпоинт."""
    if not CHECKPOINT_FILE.exists():
        return set(), []
    try:
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        log.info(f"Чекпоинт загружен: {len(data['done'])} запросов, {len(data['results'])} результатов")
        return set(data["done"]), data["results"]
    except Exception as e:
        log.warning(f"Чекпоинт повреждён: {e}")
        return set(), []


def clear_checkpoint():
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


# ============================================================
# Export
# ============================================================

def export_xlsx(results: list[dict], filepath: str):
    """Экспорт в XLSX."""
    wb = Workbook()
    ws = wb.active
    ws.title = "PAA Results"
    ws.append(["Исходный запрос", "Вопрос", "Ответ"])

    for r in results:
        ws.append([r["query"], r["question"], r["answer"]])

    # Ширины
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 60
    ws.column_dimensions["C"].width = 80

    wb.save(filepath)
    log.info(f"XLSX: {filepath} ({len(results)} строк)")


def export_json(results: list[dict], filepath: str):
    """Экспорт в JSON."""
    Path(filepath).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"JSON: {filepath} ({len(results)} строк)")


# ============================================================
# Main
# ============================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Google PAA Parser v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Примеры:
  python google_paa_parser.py                           # интерактивный режим
  python google_paa_parser.py -i queries.txt --hl ru --gl ru
  python google_paa_parser.py --captcha-key YOUR_KEY    # авто-решение капч
  python google_paa_parser.py --headless --clicks 20""",
    )
    p.add_argument("-i", "--input", default=None,
                   help="Файл с запросами (по одному на строку)")
    p.add_argument("-o", "--output", default=str(SCRIPT_DIR / "results.xlsx"),
                   help="Выходной файл (.xlsx или .json)")
    p.add_argument("--hl", default=None, help="Язык Google (en, ru, es, ...)")
    p.add_argument("--gl", default=None, help="Регион Google (us, ru, uk, ...)")
    p.add_argument("--clicks", type=int, default=15,
                   help="Макс. кликов по вопросам на запрос (default: 15)")
    p.add_argument("--headless", action="store_true", help="Headless режим (без окна)")
    p.add_argument("--resume", action="store_true", help="Продолжить с чекпоинта")
    p.add_argument("--pause-min", type=float, default=10,
                   help="Мин. пауза между запросами, сек (default: 10)")
    p.add_argument("--pause-max", type=float, default=20,
                   help="Макс. пауза между запросами, сек (default: 20)")

    # Captcha API
    cap = p.add_argument_group("captcha", "Авто-решение капч через API")
    cap.add_argument("--captcha-key", default=os.environ.get("CAPTCHA_API_KEY", ""),
                     help="API-ключ для решения капч (или env CAPTCHA_API_KEY)")
    cap.add_argument("--captcha-service", default="2captcha",
                     choices=list(CAPTCHA_SERVICES.keys()),
                     help="Сервис решения капч (default: 2captcha)")

    return p.parse_args()


def main():
    args = parse_args()

    # Определяем источник запросов и локаль
    queries = []
    hl = args.hl
    gl = args.gl

    # Интерактивный режим: нет -i и queries.txt нет, или hl/gl не указаны
    default_queries_path = SCRIPT_DIR / "queries.txt"
    need_interactive = (args.input is None and not default_queries_path.exists())

    if need_interactive:
        setup = interactive_setup()
        queries = setup["queries"]
        if not hl:
            hl = setup["hl"]
        if not gl:
            gl = setup["gl"]
    else:
        # Из файла (CLI или дефолт)
        input_path = Path(args.input) if args.input else default_queries_path
        if not input_path.exists():
            log.error(f"Файл не найден: {input_path}")
            sys.exit(1)
        queries = [
            line.strip()
            for line in input_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    # Дефолты если не указали
    hl = hl or "en"
    gl = gl or "us"

    log.info(f"Запросов: {len(queries)} | hl={hl} gl={gl} clicks={args.clicks}")
    if args.captcha_key:
        log.info(f"Captcha API: {args.captcha_service} (ключ задан)")

    # Resume
    done_queries, all_results = set(), []
    if args.resume:
        done_queries, all_results = load_checkpoint()

    remaining = [q for q in queries if q not in done_queries]
    if not remaining:
        log.info("Все запросы уже обработаны.")
        if all_results:
            export_xlsx(all_results, args.output)
        return

    log.info(f"Осталось: {len(remaining)} запросов")

    # Start driver (один на всю сессию)
    driver = create_driver(headless=args.headless, lang=hl)

    # Accept cookies один раз
    driver.get(f"https://www.google.com/?hl={hl}&gl={gl}")
    time.sleep(2)
    accept_cookies(driver)

    # Dedup set
    seen_questions = set()
    for r in all_results:
        seen_questions.add(r["question"])

    captcha_count = 0
    MAX_CAPTCHAS = 3  # после 3 капч подряд — стоп

    try:
        for i, query in enumerate(remaining):
            log.info(f"[{len(done_queries)+1}/{len(queries)}] '{query}'")
            t0 = time.time()

            results = parse_query(
                driver, query, hl, gl, args.clicks,
                captcha_api_key=args.captcha_key,
                captcha_service=args.captcha_service,
            )

            # Captcha tracking
            if not results and is_captcha(driver):
                captcha_count += 1
                log.warning(f"Captcha #{captcha_count}")
                if captcha_count >= MAX_CAPTCHAS:
                    log.error(f"Стоп: {MAX_CAPTCHAS} капч подряд. Сохраняю чекпоинт.")
                    break
            else:
                captcha_count = 0

            # Deduplicate
            new_count = 0
            for qa in results:
                if qa["question"] not in seen_questions:
                    seen_questions.add(qa["question"])
                    all_results.append({
                        "query": query,
                        "question": qa["question"],
                        "answer": qa["answer"],
                    })
                    new_count += 1

            with_answer = sum(1 for qa in results if qa["answer"])
            elapsed = round(time.time() - t0, 1)
            log.info(
                f"  → {len(results)} вопросов ({with_answer} с ответом), "
                f"{new_count} новых, {elapsed}s"
            )

            done_queries.add(query)

            # Checkpoint каждые 5 запросов
            if (len(done_queries)) % 5 == 0:
                save_checkpoint(list(done_queries), all_results)

            # Пауза
            if i < len(remaining) - 1:
                pause = random.uniform(args.pause_min, args.pause_max)
                log.info(f"  Пауза {pause:.0f}s...")
                time.sleep(pause)

    except KeyboardInterrupt:
        log.warning("Прервано пользователем. Сохраняю чекпоинт...")
    except Exception as e:
        log.error(f"Ошибка: {e}. Сохраняю чекпоинт...")
    finally:
        driver.quit()
        save_checkpoint(list(done_queries), all_results)

    # Export
    if all_results:
        output = args.output
        if output.endswith(".json"):
            export_json(all_results, output)
        else:
            export_xlsx(all_results, output)

        # Всегда сохраняем и JSON для надёжности
        json_path = str(Path(output).with_suffix(".json"))
        export_json(all_results, json_path)

    # Summary
    total_q = len(all_results)
    with_a = sum(1 for r in all_results if r["answer"])
    log.info("=" * 50)
    log.info(f"ИТОГО: {total_q} вопросов, {with_a} с ответом ({round(with_a/max(total_q,1)*100)}%)")
    log.info(f"Обработано запросов: {len(done_queries)}/{len(queries)}")
    log.info("=" * 50)

    clear_checkpoint()


if __name__ == "__main__":
    main()
