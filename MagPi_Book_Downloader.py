#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📚 Raspberry Pi Books PDF Downloader (Playwright Edition)
Скачивает все бесплатные книги с:
https://magazine.raspberrypi.com/books

Требуется: pip install playwright beautifulsoup4 requests
           playwright install chromium
"""

import os
import re
import time
import logging
import requests
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, Browser, Download, TimeoutError as PlaywrightTimeout
from playwright._impl._errors import Error as PlaywrightError

# ==================== НАСТРОЙКИ ====================
BASE_URL = "https://magazine.raspberrypi.com/books"
OUTPUT_DIR = Path("rpi_books_pdfs")
DOWNLOAD_TIMEOUT = 30_000  # мс
PAGE_LOAD_TIMEOUT = 20_000  # мс
REQUEST_DELAY = 2  # секунды между запросами
MIN_FILE_SIZE = 1024 * 10  # 10 KB минимум для валидации
LOG_FILE = OUTPUT_DIR / "download_log.txt"
HEADLESS = True  # False для отладки с видимым браузером
# ===================================================


# 🔹 Настройка логирования
def setup_logging(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(output_dir / "download_log.txt", encoding='utf-8', mode='a'),
            logging.StreamHandler()
        ]
    )


# 🔹 Проверка: уже скачан?
def is_already_downloaded(output_dir: Path, book_slug: str) -> tuple[bool, Path]:
    # slug может быть числом или строкой типа "code-club-vol1"
    safe_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', book_slug)
    filename = f"RaspberryPi_Book_{safe_slug}.pdf"
    filepath = output_dir / filename
    
    if filepath.exists():
        size = filepath.stat().st_size
        if size >= MIN_FILE_SIZE:
            return True, filepath
        else:
            logging.warning(f"⚠️  Файл {filename} повреждён ({size} байт), будет перезапущен")
            try:
                filepath.unlink()
            except OSError:
                pass
    return False, filepath


# 🔹 Скачивание через requests (надёжнее)
def download_pdf_direct(pdf_url: str, filepath: Path) -> bool:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://magazine.raspberrypi.com/',
            'Accept': 'application/pdf,*/*',
        }
        response = requests.get(pdf_url, headers=headers, stream=True, timeout=30)
        if response.status_code == 200:
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            if filepath.stat().st_size >= MIN_FILE_SIZE:
                return True
            filepath.unlink()
        logging.warning(f"   ❌ HTTP {response.status_code}")
        return False
    except Exception as e:
        logging.warning(f"   ❌ Ошибка requests: {e}")
        return False


# 🔹 Извлечение безопасного имени книги из URL
def extract_book_slug(url: str) -> str:
    # /books/123 или /books/my-awesome-book
    match = re.search(r'/books/([^/]+)', url)
    if match:
        slug = match.group(1)
        # Если это число — оставляем как есть, иначе — санитайзим
        if slug.isdigit():
            return slug
        return re.sub(r'[^a-zA-Z0-9_-]', '_', slug)
    return "unknown"


# 🔹 Сбор всех ссылок на книги со всех страниц пагинации
def get_all_book_links(page: Page, base_url: str) -> list[str]:
    logging.info(f"📋 Сбор ссылок на книги с {base_url}...")
    all_books = set()
    visited_pages = set()
    next_url = base_url
    page_num = 1
    
    while next_url and next_url not in visited_pages:
        visited_pages.add(next_url)
        logging.info(f"   📄 Страница #{page_num}: {next_url}")
        
        try:
            page.goto(next_url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
        except PlaywrightTimeout:
            logging.warning(f"⏱️ Таймаут загрузки страницы #{page_num}")
            page.goto(next_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        
        time.sleep(1)  # Ждём рендеринга
        soup = BeautifulSoup(page.content(), 'html.parser')
        
        # 🔹 Извлекаем ссылки на книги (формат: /books/...)
        for link in soup.find_all('a', href=True):
            href = link['href']
            if re.match(r'^/books/[^/]+/?$', href) and '/books/' in href and href != '/books':
                clean_href = href.rstrip('/')
                full_url = f"https://magazine.raspberrypi.com{clean_href}"
                all_books.add(full_url)
        
        # 🔹 Поиск следующей страницы
        next_link = None
        
        # Вариант 1: по тексту
        for link in soup.find_all('a', href=True):
            text = link.get_text(strip=True).lower()
            if text in ['next', '»', 'next page', '→'] and link['href']:
                next_link = link['href']
                break
        
        # Вариант 2: по классам пагинации
        if not next_link:
            pagination = soup.find('nav', class_='pagination') or soup.find('ul', class_='pagination')
            if pagination:
                next_li = pagination.find('li', class_='next')
                if next_li:
                    next_tag = next_li.find('a', href=True)
                    if next_tag:
                        next_link = next_tag['href']
        
        # Вариант 3: rel="next"
        if not next_link:
            next_tag = soup.find('a', rel='next')
            if next_tag and next_tag.get('href'):
                next_link = next_tag['href']
        
        # Формируем URL следующей страницы
        if next_link:
            next_url = urljoin(base_url, next_link)
            if next_url in visited_pages or not next_url.startswith('https://magazine.raspberrypi.com/books'):
                next_url = None
            else:
                page_num += 1
        else:
            logging.info("   ✅ Последняя страница достигнута")
            next_url = None
        
        time.sleep(1)
    
    # Сортировка: сначала числовые ID (новые), потом строковые слаги
    def sort_key(url):
        slug = extract_book_slug(url)
        if slug.isdigit():
            return (0, -int(slug))  # Новые сначала
        return (1, slug)  # Строковые слаги после
    
    sorted_books = sorted(all_books, key=sort_key)
    logging.info(f"✅ Всего найдено книг: {len(sorted_books)}")
    return sorted_books


# 🔹 Скачивание одной книги
def download_book_pdf(page: Page, book_url: str, output_dir: Path) -> bool:
    book_slug = extract_book_slug(book_url)
    safe_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', book_slug)
    
    # 🔹 Проверка на уже скачанную
    downloaded, filepath = is_already_downloaded(output_dir, book_slug)
    if downloaded:
        logging.info(f"⏭️  Книга '{book_slug}' уже скачана: {filepath.name}")
        return True
    
    filename = f"RaspberryPi_Book_{safe_slug}.pdf"
    logging.info(f"📥 Обработка книги '{book_slug}'...")
    
    try:
        # 🔹 Шаг 1: Страница книги
        page.goto(book_url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_timeout(1000)
        
        # 🔹 Шаг 2: Клик по "free PDF download"
        pdf_link_clicked = False
        
        # Ищем по тексту ссылки (регистронезависимо)
        pdf_link = page.locator('a:has-text("free PDF download"), a:has-text("Free PDF Download"), a:has-text("Download PDF")').first
        if pdf_link.count() > 0 and pdf_link.is_visible():
            pdf_link.click()
            pdf_link_clicked = True
            logging.info("   🔗 Клик по ссылке 'free PDF download'")
        
        # Альтернатива: по href с /pdf
        if not pdf_link_clicked:
            links = page.locator('a[href*="/pdf"][href*="books"]')
            if links.count() > 0:
                links.first.click()
                pdf_link_clicked = True
                logging.info("   🔗 Клик по ссылке с /pdf в href")
        
        if not pdf_link_clicked:
            logging.warning(f"⚠️  Не найдена ссылка на PDF для книги '{book_slug}'")
            return False
        
        page.wait_for_timeout(1500)
        
        # 🔹 Шаг 3: Страница /pdf — кнопка "No thanks..."
        try:
            no_thanks_btn = page.locator('a:has-text("No thanks, take me to the free PDF")').first
            no_thanks_btn.wait_for(state="visible", timeout=10000)
            
            # 🔹 Шаг 4: Получаем целевой URL
            target_url = no_thanks_btn.get_attribute('href')
            
            if target_url and target_url.lower().endswith('.pdf'):
                # 🎯 Прямая ссылка — скачиваем через requests
                logging.info(f"   📄 Прямая ссылка на PDF найдена")
                if download_pdf_direct(target_url, filepath):
                    logging.info(f"   ✅ Скачан: {filename}")
                    return True
                else:
                    logging.warning(f"   ❌ Не удалось скачать по прямой ссылке")
            else:
                # 🖱️ Кликаем и используем expect_download
                logging.info(f"   🖱️  Ожидание скачивания через браузер...")
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as download_info:
                    no_thanks_btn.click()
                
                download: Download = download_info.value
                download.save_as(filepath)
                
                # Проверяем результат
                if filepath.exists() and filepath.stat().st_size >= MIN_FILE_SIZE:
                    logging.info(f"   ✅ Скачан: {filename}")
                    return True
                else:
                    logging.warning(f"⚠️  Файл пустой или не скачался")
                    if filepath.exists():
                        filepath.unlink()
                    return False
                    
        except PlaywrightTimeout:
            logging.warning(f"⏱️  Таймаут ожидания кнопки 'No thanks' для книги '{book_slug}'")
            return False
                
    except PlaywrightError as e:
        logging.warning(f"🌐 Playwright ошибка для книги '{book_slug}': {e}")
        return False
    except Exception as e:
        logging.error(f"❌ Неожиданная ошибка при скачивании '{book_slug}': {type(e).__name__}: {e}")
        return False


# 🔹 Главная функция
def main():
    setup_logging(OUTPUT_DIR)
    logging.info("🚀 Raspberry Pi Books PDF Downloader (Playwright Edition)")
    logging.info(f"📁 Папка для скачивания: {OUTPUT_DIR.resolve()}")
    logging.info(f"🔍 Headless режим: {'✅ Да' if HEADLESS else '❌ Нет (отладка)'}")
    logging.info("-" * 60)
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    with sync_playwright() as p:
        # 🔹 Запуск браузера
        browser: Browser = p.chromium.launch(
            headless=HEADLESS,
            args=['--disable-blink-features=AutomationControlled']
        )
        
        # 🔹 Контекст с настройками скачивания
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            accept_downloads=True
        )
        context.set_default_timeout(PAGE_LOAD_TIMEOUT)
        
        page: Page = context.new_page()
        
        try:
            # 🔹 Сбор всех ссылок
            book_links = get_all_book_links(page, BASE_URL)
            if not book_links:
                logging.error("❌ Не найдено ни одной книги. Проверьте доступ к сайту.")
                return
            
            # 🔹 Скачивание с поддержкой возобновления
            success = fail = skip = 0
            
            for i, url in enumerate(book_links, 1):
                book_slug = extract_book_slug(url)
                
                # Предварительная проверка
                if is_already_downloaded(OUTPUT_DIR, book_slug)[0]:
                    skip += 1
                    logging.info(f"[{i:3d}/{len(book_links)}] ⏭️  Пропущено '{book_slug}' (уже скачано)")
                    continue
                
                logging.info(f"[{i:3d}/{len(book_links)}] Обработка книги '{book_slug}'")
                
                if download_book_pdf(page, url, OUTPUT_DIR):
                    success += 1
                else:
                    fail += 1
                
                time.sleep(REQUEST_DELAY)
            
            # 🔹 Итоги
            logging.info("\n" + "=" * 60)
            logging.info(f"🎉 Завершено!")
            logging.info(f"✅ Успешно скачано: {success}")
            logging.info(f"⏭️  Пропущено (уже есть): {skip}")
            logging.info(f"❌ Ошибки:  {fail}")
            logging.info(f"📁 Файлы в: {OUTPUT_DIR.resolve()}")
            logging.info(f"📝 Лог сохранён: {LOG_FILE}")
            
        except KeyboardInterrupt:
            logging.warning("\n⚠️  Прервано пользователем.")
        except Exception as e:
            logging.error(f"\n❌ Критическая ошибка: {e}", exc_info=True)
        finally:
            context.close()
            browser.close()
            logging.info("🔚 Браузер закрыт.")


# 🔧 Тестовый режим: одна книга
# if __name__ == "__main__":
#     # Для теста:
#     # 1. Замени BASE_URL на конкретную книгу
#     # 2. В main() замени get_all_book_links на: book_links = [BASE_URL]
#     main()


if __name__ == "__main__":
    main()
