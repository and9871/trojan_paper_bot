import asyncio
import json
import logging
import time
import requests
import websockets
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = "8899550041:AAGKxh6OUV8MR6l4_5m3ZUHI8zdkh4ZjbDw"
TELEGRAM_CHAT_ID = "5689611608"  # Твой ID в Telegram
HELIUS_API_KEY = "ef5ff0b7-0073-40ea-8d5a-d8e334a64809"

# Адрес кошелька для отслеживания
TARGET_WALLET = "4NGX3sD6PQJLwPCSHdLTACFzLRqR1JhpUEtjpsmCAwGn"

# Параметры симуляции Trojan
LATENCY_DELAY = 2.0         # Искусственная задержка (секунды)
SLIPPAGE_LIMIT = 0.15       # Максимальное проскальзывание (15%)
PRICE_IMPACT_LIMIT = 0.20   # Лимит ценового щита (20%)
TRADE_AMOUNT_USD = 50.0     # Размер виртуального входа ($)
GAS_FEE_USD = 0.50          # Виртуальная комиссия за круг (покупка + продажа)

# ==================== СОСТОЯНИЕ БОТА ====================
state = {
    "initial_balance": 200.0,
    "balance": 200.0,
    "total_signals": 0,
    "copied_trades": 0,
    "blocked_trades": 0,
    "total_gas_spent": 0.0,
    "positions": {},   # token_mint: {"buy_price": float, "amount_usd": float, "time": float}
    "closed_trades": [] # [{"pnl_usd": float, "pnl_pct": float, "win": bool}]
}

logging.basicConfig(level=logging.INFO)

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_token_price(token_mint: str) -> float:
    """Получение текущей цены токена в USD через DexScreener API."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
        resp = requests.get(url, timeout=5).json()
        if resp.get("pairs"):
            return float(resp["pairs"][0].get("priceUsd", 0))
    except Exception as e:
        logging.error(f"Ошибка получения цены {token_mint}: {e}")
    return 0.0

async def send_tg_message(app, text: str):
    """Отправка уведомления в Telegram."""
    if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID != "ТВОЙ_CHAT_ID":
        try:
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка отправки Telegram: {e}")

# ==================== ЛОГИКА ТОРГОВЛИ ====================
async def process_buy_signal(app, token_mint: str, initial_price: float, estimated_impact: float):
    state["total_signals"] += 1

    # 1. Защита "Ценовой щит" (Price Impact)
    if estimated_impact > PRICE_IMPACT_LIMIT:
        state["blocked_trades"] += 1
        msg = (
            f"🛑 *[СДЕЛКА БЛОКИРОВАНА]*\n"
            f"Токен: `{token_mint[:8]}...`\n"
            f"Причина: Price Impact ({estimated_impact*100:.1f}%) "
            f"превышает лимит ({PRICE_IMPACT_LIMIT*100:.0f}%)."
        )
        await send_tg_message(app, msg)
        return

    # 2. Имитация задержки обработки Trojan (Latency)
    await asyncio.sleep(LATENCY_DELAY)

    # 3. Запрос реальной цены ПОСЛЕ задержки
    execution_price = get_token_price(token_mint)
    if execution_price == 0:
        execution_price = initial_price * 1.05  # Фолбэк, если API задерживается

    # 4. Проверка на проскальзывание цены
    price_change = (execution_price - initial_price) / initial_price if initial_price > 0 else 0
    if price_change > SLIPPAGE_LIMIT:
        state["blocked_trades"] += 1
        msg = (
            f"🛑 *[СДЕЛКА БЛОКИРОВАНА]*\n"
            f"Токен: `{token_mint[:8]}...`\n"
            f"Причина: Проскальзывание ({price_change*100:.1f}%) "
            f"превышает Slippage ({SLIPPAGE_LIMIT*100:.0f}%)."
        )
        await send_tg_message(app, msg)
        return

    # 5. Исполнение виртуальной покупки
    if state["balance"] < TRADE_AMOUNT_USD:
        await send_tg_message(app, "⚠️ *[ОШИБКА]* Недостаточно виртуального баланса для покупки!")
        return

    state["balance"] -= TRADE_AMOUNT_USD
    state["copied_trades"] += 1
    state["positions"][token_mint] = {
        "buy_price": execution_price,
        "amount_usd": TRADE_AMOUNT_USD,
        "time": time.time()
    }

    msg = (
        f"🟢 *[PAPER BUY — TROJAN]*\n"
        f"Токен: `{token_mint}`\n"
        f"Задержка: {LATENCY_DELAY}s\n"
        f"Цена входа: `${execution_price:.6f}`\n"
        f"Сумма: `${TRADE_AMOUNT_USD}`\n"
        f"Свободный баланс: `${state['balance']:.2f}`"
    )
    await send_tg_message(app, msg)

async def process_sell_signal(app, token_mint: str):
    if token_mint not in state["positions"]:
        return  # Продажа токена, которого нет в наших позициях

    pos = state["positions"].pop(token_mint)
    
    # Задержка на продажу
    await asyncio.sleep(LATENCY_DELAY)
    
    exit_price = get_token_price(token_mint)
    if exit_price == 0:
        exit_price = pos["buy_price"]

    # Расчет профита и комиссий
    raw_return = pos["amount_usd"] * (exit_price / pos["buy_price"])
    net_return = raw_return - GAS_FEE_USD
    pnl_usd = net_return - pos["amount_usd"]
    pnl_pct = (pnl_usd / pos["amount_usd"]) * 100

    state["balance"] += net_return
    state["total_gas_spent"] += GAS_FEE_USD
    is_win = pnl_usd > 0
    state["closed_trades"].append({"pnl_usd": pnl_usd, "pnl_pct": pnl_pct, "win": is_win})

    icon = "🟩" if is_win else "🟥"
    msg = (
        f"{icon} *[PAPER SELL — TROJAN]*\n"
        f"Токен: `{token_mint}`\n"
        f"Цена выхода: `${exit_price:.6f}`\n"
        f"Чистый PnL: *${pnl_usd:+.2f} ({pnl_pct:+.1f}%)*\n"
        f"Комиссия (Gas): `${GAS_FEE_USD}`\n"
        f"Новый баланс: `${state['balance']:.2f}`"
    )
    await send_tg_message(app, msg)

# ==================== WEBSOCKET СЛУШАТЕЛЬ SOLANA ====================
async def solana_listener(app):
    ws_url = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [
            {"accountInclude": [TARGET_WALLET]},
            {
                "commitment": "confirmed",
                "encoding": "jsonParsed",
                "transactionDetails": "full"
            }
        ]
    }

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps(payload))
                logging.info("Успешное подключение к Helius WebSocket!")
                
                async for msg in ws:
                    data = json.loads(msg)
                    if "params" in data and "result" in data["params"]:
                        logs = str(data["params"]["result"])
                        logs_lower = logs.lower()
                        
                        # Расширенный фильтр для Raydium, Jupiter и Pump.fun
                        if any(kw in logs_lower for kw in ["swap", "route", "pump", "instruction: buy", "instruction: sell"]):
                            dummy_token = "So11111111111111111111111111111111111111112"
                            
                            if any(b_kw in logs_lower for b_kw in ["buy", "instruction: buy"]):
                                asyncio.create_task(process_buy_signal(app, dummy_token, 0.001, 0.10))
                            elif any(s_kw in logs_lower for s_kw in ["sell", "instruction: sell"]):
                                asyncio.create_task(process_sell_signal(app, dummy_token))

        except Exception as e:
            print(f"Ошибка WebSocket: {e}. Переподключение через 5 сек...")
            try:
                await asyncio.sleep(5)
            except RuntimeError:
                break

# ==================== TELEGRAM КОМАНДЫ ====================
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades = state["closed_trades"]
    wins = sum(1 for t in trades if t["win"])
    winrate = (wins / len(trades) * 100) if trades else 0.0
    total_pnl = state["balance"] - state["initial_balance"]
    pnl_pct = (total_pnl / state["initial_balance"]) * 100

    report = (
        f"📊 *ОТЧЁТ ПО PAPER TRADING (Trojan Mode)*\n\n"
        f"💵 *Баланс:* `${state['balance']:.2f}` (Старт: `${state['initial_balance']:.2f}`)\n"
        f"📈 *Чистый PnL:* *${total_pnl:+.2f} ({pnl_pct:+.1f}%)*\n"
        f"🎯 *Всего сигналов:* {state['total_signals']}\n"
        f"✅ *Скопировано:* {state['copied_trades']}\n"
        f"🛑 *Отклонено (Slippage/Shield):* {state['blocked_trades']}\n"
        f"🏆 *Winrate:* {winrate:.1f}% ({wins}W / {len(trades)-wins}L)\n"
        f"💸 *Потрачено на виртуальный газ:* `${state['total_gas_spent']:.2f}`"
    )
    await update.message.reply_text(report, parse_mode="Markdown")

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["positions"]:
        await update.message.reply_text("📭 Нет открытых позиций.")
        return

    msg = "📂 *ОТКРЫТЫЕ ПОЗИЦИИ:*\n\n"
    for token, info in state["positions"].items():
        curr_price = get_token_price(token)
        pnl = ((curr_price - info["buy_price"]) / info["buy_price"] * 100) if info["buy_price"] > 0 else 0
        msg += f"• `{token[:8]}...` | Вход: `${info['buy_price']:.6f}` | PnL: *{pnl:+.1f}%*\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["balance"] = state["initial_balance"]
    state["total_signals"] = 0
    state["copied_trades"] = 0
    state["blocked_trades"] = 0
    state["total_gas_spent"] = 0.0
    state["positions"].clear()
    state["closed_trades"].clear()
    await update.message.reply_text("🔄 Баланс и статистика успешно сброшены к $200!")

# ==================== ЗАПУСК ====================
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("reset", cmd_reset))

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
    loop.create_task(solana_listener(app))

    print("🚀 Симулятор Paper Trading запущен!")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass