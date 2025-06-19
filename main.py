from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import random
import aiosqlite
import time

app = FastAPI()

origins = [
    "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "aviator.db"

# ===================== DATABASE =====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id TEXT PRIMARY KEY,
            balance REAL DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            telegram_id TEXT,
            amount REAL,
            cashed_out INTEGER DEFAULT 0,
            multiplier REAL DEFAULT 1.0
        )
        """)
        await db.commit()

@app.on_event("startup")
async def startup():
    await init_db()
    asyncio.create_task(game_loop())

async def get_balance(telegram_id):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone("SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,))
        if row:
            return row[0]
        await db.execute("INSERT INTO users (telegram_id, balance) VALUES (?, 0)", (telegram_id,))
        await db.commit()
        return 0

async def update_balance(telegram_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (telegram_id, balance) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET balance = balance + ?", (telegram_id, amount, amount))
        await db.commit()

async def place_bet(telegram_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO bets (telegram_id, amount) VALUES (?, ?)", (telegram_id, amount))
        await db.execute("UPDATE users SET balance = balance - ? WHERE telegram_id = ?", (amount, telegram_id))
        await db.commit()

async def cashout(telegram_id, multiplier):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchone("SELECT amount FROM bets WHERE telegram_id = ? AND cashed_out = 0", (telegram_id,))
        if row:
            win = row[0] * multiplier
            await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (win, telegram_id))
            await db.execute("UPDATE bets SET cashed_out = 1, multiplier = ? WHERE telegram_id = ?", (multiplier, telegram_id))
            await db.commit()
            return win
        return 0

async def reset_bets():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bets")
        await db.commit()

# ===================== API =====================

class TopUp(BaseModel):
    telegram_id: str
    amount: float

@app.post("/topup_balance")
async def topup_balance(data: TopUp):
    await update_balance(data.telegram_id, data.amount)
    return {"status": "success"}

@app.get("/balance/{telegram_id}")
async def get_user_balance(telegram_id: str):
    balance = await get_balance(telegram_id)
    return {"balance": balance}

@app.post("/bet")
async def bet(data: TopUp):
    if not game_state['is_running'] and game_state['time_left'] <= 0:
        bal = await get_balance(data.telegram_id)
        if bal >= data.amount:
            await place_bet(data.telegram_id, data.amount)
            return {"status": "bet placed"}
        return {"status": "insufficient funds"}
    return {"status": "betting closed"}

@app.post("/cashout")
async def do_cashout(data: TopUp):
    if game_state['is_running']:
        win = await cashout(data.telegram_id, game_state['multiplier'])
        return {"status": "cashed out", "win": win}
    return {"status": "not running"}

# ===================== GAME LOOP =====================

connections = []

async def notify_all(data):
    to_remove = []
    for conn in connections:
        try:
            await conn.send_json(data)
        except WebSocketDisconnect:
            to_remove.append(conn)
    for conn in to_remove:
        connections.remove(conn)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connections.append(ws)
    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        connections.remove(ws)

# Game state

game_state = {
    "time_left": 10,
    "multiplier": 1.0,
    "is_running": False
}

async def game_loop():
    while True:
        # Countdown before flight
        game_state['time_left'] = 10
        game_state['is_running'] = False
        await notify_all({"type": "countdown", "time": game_state['time_left']})

        while game_state['time_left'] > 0:
            await asyncio.sleep(1)
            game_state['time_left'] -= 1
            await notify_all({"type": "countdown", "time": game_state['time_left']})

        # Plane starts flying
        game_state['is_running'] = True
        multiplier = 1.0
        await notify_all({"type": "start"})

        while True:
            await asyncio.sleep(0.1)
            multiplier += round(random.uniform(0.01, 0.05), 2)
            game_state['multiplier'] = multiplier
            await notify_all({"type": "multiplier", "value": multiplier})

            if random.random() < 0.01 or multiplier > 100:
                break

        # End of flight
        await notify_all({"type": "end", "final_multiplier": multiplier})
        await reset_bets()
        await asyncio.sleep(2)
