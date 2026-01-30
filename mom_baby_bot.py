import asyncio
import time
import sqlite3
from datetime import datetime, date, timedelta, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import os
import csv
from io import StringIO, BytesIO

# ========== КОНФИГУРАЦИЯ ==========
API_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not API_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен в переменных окружения!")

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== КЛАССЫ ==========
class SimpleTimezone:
    """Упрощенная работа с часовыми поясами без pytz"""
    TIMEZONES = {
        'Europe/Moscow': 3,
        'Asia/Tbilisi': 4,
        'Europe/Samara': 4,
        'Asia/Yekaterinburg': 5,
        'Europe/London': 0,
        'Asia/Bangkok': 7
    }
    
    def __init__(self, name: str):
        self.name = name
        self.offset_hours = self.TIMEZONES.get(name, 3)
    
    @staticmethod
    def is_valid(tz_name: str) -> bool:
        return tz_name in SimpleTimezone.TIMEZONES
    
    def get_current_time(self) -> datetime:
        utc_now = datetime.now(timezone.utc)
        local_tz = timezone(timedelta(hours=self.offset_hours))
        return utc_now.astimezone(local_tz)

MOSCOW_TZ = SimpleTimezone('Europe/Moscow')

class BabyStates(StatesGroup):
    """Состояния FSM для бота"""
    waiting_baby_name = State()
    waiting_timezone_choice = State()
    waiting_custom_timezone = State()
    waiting_volume = State()
    waiting_description_choice = State()
    waiting_description_text = State()
    waiting_reports_menu = State()
    choosing_calendar_month = State()
    choosing_category_report = State()

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
active_timers = {}  # {user_id: {'start': time, 'category': str, 'date': str}}
reminder_tasks = {}  # {user_id: asyncio.Task} для уведомлений

# ========== ИНИЦИАЛИЗАЦИЯ БД ==========
def init_db():
    """Создает таблицы БД при первом запуске"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    
    # Таблица для логов активностей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS baby_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            duration INTEGER,
            volume INTEGER,
            date TEXT,
            time_start TEXT,
            description TEXT
        )
    ''')
    
    # Таблица для часовых поясов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_timezones (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT
        )
    ''')
    
    # Таблица для пользователей с именем ребенка
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            baby_name TEXT,
            joined_date TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С БД ==========
def log_user(user_id: int, username: str, first_name: str, baby_name: str = None):
    """Регистрирует или обновляет пользователя"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO users (user_id, username, first_name, baby_name, joined_date)
        VALUES (?, ?, ?, COALESCE(?, (SELECT baby_name FROM users WHERE user_id = ?)), 
                COALESCE((SELECT joined_date FROM users WHERE user_id = ?), ?))
    ''', (user_id, username or 'unknown', first_name or 'User', baby_name, user_id, user_id, date.today().isoformat()))
    conn.commit()
    conn.close()

def get_baby_name(user_id: int) -> str:
    """Получает имя ребенка"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('SELECT baby_name FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result and result[0] else 'Малыш'

def update_baby_name(user_id: int, baby_name: str):
    """Обновляет имя ребенка"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET baby_name = ? WHERE user_id = ?', (baby_name, user_id))
    conn.commit()
    conn.close()

def get_user_tz(user_id: int) -> SimpleTimezone:
    """Получает часовой пояс пользователя"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('SELECT timezone FROM user_timezones WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result and result[0]:
        try:
            return SimpleTimezone(result[0])
        except:
            pass
    return MOSCOW_TZ

def save_user_tz(user_id: int, tz_str: str) -> bool:
    """Сохраняет часовой пояс"""
    if not SimpleTimezone.is_valid(tz_str):
        return False
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO user_timezones (user_id, timezone) VALUES (?, ?)', (user_id, tz_str))
    conn.commit()
    conn.close()
    return True

def format_duration(seconds: int) -> str:
    """Форматирует секунды в чч:мм:сс"""
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f'{hours:02d}:{mins:02d}:{secs:02d}'

def get_average_interval(user_id: int, category: str) -> int:
    """Вычисляет средний интервал между активностями (в секундах)"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    
    # Получаем последние 10 записей по категории
    cursor.execute('''
        SELECT date, time_start FROM baby_logs 
        WHERE user_id = ? AND category = ?
        ORDER BY date DESC, time_start DESC
        LIMIT 10
    ''', (user_id, category))
    
    records = cursor.fetchall()
    conn.close()
    
    if len(records) < 2:
        return None
    
    intervals = []
    for i in range(len(records) - 1):
        dt1 = datetime.fromisoformat(f"{records[i][0]} {records[i][1]}")
        dt2 = datetime.fromisoformat(f"{records[i+1][0]} {records[i+1][1]}")
        interval = abs((dt1 - dt2).total_seconds())
        intervals.append(interval)
    
    return int(sum(intervals) / len(intervals)) if intervals else None

def get_statistics(user_id: int):
    """Получает статистику пользователя"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    
    stats = {}
    for category in ['ГВ', 'Сон', 'Смесь']:
        cursor.execute('''
            SELECT COUNT(*), SUM(duration), AVG(volume)
            FROM baby_logs 
            WHERE user_id = ? AND category = ?
        ''', (user_id, category))
        
        count, total_duration, avg_volume = cursor.fetchone()
        stats[category] = {
            'count': count or 0,
            'duration': total_duration or 0,
            'avg_volume': round(avg_volume, 1) if avg_volume else None
        }
    
    conn.close()
    return stats

# ========== КЛАВИАТУРЫ ==========
def get_timezone_keyboard():
    """Клавиатура выбора часового пояса"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='🇷🇺 Москва (UTC+3)')],
            [KeyboardButton(text='🇬🇪 Батуми (UTC+4)')],
            [KeyboardButton(text='🇷🇺 Самара (UTC+4)')],
            [KeyboardButton(text='🇷🇺 Екатеринбург (UTC+5)')],
            [KeyboardButton(text='🇬🇧 Лондон (UTC+0)')],
            [KeyboardButton(text='🇹🇭 Бангкок (UTC+7)')],
            [KeyboardButton(text='🌍 Другой пояс')],
            [KeyboardButton(text='⏭️ Пропустить')]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_main_keyboard():
    """Главная клавиатура"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='🍼 ГВ'), KeyboardButton(text='😴 Сон')],
            [KeyboardButton(text='🍶 Смесь'), KeyboardButton(text='⏹ Стоп')],
            [KeyboardButton(text='📊 Отчет'), KeyboardButton(text='📈 Статистика')]
        ],
        resize_keyboard=True
    )

def get_reports_submenu():
    """Подменю отчетов"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='📅 По дате'), KeyboardButton(text='📋 По категории')],
            [KeyboardButton(text='📄 За сегодня'), KeyboardButton(text='📥 Экспорт CSV')],
            [KeyboardButton(text='⬅️ Назад')]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    """Создает инлайн-календарь"""
    keyboard = []
    
    # Навигация
    prev_year = year - 1 if month == 1 else year
    prev_month = 12 if month == 1 else month - 1
    next_year = year + 1 if month == 12 else year
    next_month = 1 if month == 12 else month + 1
    
    keyboard.append([
        InlineKeyboardButton(text='◀', callback_data=f'cal:{prev_year}:{prev_month:02d}'),
        InlineKeyboardButton(text=f'{datetime(year, month, 1).strftime("%B %Y")}', callback_data='noop'),
        InlineKeyboardButton(text='▶', callback_data=f'cal:{next_year}:{next_month:02d}')
    ])
    
    # Дни недели
    days_of_week = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    keyboard.append([InlineKeyboardButton(text=day, callback_data='noop') for day in days_of_week])
    
    # Дни месяца
    first_day = datetime(year, month, 1)
    last_day = datetime(year, month + 1, 1) - timedelta(days=1) if month < 12 else datetime(year + 1, 1, 1) - timedelta(days=1)
    start_weekday = first_day.weekday()
    
    week = []
    for _ in range(start_weekday):
        week.append(InlineKeyboardButton(text=' ', callback_data='noop'))
    
    for day in range(1, last_day.day + 1):
        week.append(InlineKeyboardButton(text=str(day), callback_data=f'date:{year}:{month:02d}:{day:02d}'))
        if len(week) == 7:
            keyboard.append(week)
            week = []
    
    if week:
        keyboard.append(week)
    
    keyboard.append([InlineKeyboardButton(text='❌ Отмена', callback_data='cancel_calendar')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_categories_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора категории для отчета"""
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('SELECT DISTINCT category FROM baby_logs WHERE user_id = ? ORDER BY category', (user_id,))
    cats = cursor.fetchall()
    conn.close()
    
    keyboard = []
    row = []
    for cat, in cats:
        row.append(InlineKeyboardButton(text=cat, callback_data=f'cat:{cat}'))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text='❌ Отмена', callback_data='cancel_cat')])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ========== УВЕДОМЛЕНИЯ ==========
async def schedule_reminder(user_id: int, category: str, interval: int):
    """Планирует напоминание о следующем кормлении"""
    try:
        await asyncio.sleep(interval)
        baby_name = get_baby_name(user_id)
        
        messages = {
            'ГВ': f'🍼 Пора покормить {baby_name}! Прошло {format_duration(interval)}',
            'Смесь': f'🍶 Время смеси для {baby_name}! Прошло {format_duration(interval)}',
            'Сон': f'😴 {baby_name} скоро захочет спать! Прошло {format_duration(interval)}'
        }
        
        await bot.send_message(user_id, messages.get(category, f'⏰ Напоминание: {category}'))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f'Ошибка напоминания для {user_id}: {e}')

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command('start'))
async def start_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    
    log_user(user_id, username, first_name)
    
    # Проверяем наличие имени ребенка
    baby_name = get_baby_name(user_id)
    if baby_name == 'Малыш':
        await state.set_state(BabyStates.waiting_baby_name)
        await message.answer(
            '👶 Привет! Я помогу отслеживать режим малыша.\n\n'
            'Как зовут вашего ребенка?'
        )
        return
    
    # Проверяем часовой пояс
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('SELECT timezone FROM user_timezones WHERE user_id = ?', (user_id,))
    has_tz = cursor.fetchone()
    conn.close()
    
    if not has_tz:
        await state.set_state(BabyStates.waiting_timezone_choice)
        await message.answer('🌍 Выберите часовой пояс:', reply_markup=get_timezone_keyboard())
    else:
        await message.answer(
            f'👶 С возвращением! Продолжаем следить за {baby_name}!',
            reply_markup=get_main_keyboard()
        )

@dp.message(BabyStates.waiting_baby_name)
async def handle_baby_name(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    baby_name = message.text.strip()[:50]  # Ограничение 50 символов
    
    update_baby_name(user_id, baby_name)
    
    await state.set_state(BabyStates.waiting_timezone_choice)
    await message.answer(
        f'✅ Отлично! Теперь выберите часовой пояс:',
        reply_markup=get_timezone_keyboard()
    )

@dp.message(BabyStates.waiting_timezone_choice)
async def handle_timezone_choice(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()
    
    timezone_map = {
        '🇷🇺 Москва (UTC+3)': 'Europe/Moscow',
        '🇬🇪 Батуми (UTC+4)': 'Asia/Tbilisi',
        '🇷🇺 Самара (UTC+4)': 'Europe/Samara',
        '🇷🇺 Екатеринбург (UTC+5)': 'Asia/Yekaterinburg',
        '🇬🇧 Лондон (UTC+0)': 'Europe/London',
        '🇹🇭 Бангкок (UTC+7)': 'Asia/Bangkok',
        '⏭️ Пропустить': 'Europe/Moscow'
    }
    
    if text in timezone_map:
        save_user_tz(user_id, timezone_map[text])
        await state.clear()
        baby_name = get_baby_name(user_id)
        await message.answer(
            f'🎉 Готово! Начинаем следить за {baby_name}!',
            reply_markup=get_main_keyboard()
        )
    elif text == '🌍 Другой пояс':
        await state.set_state(BabyStates.waiting_custom_timezone)
        await message.answer(
            'Введите часовой пояс:\n'
            'Europe/Moscow, Asia/Tbilisi, Europe/Samara,\n'
            'Asia/Yekaterinburg, Europe/London, Asia/Bangkok'
        )
    else:
        await message.answer('Выберите из предложенных вариантов:', reply_markup=get_timezone_keyboard())

@dp.message(BabyStates.waiting_custom_timezone)
async def handle_custom_timezone(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    tz_str = message.text.strip()
    
    if save_user_tz(user_id, tz_str):
        await state.clear()
        baby_name = get_baby_name(user_id)
        await message.answer(
            f'✅ Часовой пояс {tz_str} установлен!\n\n'
            f'🎉 Начинаем следить за {baby_name}!',
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            f'❌ Часовой пояс "{tz_str}" не найден.\n'
            'Поддерживаемые: Europe/Moscow, Asia/Tbilisi, Europe/Samara, '
            'Asia/Yekaterinburg, Europe/London, Asia/Bangkok'
        )

# ========== СТАРТ/СТОП АКТИВНОСТЕЙ ==========
@dp.message(F.text.in_(['🍼 ГВ', '😴 Сон', '🍶 Смесь']))
async def start_activity(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if user_id in active_timers:
        await message.answer('⏳ Уже идет отсчет! Нажмите "⏹ Стоп" для завершения.')
        return
    
    category_map = {'🍼 ГВ': 'ГВ', '😴 Сон': 'Сон', '🍶 Смесь': 'Смесь'}
    category = category_map[message.text]
    
    active_timers[user_id] = {
        'start': time.time(),
        'category': category,
        'date': date.today().isoformat()
    }
    
    baby_name = get_baby_name(user_id)
    emoji_map = {'ГВ': '🍼', 'Сон': '😴', 'Смесь': '🍶'}
    
    await message.answer(
        f'{emoji_map[category]} {category} для {baby_name} начато!\n'
        f'⏱ Таймер запущен...',
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == '⏹ Стоп')
async def stop_activity(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    if user_id not in active_timers:
        await message.answer('⏰ Таймер не запущен! Выберите активность для начала.')
        return
    
    timer_data = active_timers.pop(user_id)
    start_time = timer_data['start']
    category = timer_data['category']
    date_str = timer_data['date']
    
    elapsed = int(time.time() - start_time)
    time_str = format_duration(elapsed)
    
    user_tz = get_user_tz(user_id)
    now_user = user_tz.get_current_time()
    timestart_str = now_user.strftime('%H:%M')
    
    baby_name = get_baby_name(user_id)
    
    # Если Смесь - запрашиваем объем
    if category == 'Смесь':
        await state.update_data(
            last_category=category,
            last_elapsed=elapsed,
            last_start=timestart_str,
            last_date=date_str
        )
        await state.set_state(BabyStates.waiting_volume)
        await message.answer(f'🍶 {baby_name} покушал(а)!\n⏱ Время: {time_str}\n\n💧 Введите объем смеси (мл):')
    else:
        # Сохраняем без объема
        conn = sqlite3.connect('baby_logs.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO baby_logs (user_id, category, duration, volume, date, time_start, description)
            VALUES (?, ?, ?, NULL, ?, ?, NULL)
        ''', (user_id, category, elapsed, date_str, timestart_str))
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        emoji_map = {'ГВ': '🍼', 'Сон': '😴'}
        await message.answer(
            f'{emoji_map[category]} {category} завершено!\n'
            f'👶 {baby_name}\n'
            f'⏱ Время: {time_str}\n'
            f'📅 {date_str}',
            reply_markup=get_main_keyboard()
        )
        
        # Планируем напоминание
        avg_interval = get_average_interval(user_id, category)
        if avg_interval and avg_interval > 300:  # Минимум 5 минут
            if user_id in reminder_tasks:
                reminder_tasks[user_id].cancel()
            reminder_tasks[user_id] = asyncio.create_task(schedule_reminder(user_id, category, avg_interval))
        
        # Предлагаем добавить описание
        await state.update_data(last_task_id=task_id)
        desc_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text='📝 Да'), KeyboardButton(text='⏭ Нет')]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await message.answer('Добавить заметку?', reply_markup=desc_kb)
        await state.set_state(BabyStates.waiting_description_choice)

@dp.message(BabyStates.waiting_volume)
async def handle_volume(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    try:
        volume = int(message.text.strip())
        if volume <= 0 or volume > 500:
            await message.answer('❌ Введите корректный объем (1-500 мл):')
            return
    except ValueError:
        await message.answer('❌ Введите число (например: 120):')
        return
    
    data = await state.get_data()
    category = data['last_category']
    elapsed = data['last_elapsed']
    timestart_str = data['last_start']
    date_str = data['last_date']
    
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO baby_logs (user_id, category, duration, volume, date, time_start, description)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
    ''', (user_id, category, elapsed, volume, date_str, timestart_str))
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    baby_name = get_baby_name(user_id)
    time_str = format_duration(elapsed)
    
    await message.answer(
        f'🍶 Смесь завершено!\n'
        f'👶 {baby_name}\n'
        f'⏱ Время: {time_str}\n'
        f'💧 Объем: {volume} мл\n'
        f'📅 {date_str}',
        reply_markup=get_main_keyboard()
    )
    
    # Планируем напоминание
    avg_interval = get_average_interval(user_id, category)
    if avg_interval and avg_interval > 300:
        if user_id in reminder_tasks:
            reminder_tasks[user_id].cancel()
        reminder_tasks[user_id] = asyncio.create_task(schedule_reminder(user_id, category, avg_interval))
    
    # Предлагаем описание
    await state.update_data(last_task_id=task_id)
    desc_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text='📝 Да'), KeyboardButton(text='⏭ Нет')]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer('Добавить заметку?', reply_markup=desc_kb)
    await state.set_state(BabyStates.waiting_description_choice)

@dp.message(BabyStates.waiting_description_choice)
async def handle_description_choice(message: types.Message, state: FSMContext):
    text = message.text.strip()
    
    if text == '⏭ Нет':
        await state.clear()
        await message.answer('✅ Готово!', reply_markup=get_main_keyboard())
    elif text == '📝 Да':
        await state.set_state(BabyStates.waiting_description_text)
        await message.answer('📝 Введите заметку (настроение, особенности и т.д.):')
    else:
        await message.answer('Выберите "📝 Да" или "⏭ Нет"')

@dp.message(BabyStates.waiting_description_text)
async def save_description(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    task_id = data.get('last_task_id')
    
    if not task_id:
        await state.clear()
        await message.answer('❌ Ошибка сохранения', reply_markup=get_main_keyboard())
        return
    
    description = message.text.strip()[:500]  # Ограничение 500 символов
    
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE baby_logs SET description = ? WHERE id = ? AND user_id = ?',
                   (description, task_id, user_id))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer('✅ Заметка сохранена!', reply_markup=get_main_keyboard())

# ========== ОТЧЕТЫ ==========
async def send_report_for_date(user_id: int, report_date: date, message: types.Message):
    """Отчет по дате"""
    date_str = report_date.isoformat()
    baby_name = get_baby_name(user_id)
    
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT category, duration, volume, time_start, description
        FROM baby_logs
        WHERE user_id = ? AND date = ?
        ORDER BY time_start
    ''', (user_id, date_str))
    
    logs = cursor.fetchall()
    conn.close()
    
    if not logs:
        await message.answer(f'📊 За {date_str} записей нет.')
        return
    
    report_text = f'📊 *Отчет за {date_str}*\n👶 {baby_name}\n\n'
    
    stats_by_cat = {}
    for category, duration, volume, time_start, description in logs:
        if category not in stats_by_cat:
            stats_by_cat[category] = {'count': 0, 'duration': 0, 'volumes': []}
        stats_by_cat[category]['count'] += 1
        stats_by_cat[category]['duration'] += duration
        if volume:
            stats_by_cat[category]['volumes'].append(volume)
    
    for category, stats in stats_by_cat.items():
        emoji = {'ГВ': '🍼', 'Сон': '😴', 'Смесь': '🍶'}[category]
        time_str = format_duration(stats['duration'])
        report_text += f'{emoji} *{category}*: {stats["count"]}x, ⏱ {time_str}'
        if stats['volumes']:
            avg_vol = sum(stats['volumes']) / len(stats['volumes'])
            report_text += f', 💧 {int(avg_vol)} мл сред.'
        report_text += '\n'
    
    report_text += '\n*Детали:*\n'
    for category, duration, volume, time_start, description in logs:
        emoji = {'ГВ': '🍼', 'Сон': '😴', 'Смесь': '🍶'}[category]
        time_str = format_duration(duration)
        report_text += f'{time_start} | {emoji} {category}: {time_str}'
        if volume:
            report_text += f' ({volume} мл)'
        if description:
            report_text += f'\n  💬 {description}'
        report_text += '\n'
    
    await message.answer(report_text, parse_mode='Markdown', reply_markup=get_main_keyboard())

async def send_report_for_category(user_id: int, category: str, message: types.Message):
    """Отчет по категории"""
    baby_name = get_baby_name(user_id)
    
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('''
        SELECT date, duration, volume, time_start, description
        FROM baby_logs
        WHERE user_id = ? AND category = ?
        ORDER BY date DESC, time_start DESC
        LIMIT 20
    ''', (user_id, category))
    
    logs = cursor.fetchall()
    conn.close()
    
    if not logs:
        await message.answer(f'📋 Нет записей для категории "{category}".')
        return
    
    total_duration = sum(log[1] for log in logs)
    volumes = [log[2] for log in logs if log[2]]
    
    emoji = {'ГВ': '🍼', 'Сон': '😴', 'Смесь': '🍶'}[category]
    report_text = f'📋 *Отчет: {emoji} {category}*\n👶 {baby_name}\n\n'
    report_text += f'*Записей:* {len(logs)}\n'
    report_text += f'*Общее время:* {format_duration(total_duration)}\n'
    
    if volumes:
        avg_vol = sum(volumes) / len(volumes)
        report_text += f'*Средний объем:* {int(avg_vol)} мл\n'
    
    report_text += '\n*Последние записи:*\n'
    for date_str, duration, volume, time_start, description in logs[:10]:
        time_str = format_duration(duration)
        report_text += f'{date_str} {time_start}: {time_str}'
        if volume:
            report_text += f' ({volume} мл)'
        if description:
            report_text += f'\n  💬 {description}'
        report_text += '\n'
    
    await message.answer(report_text, parse_mode='Markdown', reply_markup=get_main_keyboard())

@dp.message(F.text == '📊 Отчет')
async def show_reports_menu(message: types.Message, state: FSMContext):
    await state.set_state(BabyStates.waiting_reports_menu)
    await message.answer('Выберите тип отчета:', reply_markup=get_reports_submenu())

@dp.message(BabyStates.waiting_reports_menu, F.text == '📄 За сегодня')
async def report_today(message: types.Message, state: FSMContext):
    await state.clear()
    await send_report_for_date(message.from_user.id, date.today(), message)

@dp.message(BabyStates.waiting_reports_menu, F.text == '📅 По дате')
async def ask_report_date(message: types.Message, state: FSMContext):
    today = date.today()
    await state.set_state(BabyStates.choosing_calendar_month)
    await message.answer('📅 Выберите дату:', reply_markup=get_calendar_keyboard(today.year, today.month))

@dp.message(BabyStates.waiting_reports_menu, F.text == '📋 По категории')
async def ask_report_category(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    cat_kb = get_categories_keyboard(user_id)
    
    if len(cat_kb.inline_keyboard) == 1:  # Только кнопка отмены
        await message.answer('📋 У вас нет записей.')
        return
    
    await state.set_state(BabyStates.choosing_category_report)
    await message.answer('📋 Выберите категорию:', reply_markup=cat_kb)

@dp.message(BabyStates.waiting_reports_menu, F.text == '📥 Экспорт CSV')
async def export_csv(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    
    conn = sqlite3.connect('baby_logs.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM baby_logs WHERE user_id = ?', (user_id,))
    count = cursor.fetchone()[0]
    
    if count == 0:
        await message.answer('❌ У вас нет записей для экспорта.')
        return
    
    cursor.execute('''
        SELECT date, time_start, category, duration, volume, description
        FROM baby_logs
        WHERE user_id = ?
        ORDER BY date DESC, time_start DESC
    ''', (user_id,))
    logs = cursor.fetchall()
    conn.close()
    
    # Генерируем CSV
    output = StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(['Дата', 'Время', 'Категория', 'Длительность', 'Объем (мл)', 'Заметка'])
    
    for log in logs:
        date_str, time_start, category, duration, volume, description = log
        duration_str = format_duration(duration)
        writer.writerow([date_str, time_start, category, duration_str, volume or '', description or ''])
    
    csv_bytes = BytesIO(output.getvalue().encode('utf-8-sig'))
    csv_bytes.seek(0)
    
    baby_name = get_baby_name(user_id)
    await message.answer_document(
        document=types.BufferedInputFile(
            file=csv_bytes.getvalue(),
            filename=f'baby_{baby_name}_{date.today().isoformat()}.csv'
        ),
        caption=f'📊 Данные о {baby_name}\n📋 Записей: {count}'
    )
    await message.answer('✅ Готово!', reply_markup=get_main_keyboard())

@dp.message(BabyStates.waiting_reports_menu, F.text == '⬅️ Назад')
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer('Главное меню', reply_markup=get_main_keyboard())

@dp.message(F.text == '📈 Статистика')
async def show_statistics(message: types.Message):
    user_id = message.from_user.id
    baby_name = get_baby_name(user_id)
    stats = get_statistics(user_id)
    
    report = f'📈 *Статистика*\n👶 {baby_name}\n\n'
    
    for category, data in stats.items():
        emoji = {'ГВ': '🍼', 'Сон': '😴', 'Смесь': '🍶'}[category]
        report += f'{emoji} *{category}*\n'
        report += f'  Записей: {data["count"]}\n'
        if data['duration'] > 0:
            report += f'  Время: {format_duration(data["duration"])}\n'
        if data['avg_volume']:
            report += f'  Средний объем: {data["avg_volume"]} мл\n'
        
        # Средний интервал
        avg_interval = get_average_interval(user_id, category)
        if avg_interval:
            report += f'  Интервал: ~{format_duration(avg_interval)}\n'
        report += '\n'
    
    await message.answer(report, parse_mode='Markdown')

# ========== CALLBACK HANDLERS ==========
@dp.callback_query(F.data.startswith('cal:'))
async def handle_calendar_nav(callback: types.CallbackQuery):
    try:
        parts = callback.data.split(':')
        year, month = int(parts[1]), int(parts[2])
        await callback.message.edit_reply_markup(reply_markup=get_calendar_keyboard(year, month))
        await callback.answer()
    except:
        await callback.answer('Ошибка навигации', show_alert=False)

@dp.callback_query(F.data.startswith('date:'))
async def handle_date_selection(callback: types.CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(':')
        year, month, day = int(parts[1]), int(parts[2]), int(parts[3])
        selected_date = date(year, month, day)
        
        await callback.message.delete()
        await state.clear()
        await send_report_for_date(callback.from_user.id, selected_date, callback.message)
    except:
        await callback.answer('Ошибка обработки даты', show_alert=False)

@dp.callback_query(F.data.startswith('cat:'))
async def handle_category_selection(callback: types.CallbackQuery, state: FSMContext):
    try:
        category = callback.data.split(':', 1)[1]
        await callback.message.delete()
        await state.clear()
        await send_report_for_category(callback.from_user.id, category, callback.message)
    except:
        await callback.answer('Ошибка выбора категории', show_alert=False)

@dp.callback_query(F.data == 'cancel_calendar')
async def cancel_calendar(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data == 'cancel_cat')
async def cancel_category(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data == 'noop')
async def handle_noop(callback: types.CallbackQuery):
    await callback.answer()

# ========== FALLBACK ==========
@dp.message()
async def fallback_handler(message: types.Message):
    user_id = message.from_user.id
    if user_id in active_timers:
        await message.answer('⏳ Таймер активен! Нажмите "⏹ Стоп" для завершения.')
    else:
        await message.answer('Используйте кнопки меню 👇', reply_markup=get_main_keyboard())

# ========== ЗАПУСК ==========
async def main():
    print('🚀 Бот запущен!')
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('👋 Бот остановлен')