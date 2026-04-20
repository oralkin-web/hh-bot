#!/usr/bin/env python3
"""
HH.ru Design Director Job Bot with Claude AI
Использует RSS-ленту hh.ru вместо заблокированного API
"""

import os
import json
import time
import logging
import asyncio
import re
import schedule
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import requests
import anthropic
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# RSS-ленты hh.ru — одна на каждый поисковый запрос
RSS_QUERIES = [
    "дизайн директор",
    "Design Director",
    "head of design",
    "руководитель дизайн студии",
    "руководитель дизайн группы",
    "руководитель отдела дизайна",
    "руководитель департамента дизайна",
    "директор по дизайну",
]

AREA = "1"  # 1 = Москва
CHECK_INTERVAL_MINUTES = 60

MY_PROFILE = """
Константин, 43 года, Москва. Опыт 24+ года в дизайне и креативном управлении.

КОГО ИЩУ:
- Операционный директор дизайн-студии / Creative Operations Director
- Дизайн-директор / Design Director / Head of Design
- Руководитель дизайн-студии или креативного департамента
- Рассматриваю крупный бизнес, продуктовые компании, retail, fintech, tech
- Только офис или гибрид в Москве
- Полная занятость
- Руководитель дизайн-группы / дизайн-отдела в крупном девелопере или корпорации

ОПЫТ:
- Операционный директор дизайн-студии Азбуки Вкуса (март 2025–сейчас)
  4 команды: дизайн коммуникаций, упаковка, фотостудия, копирайтеры
- БКС Мир инвестиций (3.5 года): создал департамент дизайна с нуля,
  ребрендинг, дизайн-система, федеральные РК
- Лаборатория Касперского: Senior Designer / PM, глобальный ребрендинг
- BBDO, Publicis: арт-директор, международные бренды
- 13 лет Крик Дизайн: Креативный директор, IKEA, VISA, VW, AWWWARDS

КОМПЕТЕНЦИИ:
- Управление командами до 20+ человек (дизайнеры, копирайтеры, фотографы)
- Найм, мотивация, развитие персонала
- Внедрение AI-инструментов в дизайн-процессы
- Бюджетирование, тендеры, подрядчики
- Дизайн-системы, бренд-гайдлайны, фирменный стиль
- Figma, Яндекс Трекер, английский B2

НЕ ПОДХОДИТ:
- Арт-директор или креативный директор без управленческой функции
- Рядовой дизайнер без управления командой
- Чисто IT без креатива
- Аутсорс-агентства низкого уровня (агентства уровня ONY, Superheroes, Plenum — подходят)
- Типографии
- Вакансии вне Москвы или полностью удалённые
- Стартапы без бюджета и команды
"""

MIN_SCORE = 65

SEEN_FILE   = Path("seen_vacancies.json")
PAUSED_FILE = Path("paused.flag")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def is_paused() -> bool:
    return PAUSED_FILE.exists()


def set_paused(state: bool):
    if state:
        PAUSED_FILE.touch()
    else:
        PAUSED_FILE.unlink(missing_ok=True)


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))


def build_rss_url(query: str) -> str:
    encoded = quote(query)
    return f"https://hh.ru/search/vacancy/rss?text={encoded}&area={AREA}&order_by=publication_time"


def fetch_rss(url: str) -> list:
    """Получаем вакансии из одной RSS-ленты."""
    try:
        r = requests.get(url, headers=RSS_HEADERS, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link  = item.findtext("link", "").strip()
            desc  = item.findtext("description", "").strip()
            pub   = item.findtext("pubDate", "").strip()

            # Извлекаем ID вакансии из URL
            match = re.search(r"/vacancy/(\d+)", link)
            if not match:
                continue
            vacancy_id = match.group(1)

            # Парсим название и компанию из title (формат: "Должность, Компания")
            parts = title.split(", ", 1)
            name    = parts[0].strip() if parts else title
            company = parts[1].strip() if len(parts) > 1 else "—"

            # Убираем HTML из описания
            desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()

            items.append({
                "id":      vacancy_id,
                "name":    name,
                "company": company,
                "url":     link,
                "desc":    desc_clean[:1000],
                "pub":     pub,
            })
        return items
    except Exception as e:
        log.error(f"Ошибка RSS ({url[:60]}...): {e}")
        return []


def fetch_all_vacancies() -> list:
    """Собираем вакансии из всех RSS-лент, убираем дубли."""
    seen_ids = set()
    all_items = []
    for query in RSS_QUERIES:
        url   = build_rss_url(query)
        items = fetch_rss(url)
        for item in items:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_items.append(item)
        log.info(f"RSS '{query}': {len(items)} вакансий")
        time.sleep(2)  # пауза между запросами
    log.info(f"Итого уникальных: {len(all_items)}")
    return all_items


def score_vacancy_with_claude(vacancy: dict) -> dict:
    vacancy_text = (
        f"Название: {vacancy.get('name','—')}\n"
        f"Компания: {vacancy.get('company','—')}\n"
        f"Описание: {vacancy.get('desc','—')}\n"
        f"Ссылка: {vacancy.get('url','')}"
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            system='Ты помощник по поиску работы. Оцени вакансию. Отвечай СТРОГО JSON без markdown:\n{"score":<0-100>,"reason":"<1 предложение>","pros":["..."],"cons":["..."]}',
            messages=[{"role": "user", "content": f"Профиль:\n{MY_PROFILE}\n\nВакансия:\n{vacancy_text}\n\nОцени совпадение."}]
        )
        text = re.sub(r"^```json\s*|```$", "", msg.content[0].text.strip(), flags=re.MULTILINE).strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {"score": 50, "reason": "Не удалось разобрать ответ AI", "pros": [], "cons": []}
    except Exception as e:
        log.error(f"Claude ошибка: {e}")
        return {"score": 0, "reason": f"Ошибка AI: {e}", "pros": [], "cons": []}


def esc(t: str) -> str:
    chars = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in chars else c for c in str(t))


def build_message(vacancy: dict, ai: dict) -> str:
    score = ai.get("score", 0)
    emoji = "🟢" if score >= 80 else "🟡" if score >= 65 else "🔴"

    msg = (
        f"{emoji} *{esc(vacancy.get('name','—'))}*\n"
        f"🏢 {esc(vacancy.get('company','—'))}\n\n"
        f"🤖 *AI\\-оценка: {score}/100*\n"
        f"_{esc(ai.get('reason',''))}_\n"
    )
    for p in ai.get("pros", []): msg += f"\n  ✅ {esc(p)}"
    for c in ai.get("cons", []): msg += f"\n  ❌ {esc(c)}"
    msg += f"\n\n🔗 [Открыть вакансию]({vacancy.get('url','')})"
    return msg


async def check_and_notify(bot: Bot):
    if is_paused():
        log.info("⏸ Бот на паузе")
        return

    log.info("🔍 Проверяю вакансии через RSS...")
    seen      = load_seen()
    vacancies = fetch_all_vacancies()
    new       = [v for v in vacancies if v["id"] not in seen]
    log.info(f"Новых: {len(new)}")

    if not new:
        return

    sent = 0
    for v in new:
        seen.add(v["id"])
        log.info(f"  → {v.get('name')} / {v.get('company','?')}")
        ai    = score_vacancy_with_claude(v)
        score = ai.get("score", 0)
        log.info(f"     {score}/100")

        if score >= MIN_SCORE:
            try:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=build_message(v, ai),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True
                )
                sent += 1
            except Exception as e:
                log.error(f"Telegram ошибка: {e}")
        time.sleep(1.5)

    save_seen(seen)
    log.info(f"Отправлено: {sent}")

    if sent == 0:
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"🔍 Проверено {len(new)} вакансий — подходящих нет\\. Слежу дальше\\.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "⏸ на паузе" if is_paused() else "✅ активен"
    await update.message.reply_text(
        f"👋 Привет, Константин\\!\n\n"
        f"Статус: {status}\n\n"
        f"Слежу за вакансиями Design Director / Head of Design на hh\\.ru через RSS\\.\n\n"
        f"/check — проверить прямо сейчас\n"
        f"/pause — поставить на паузу\n"
        f"/resume — возобновить\n"
        f"/status — текущие настройки\n"
        f"/clear — сбросить историю",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_paused():
        await update.message.reply_text("⏸ Бот на паузе\\. Напиши /resume чтобы возобновить\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    await update.message.reply_text("🔍 Запускаю проверку, подожди пару минут...")
    await check_and_notify(context.bot)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_paused(True)
    await update.message.reply_text(
        "⏸ Бот поставлен на паузу\\.\nНапиши /resume чтобы возобновить\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_paused(False)
    await update.message.reply_text(
        "▶️ Бот возобновлён\\! Буду проверять вакансии каждый час\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seen   = load_seen()
    status = "⏸ на паузе" if is_paused() else "✅ активен"
    await update.message.reply_text(
        f"⚙️ *Настройки*\n\n"
        f"Статус: {status}\n"
        f"📍 Регион: Москва\n"
        f"⏰ Проверка каждые: `{CHECK_INTERVAL_MINUTES} мин`\n"
        f"🏆 Мин\\. балл AI: `{MIN_SCORE}/100`\n"
        f"👁 Просмотрено: `{len(seen)}` вакансий",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SEEN_FILE.unlink(missing_ok=True)
    await update.message.reply_text(
        "🗑 История сброшена\\. При следующей проверке все вакансии будут новыми\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )


def run_scheduler(bot: Bot, loop):
    def job():
        asyncio.run_coroutine_threadsafe(check_and_notify(bot), loop)
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(30)


def main():
    log.info("🚀 Запускаю HH Design Bot (RSS режим)...")
    if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY]):
        print("⚠️  Задай переменные окружения: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ANTHROPIC_API_KEY")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("clear",  cmd_clear))

    loop = asyncio.get_event_loop()
    bot  = Bot(token=TELEGRAM_TOKEN)
    threading.Thread(target=run_scheduler, args=(bot, loop), daemon=True).start()

    log.info("✅ Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
