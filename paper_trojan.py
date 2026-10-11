import asyncio
import json
import logging
import time
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = "8899550041:AAG6D5KPENN_DxNcwVALl6MTJoANZiVqSgA"
TELEGRAM_CHAT_ID = "5689611608"

# Адрес целевого кошелька для отслеживания
TARGET_WALLET = "4NGX3sD6PQJLwPCSHdLTACFzLRqR1JhpUEtjpsmCAwGn"

# Параметры симуляции
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

# ==================== ТОРГОВАЯ ЛОГИКА ====================
async def process_buy_signal(bot_app, token_mint: str):
    state["total_signals"] += 1
    initial_price, symbol = get_token_info(token_mint)
    if initial_price == 0:
        initial_price = 0.00001 

    await asyncio.sleep(LATENCY_DELAY)

    execution_price, _ = get_token_info(token_mint)
    if execution_price == 0:
        execution_price = initial_price * 1.05

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
        f"🟢 *[PAPER BUY — COPY]*\n"
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
        f"{icon} *[PAPER SELL — COPY]*\n"
        f"Монета: *{symbol}*\n"
        f"Цена выхода: `${exit_price:.6f}`\n"
        f"Чистый PnL: *${pnl_usd:+.2f} ({pnl_pct:+.1f}%)*\n"
        f"Баланс: `${state['balance']:.2f}`"
    )
    await send_tg_message(bot_app, msg)

# ==================== НАДЕЖНЫЙ СЛУШАТЕЛЬ (SOLANA RPC / DEXSCREENER) ====================
async def reliable_listener(bot_app):
    # Используем публичный RPC Solana для запроса последних транзакций кошелька
    rpc_url = "https://api.mainnet-beta.solana.com"
    seen_sigs = set()

    # Первичная инициализация подписи транзакций
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [TARGET_WALLET, {"limit": 10}]
    }
    
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10).json()
        items = resp.get("result", [])
        for item in items:
            sig = item.get("signature")
            if sig:
                seen_sigs.add(sig)
        logging.info(f"Слушатель Solana RPC подключен. Запомнено транзакций: {len(seen_sigs)}")
    except Exception as e:
        logging.error(f"Ошибка инициализации Solana RPC: {e}")

    while True:
        try:
            resp = requests.post(rpc_url, json=payload, timeout=10).json()
            items = resp.get("result", [])
            
            for item in reversed(items):
                sig = item.get("signature")
                if not sig or sig in seen_sigs:
                    continue
                
                seen_sigs.add(sig)
                if len(seen_sigs) > 200:
                    seen_sigs.pop()

                # Запрашиваем детали транзакции, чтобы найти измененные токены
                tx_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"maxSupportedTransactionVersion": 0}]
                }
                tx_resp = requests.post(rpc_url, json=tx_payload, timeout=10).json()
                result = tx_resp.get("result")
                
                if not result:
                    continue

                # Ищем токен в пост-балансах транзакции
                meta = result.get("meta", {})
                token_mint = None
                all_bals = meta.get("postTokenBalances", []) + meta.get("preTokenBalances", [])
                for b in all_bals:
                    mint = b.get("mint")
                    if mint and mint != "So11111111111111111111111111111111111111112":
                        token_mint = mint
                        break

                if token_mint:
                    # Определяем покупка или продажа по логам/изменениям
                    logs = str(meta.get("logMessages", [])).lower()
                    if any(w in logs for w in ["sell", "close", "transfer"]):
                        logging.info(f"Обнаружена продажа для {token_mint}")
                        asyncio.create_task(process_sell_signal(bot_app, token_mint))
                    else:
                        logging.info(f"Обнаружена покупка для {token_mint}")
                        asyncio.create_task(process_buy_signal(bot_app, token_mint))

        except Exception as e:
            logging.error(f"Ошибка в цикле слушателя Solana: {e}")

        await asyncio.sleep(7)

# ==================== TELEGRAM КОМАНДЫ ====================
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades = state["closed_trades"]
    wins = sum(1 for t in trades if t["win"])
    winrate = (wins / len(trades) * 100) if trades else 0.0
    total_pnl = state["balance"] - state["initial_balance"]
    pnl_pct = (total_pnl / state["initial_balance"]) * 100

    report = (
        f"📊 *ОТЧЁТ ПО PAPER TRADING*\n\n"
        f"💵 *Баланс:* `${state['balance']:.2f}` (Старт: `${state['initial_balance']:.2f}`)\n"
        f"📈 *Чистый PnL:* *${total_pnl:+.2f} ({pnl_pct:+.1f}%)*\n"
        f"🎯 *Всего сигналов:* {state['total_signals']}\n"
        f"✅ *Скопировано:* {state['copied_trades']}\n"
        f"🛑 *Отклонено:* {state['blocked_trades']}\n"
        f"🏆 *Winrate:* {winrate:.1f}% ({wins}W / {len(trades)-wins}L)\n"
        f"💸 *Потрачено на газ:* `${state['total_gas_spent']:.2f}`"
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

    # Запускаем надежный Solana RPC слушатель
    asyncio.create_task(reliable_listener(bot_app))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    stop_event = asyncio.Event()
    await stop_event.wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass