#!/usr/bin/env python3
"""
Google PAA (People Also Ask) Parser v2.

Собирает вопросы и ответы из блока "Люди также спрашивают" Google.
Читает ответ сразу после клика (не в конце), переиспользует драйвер,
поддерживает гео/язык, чекпоинты, headless, дедупликацию.

Использование:
  python3 google-qa-parser.py                        # queries.txt → results.xlsx
  python3 google-qa-parser.py -i my_queries.txt      # свой файл
  python3 google-qa-parser.py --hl ru --gl ru        # русский Google
  python3 google-qa-parser.py --clicks 20            # больше вопросов
  python3 google-qa-parser.py --headless             # без окна браузера
  python3 google-qa-parser.py --resume               # продолжить после сбоя
"""
import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

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


def wait_for_captcha_resolution(driver, timeout: int = 300) -> bool:
    """Ждёт ручного решения капчи (в non-headless) или таймаутит."""
    log.warning("=" * 50)
    log.warning("CAPTCHA DETECTED! Реши вручную в браузере.")
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


def parse_query(driver, query: str, hl: str, gl: str, max_clicks: int) -> list[dict]:
    """Парсит PAA для одного запроса."""
    url = f"https://www.google.com/search?q={quote_plus(query)}&hl={hl}&gl={gl}"
    driver.get(url)
    time.sleep(random.uniform(2, 3))

    # Captcha check
    if is_captcha(driver):
        resolved = wait_for_captcha_resolution(driver)
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
    p = argparse.ArgumentParser(description="Google PAA Parser v2")
    p.add_argument("-i", "--input", default=str(SCRIPT_DIR / "queries.txt"),
                   help="Файл с запросами (по одному на строку)")
    p.add_argument("-o", "--output", default=str(SCRIPT_DIR / "results.xlsx"),
                   help="Выходной файл (.xlsx или .json)")
    p.add_argument("--hl", default="en", help="Язык Google (en, ru, es, ...)")
    p.add_argument("--gl", default="us", help="Гео Google (us, ru, uk, ...)")
    p.add_argument("--clicks", type=int, default=15,
                   help="Макс. кликов по вопросам на запрос (default: 15)")
    p.add_argument("--headless", action="store_true", help="Headless режим (без окна)")
    p.add_argument("--resume", action="store_true", help="Продолжить с чекпоинта")
    p.add_argument("--pause-min", type=float, default=10,
                   help="Мин. пауза между запросами, сек (default: 10)")
    p.add_argument("--pause-max", type=float, default=20,
                   help="Макс. пауза между запросами, сек (default: 20)")
    return p.parse_args()


def main():
    args = parse_args()

    # Read queries
    input_path = Path(args.input)
    if not input_path.exists():
        log.error(f"Файл не найден: {input_path}")
        sys.exit(1)

    queries = [line.strip() for line in input_path.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")]

    log.info(f"Запросов: {len(queries)} | hl={args.hl} gl={args.gl} clicks={args.clicks}")

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
    driver = create_driver(headless=args.headless, lang=args.hl)

    # Accept cookies один раз
    driver.get(f"https://www.google.com/?hl={args.hl}&gl={args.gl}")
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

            results = parse_query(driver, query, args.hl, args.gl, args.clicks)

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
