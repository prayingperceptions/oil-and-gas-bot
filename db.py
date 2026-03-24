import aiosqlite
import logging

logger = logging.getLogger(__name__)

DB_PATH = "kalshi_bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_ticker TEXT,
                action TEXT,
                count INTEGER,
                price_cents INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                strategy TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS eia_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT,
                actual_draw_build REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()
    logger.info("Database initialized successfully.")

async def log_trade(market_ticker: str, action: str, count: int, price_cents: int, strategy: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO trades (market_ticker, action, count, price_cents, strategy) VALUES (?, ?, ?, ?, ?)',
            (market_ticker, action, count, price_cents, strategy)
        )
        await db.commit()

async def log_eia_report(actual_draw_build: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            'INSERT INTO eia_reports (report_date, actual_draw_build) VALUES (date("now"), ?)',
            (actual_draw_build,)
        )
        await db.commit()
