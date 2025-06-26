from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import random
import aiosqlite
import contextlib

app = FastAPI()

origins = ["*"]
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

async def get_balance(telegram_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        if row:
            return row[0]
        await db.execute("INSERT INTO users (telegram_id, balance) VALUES (?, 0)", (telegram_id,))
        await db.commit()
        return 0

async def update_balance(telegram_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (telegram_id, balance) VALUES (?, ?) ON CONFLICT(telegram_id) DO UPDATE SET balance = balance + ?",
            (telegram_id, amount, amount)
        )
        await db.commit()

async def place_bet(telegram_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO bets (telegram_id, amount) VALUES (?, ?)", (telegram_id, amount))
        await db.execute("UPDATE users SET balance = balance - ? WHERE telegram_id = ?", (amount, telegram_id))
        await db.commit()

async def cashout(telegram_id, multiplier):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT amount FROM bets WHERE telegram_id = ? AND cashed_out = 0", (telegram_id,)
        )
        row = await cursor.fetchone()
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

class BalanceAction(BaseModel):
    telegram_id: str
    amount: float

class CashoutRequest(BaseModel):
    telegram_id: str

@app.post("/topup_balance")
async def topup_balance(data: BalanceAction):
    await update_balance(data.telegram_id, data.amount)
    return {"status": "success"}

@app.get("/balance/{telegram_id}")
async def get_user_balance(telegram_id: str):
    balance = await get_balance(telegram_id)
    return {"balance": balance}

@app.post("/bet")
async def bet(data: BalanceAction):
    if not game_state['is_running'] and game_state['time_left'] <= 0:
        bal = await get_balance(data.telegram_id)
        if bal >= data.amount:
            await place_bet(data.telegram_id, data.amount)
            return {"status": "bet placed"}
        return {"status": "insufficient funds"}
    return {"status": "betting closed"}

@app.post("/cashout")
async def do_cashout(data: CashoutRequest):
    if game_state['is_running']:
        win = await cashout(data.telegram_id, game_state['multiplier'])
        return {"status": "cashed out", "win": win}
    return {"status": "not running"}

# ===================== WEBSOCKET =====================

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
    # Клиентке ағымдағы күйді жіберу
    await ws.send_json({
        "type": "state",
        "is_running": game_state["is_running"],
        "multiplier": game_state["multiplier"],
        "time": game_state["time_left"]
    })
    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        connections.remove(ws)

# ===================== GAME LOOP =====================

game_state = {
    "time_left": 10,
    "multiplier": 1.0,
    "is_running": False
}

async def game_loop():
    print("Game loop started 🚀")
    while True:
        game_state['time_left'] = 10
        game_state['is_running'] = False
        await notify_all({"type": "countdown", "time": game_state['time_left']})

        while game_state['time_left'] > 0:
            await asyncio.sleep(1)
            game_state['time_left'] -= 1
            await notify_all({"type": "countdown", "time": game_state['time_left']})

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

        await notify_all({"type": "end", "final_multiplier": multiplier})
        await reset_bets()
        await asyncio.sleep(2)

# ===================== STARTUP / SHUTDOWN =====================

@app.on_event("startup")
async def startup():
    await init_db()
    app.state._game_task = asyncio.create_task(game_loop())

@app.on_event("shutdown")
async def shutdown():
    with contextlib.suppress(Exception):
        app.state._game_task.cancel()
