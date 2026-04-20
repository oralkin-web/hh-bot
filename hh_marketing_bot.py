#!/usr/bin/env python3
"""
HH.ru Design Director Job Bot with Claude AI
"""

import os
import json
import time
import logging
import asyncio
import re
import schedule
import threading
from pathlib import Path

import requests
import anthropic
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SEARCH_CONFIG = {
    "query": "руководитель департамента дизайна OR руководитель отдела дизайна OR руководитель дизайн студии OR руководитель дизайн группы OR дизайн директор OR дизайн-директор OR Design Director OR head of design OR директор по дизайну",
    "area": 1,
    "per_page": 100,
    "check_interval_minutes": 60,
}

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

HH_HEADERS = {
    "User-Agent": "HH-User-Agent/1.0 (oralkin@gmail.com)",
    "Accept": "application/json",
    "HH-User-Agent": "HH-User-Agent/1.0 (oralkin@gmail.com)",
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


def fetch_vacancies() -> list:
    params = {
        "text": SEARCH_CONFIG["query"],
        "area": SEARCH_CONFIG["area"],
        "per_page": SEARCH_CONFIG["per_page"],
        "order_by": "publication_time",
    }
    try:
        r = requests.get(
            "https://api.hh.ru/vacancies",
            params=params,
            headers=HH_HEADERS,
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        log.info(f"HH.ru: найдено {data.get('found','?')}, загружено {len(data.get('items',[]))}")
        return data.get("items", [])
    except Exception as e:
        log.error(f"Ошибка HH API: {e}")
        return []


def get_vacancy_details(vacancy_id: str) -> dict:
    try:
        r = requests.get(
            f"https://api.hh.ru/vacancies/{vacancy_id}",
            headers=HH_HEADERS,
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()[:1500]


def score_vacancy_with_claude(vacancy: dict) -> dict:
    details     = get_vacancy_details(vacancy.get("id", ""))
    description = strip_html(details.get("description", ""))
    salary      = vacancy.get("salary")
    salary_str  = "не указана"
    if salary:
        parts = []
        if salary.get("from"): parts.append(f"от {salary['from']:,}")
        if salary.get("to"):   parts.append(f"до {salary['to']:,}")
        salary_str = " ".join(parts) + f" {salary.get('currency','RUB')}"

    key_skills   = [s["name"] for s in details.get("key_skills", [])]
    vacancy_text = (
        f"Название: {vacancy.get('name','—')}\n"
        f"Компания: {vacancy.get('employer',{}).get('name','—')}\n"
        f"Зарплата: {salary_str}\n"
        f"Город: {vacancy.get('area',{}).get('name','—')}\n"
        f"Формат: {vacancy.get('schedule',{}).get('name','—')}\n"
        f"Опыт: {vacancy.get('experience',{}).get('name','—')}\n"
        f"Навыки: {', '.join(key_skills) if key_skills else '—'}\n"
        f"Описание: {description}"
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
    score      = ai.get("score", 0)
    emoji      = "🟢" if score >= 80 else "🟡" if score >= 65 else "🔴"
    salary     = vacancy.get("salary")
    salary_str = "не указана"
    if salary:
        parts = []
        if salary.get("from"): parts.append(f"от {salary['from']:,}")
        if salary.get("to"):   parts.append(f"до {salary['to']:,}")
        salary_str = " ".join(parts) + f" {salary.get('currency','RUB')}"

    msg = (
        f"{emoji} *{esc(vacancy.get('name','—'))}*\n"
        f"🏢 {esc(vacancy.get('employer',{}).get('name','—'))}\n"
        f"💰 {esc(salary_str)}\n"
        f"📍 {esc(vacancy.get('area',{}).get('name','—'))} · {esc(vacancy.get('schedule',{}).get('name','—'))}\n"
        f"🎓 {esc(vacancy.get('experience',{}).get('name','—'))} · {esc(vacancy.get('published_at','')[:10])}\n\n"
        f"🤖 *AI\\-оценка: {score}/100*\n"
        f"_{esc(ai.get('reason',''))}_\n"
    )
    for p in ai.get("pros", []): msg += f"\n  ✅ {esc(p)}"
    for c in ai.get("cons", []): msg += f"\n  ❌ {esc(c)}"
    msg += f"\n\n🔗 [Открыть вакансию]({vacancy.get('alternate_url','')})"
    return msg


async def check_and_notify(bot: Bot):
    if is_paused():
        log.info("⏸ Бот на паузе, пропускаю проверку")
        return

    log.info("🔍 Проверяю вакансии...")
    seen      = load_seen()
    vacancies = fetch_vacancies()
    new       = [v for v in vacancies if v["id"] not in seen]
    log.info(f"Новых: {len(new)}")

    if not new:
        return

    sent = 0
    for v in new:
        seen.add(v["id"])
        log.info(f"  → {v.get('name')} / {v.get('employer',{}).get('name','?')}")
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
        f"Слежу за вакансиями Design Director / Head of Design на hh\\.ru\\.\n\n"
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
    await update.message.reply_text("🔍 Запускаю проверку, подожди...")
    await check_and_notify(context.bot)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_paused(True)
    await update.message.reply_text(
        "⏸ Бот поставлен на паузу\\. Автоматические проверки остановлены\\.\nНапиши /resume чтобы возобновить\\.",
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
        f"⏰ Проверка каждые: `{SEARCH_CONFIG['check_interval_minutes']} мин`\n"
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
    schedule.every(SEARCH_CONFIG["check_interval_minutes"]).minutes.do(job)
    while True:
        schedule.run_pending()
        time.sleep(30)


def main():
    log.info("🚀 Запускаю HH Design Bot...")
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
