import asyncio
import json
import logging
import time
import requests
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = "8899550041:AAGKxh6OUV8MR6l4_5m3ZUHI8zdkh4ZjbDw"
TELEGRAM_CHAT_ID = "5689611608"
HELIUS_API_KEY = "ef5ff0b7-0073-40ea-8d5a-d8e334a64809"

# Адрес кошелька для отслеживания
TARGET_WALLET = "4NGX3sD6PQJLwPCSHdLTACFzLRqR1JhpUEtjpsmCAwGn"

# Параметры симуляции Trojan
LATENCY_DELAY = 2.0         
SLIPPAGE_LIMIT = 0.15       
PRICE_IMPACT_LIMIT = 0.20   
TRADE_AMOUNT_USD = 50.0     
GAS_FEE_USD = 0.50          

# ==================== СОСТОЯНИЕ БОТА ====================
state = {
    "initial_balance": 200.0,
    "balance": 200.0,
    "total_signals": 0,
    "copied_trades": 0,
    "blocked_trades": 0,
    "total_gas_spent": 0.0,
    "positions": {},   
    "closed_trades": [] 
}

logging.basicConfig(level=logging.INFO)

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_token_info(token_mint: str):
    """Получение цены и реального символа любого токена через DexScreener API."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
        resp = requests.get(url, timeout=5).json()
        if resp.get("pairs"):
            pair = resp["pairs"][0]
            price = float(pair.get("priceUsd", 0))
            symbol = pair.get("baseToken", {}).get("symbol", token_mint[:6])
            return price, symbol
    except Exception as e:
        logging.error(f"Ошибка получения данных токена {token_mint}: {e}")
    return 0.0, token_mint[:6]

async def send_tg_message(bot_app, text: str):
    if TELEGRAM_CHAT_ID:
        try:
            await bot_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка отправки Telegram: {e}")

# ==================== ЛОГИКА ТОРГОВЛИ ====================
async def process_buy_signal(bot_app, token_mint: str):
    state["total_signals"] += 1

    initial_price, symbol = get_token_info(token_mint)
    if initial_price == 0:
        initial_price = 0.00001 

    estimated_impact = 0.05 
    if estimated_impact > PRICE_IMPACT_LIMIT:
        state["blocked_trades"] += 1
        await send_tg_message(bot_app, f"🛑 *[СДЕЛКА БЛОКИРОВАНА]* Токен: `{symbol}` (Price Impact превышен)")
        return

    await asyncio.sleep(LATENCY_DELAY)

    execution_price, _ = get_token_info(token_mint)
    if execution_price == 0:
        execution_price = initial_price * 1.05

    price_change = (execution_price - initial_price) / initial_price if initial_price > 0 else 0
    if price_change > SLIPPAGE_LIMIT:
        state["blocked_trades"] += 1
        await send_tg_message(bot_app, f"🛑 *[СДЕЛКА БЛОКИРОВАНА]* Токен: `{symbol}` (Проскальзывание превышено)")
        return

    if state["balance"] < TRADE_AMOUNT_USD:
        await send_tg_message(bot_app, "⚠️ *[ОШИБКА]* Недостаточно виртуального баланса для покупки!")
        return

    state["balance"] -= TRADE_AMOUNT_USD
    state["copied_trades"] += 1
    state["positions"][token_mint] = {
        "buy_price": execution_price,
        "amount_usd": TRADE_AMOUNT_USD,
        "symbol": symbol,
        "time": time.time()
    }

    msg = (
        f"🟢 *[PAPER BUY — TROJAN]*\n"
        f"Монета: *{symbol}*\n"
        f"Минт: `{token_mint}`\n"
        f"Цена входа: `${execution_price:.6f}`\n"
        f"Сумма: `${TRADE_AMOUNT_USD}`\n"
        f"Баланс: `${state['balance']:.2f}`"
    )
    await send_tg_message(bot_app, msg)

async def process_sell_signal(bot_app, token_mint: str):
    if not state["positions"]:
        return
    
    if token_mint not in state["positions"]:
        token_mint = list(state["positions"].keys())[0]

    pos = state["positions"].pop(token_mint)
    symbol = pos["symbol"]
    
    await asyncio.sleep(LATENCY_DELAY)
    
    exit_price, _ = get_token_info(token_mint)
    if exit_price == 0:
        exit_price = pos["buy_price"]

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
        f"Монета: *{symbol}*\n"
        f"Цена выхода: `${exit_price:.6f}`\n"
        f"Чистый PnL: *${pnl_usd:+.2f} ({pnl_pct:+.1f}%)*\n"
        f"Баланс: `${state['balance']:.2f}`"
    )
    await send_tg_message(bot_app, msg)

# ==================== WEBSOCKET СЛУШАТЕЛЬ ====================
async def solana_listener(bot_app):
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
                logging.info("WebSocket успешно подключен!")
                
                async for msg in ws:
                    data = json.loads(msg)
                    if "params" in data and "result" in data["params"]:
                        tx_result = data["params"]["result"]
                        logs_str = str(tx_result).lower()
                        
                        token_mint = "So11111111111111111111111111111111111111112"
                        try:
                            meta = tx_result.get("transaction", {}).get("meta", {})
                            post_balances = meta.get("postTokenBalances", [])
                            for b in post_balances:
                                candidate = b.get("mint")
                                if candidate and candidate != "So11111111111111111111111111111111111111112":
                                    token_mint = candidate
                                    break
                        except Exception:
                            pass

                        if any(kw in logs_str for kw in ["swap", "route", "pump", "raydium", "jupiter"]):
                            if any(b_kw in logs_str for b_kw in ["buy", "mint", "create"]):
                                asyncio.create_task(process_buy_signal(bot_app, token_mint))
                            elif any(s_kw in logs_str for s_kw in ["sell", "close"]):
                                asyncio.create_task(process_sell_signal(bot_app, token_mint))

        except Exception as e:
            print(f"Ошибка WebSocket: {e}. Переподключение...")
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
        f"📊 *ОТЧЁТ ПО PAPER TRADING*\n\n"
        f"💵 *Баланс:* `${state['balance']:.2f}`\n"
        f"📈 *PnL:* *${total_pnl:+.2f} ({pnl_pct:+.1f}%)*\n"
        f"🎯 *Сигналов:* {state['total_signals']} | Скопировано: {state['copied_trades']}\n"
        f"🏆 *Winrate:* {winrate:.1f}%"
    )
    await update.message.reply_text(report, parse_mode="Markdown")

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state["positions"]:
        await update.message.reply_text("📭 Нет открытых позиций.")
        return

    msg = "📂 *ОТКРЫТЫЕ ПОЗИЦИИ:*\n\n"
    for token, info in state["positions"].items():
        curr_price, _ = get_token_info(token)
        pnl = ((curr_price - info["buy_price"]) / info["buy_price"] * 100) if info["buy_price"] > 0 else 0
        msg += f"• *{info['symbol']}* | Вход: `${info['buy_price']:.6f}` | PnL: *{pnl:+.1f}%*\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state["balance"] = state["initial_balance"]
    state["total_signals"] = 0
    state["copied_trades"] = 0
    state["blocked_trades"] = 0
    state["total_gas_spent"] = 0.0
    state["positions"].clear()
    state["closed_trades"].clear()
    await update.message.reply_text("🔄 Сброшено до $200!")

async def main():
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    bot_app.add_handler(CommandHandler("stats", cmd_stats))
    bot_app.add_handler(CommandHandler("positions", cmd_positions))
    bot_app.add_handler(CommandHandler("reset", cmd_reset))

    # Запускаем фоновый слушатель Solana параллельно с ботом
    asyncio.create_task(solana_listener(bot_app))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    # Держим приложение запущенным
    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass