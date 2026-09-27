#!/usr/bin/env python3
"""
pumpfun_scout_bot.py

A Telegram alert bot that watches pump.fun token launches (via PumpPortal's
public WebSocket feed) and flags tokens matching configurable "early momentum"
and "lower red-flag" heuristics.

⚠️ IMPORTANT REALITY CHECK ⚠️
No script can reliably predict which token will 50x. Pump.fun is dominated by
rug pulls, wash trading, and pure luck. Treat every alert as "worth a
15-second manual look," never as a buy signal.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional

import requests
import websockets

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"
OBSERVATION_WINDOW_SEC = 90
MIN_UNIQUE_BUYERS = 8
MIN_BUY_VOLUME_SOL = 5.0
MIN_BUY_SELL_RATIO = 1.5
NAME_BLOCKLIST_SUBSTRINGS = ["test", "scam", "airdrop claim"]
MAX_TRACKED_TOKENS = 500
SEND_ACTIVITY_LOG_TO_TELEGRAM = False
RECENT_HISTORY_MAX = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pumpfun_scout")


def send_telegram_message(text: str) -> None:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing alert instead:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("Telegram send exception: %s", e)


@dataclass
class TokenState:
    mint: str
    name: str
    symbol: str
    creator: str
    created_at: float = field(default_factory=time.time)
    buy_volume_sol: float = 0.0
    sell_volume_sol: float = 0.0
    buyers: set = field(default_factory=set)
    alerted: bool = False

    def buy_sell_ratio(self) -> float:
        if self.sell_volume_sol <= 0:
            return float("inf") if self.buy_volume_sol > 0 else 0.0
        return self.buy_volume_sol / self.sell_volume_sol

    def age(self) -> float:
        return time.time() - self.created_at


tracked_tokens: Dict[str, TokenState] = {}
recent_history: deque = deque(maxlen=RECENT_HISTORY_MAX)


def is_name_blocked(name: str, symbol: str) -> bool:
    combined = f"{name} {symbol}".lower()
    return any(bad in combined for bad in NAME_BLOCKLIST_SUBSTRINGS)


def evaluate_token(state: TokenState) -> Optional[str]:
    if state.alerted:
        return None
    if len(state.buyers) < MIN_UNIQUE_BUYERS:
        return None
    if state.buy_volume_sol < MIN_BUY_VOLUME_SOL:
        return None
    if state.buy_sell_ratio() < MIN_BUY_SELL_RATIO:
        return None

    state.alerted = True
    pumpfun_link = f"https://pump.fun/{state.mint}"
    solscan_link = f"https://solscan.io/token/{state.mint}"
    return (
        f"🚨 *Early momentum flag* 🚨\n"
        f"*{state.name}* (${state.symbol})\n"
        f"Age: {int(state.age())}s\n"
        f"Buy volume: {state.buy_volume_sol:.2f} SOL\n"
        f"Unique buyers: {len(state.buyers)}\n"
        f"Buy/Sell ratio: {state.buy_sell_ratio():.1f}x\n"
        f"[pump.fun]({pumpfun_link}) | [solscan]({solscan_link})\n\n"
        f"⚠️ This is a heuristic flag, not a signal. Check holder "
        f"distribution and socials yourself before doing anything. Most "
        f"flagged tokens will still fail."
    )


def prune_expired_tokens() -> None:
    expired = [mint for mint, st in tracked_tokens.items() if st.age() > OBSERVATION_WINDOW_SEC]
    for mint in expired:
        del tracked_tokens[mint]
    if len(tracked_tokens) > MAX_TRACKED_TOKENS:
        oldest = sorted(tracked_tokens.values(), key=lambda s: s.created_at)
        for st in oldest[: len(tracked_tokens) - MAX_TRACKED_TOKENS]:
            tracked_tokens.pop(st.mint, None)


async def handle_new_token(msg: dict) -> None:
    mint = msg.get("mint")
    if not mint:
        return
    name = msg.get("name", "")
    symbol = msg.get("symbol", "")
    creator = msg.get("traderPublicKey", "")

    if is_name_blocked(name, symbol):
        log.info("Skipping blocked name: %s (%s)", name, symbol)
        return

    tracked_tokens[mint] = TokenState(mint=mint, name=name, symbol=symbol, creator=creator)
    recent_history.append({"mint": mint, "name": name, "symbol": symbol, "time": time.time()})
    log.info("Tracking new token: %s (%s) mint=%s", name, symbol, mint)

    if SEND_ACTIVITY_LOG_TO_TELEGRAM:
        pumpfun_link = f"https://pump.fun/{mint}"
        send_telegram_message(
            f"🆕 New launch: *{name}* (${symbol})\n[pump.fun]({pumpfun_link})"
        )


async def handle_trade(msg: dict) -> None:
    mint = msg.get("mint")
    if not mint or mint not in tracked_tokens:
        return
    state = tracked_tokens[mint]

    sol_amount = float(msg.get("solAmount", 0) or 0)
    trade_type = msg.get("txType", "")
    buyer = msg.get("traderPublicKey", "")

    if trade_type == "buy":
        state.buy_volume_sol += sol_amount
        if buyer:
            state.buyers.add(buyer)
    elif trade_type == "sell":
        state.sell_volume_sol += sol_amount

    alert = evaluate_token(state)
    if alert:
        send_telegram_message(alert)


async def run_bot() -> None:
    log.info("Connecting to PumpPortal feed...")
    async for ws in _reconnecting_websocket(PUMPPORTAL_WS_URL):
        try:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            await ws.send(json.dumps({"method": "subscribeTokenTrade"}))
            log.info("Subscribed to new-token and trade feeds.")

            last_prune = time.time()
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if "traderPublicKey" in msg and "mint" in msg and "txType" in msg:
                    if msg.get("txType") in ("buy", "sell"):
                        await handle_trade(msg)
                    else:
                        if msg.get("txType") == "create":
                            await handle_new_token(msg)

                if time.time() - last_prune > 10:
                    prune_expired_tokens()
                    last_prune = time.time()
        except websockets.ConnectionClosed:
            log.warning("WebSocket closed, reconnecting...")
            continue


async def _reconnecting_websocket(url: str, retry_delay: int = 5):
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                yield ws
        except Exception as e:
            log.error("WebSocket connection error: %s — retrying in %ss", e, retry_delay)
            await asyncio.sleep(retry_delay)


def build_recent_message() -> str:
    if not recent_history:
        return "No tokens scanned yet — still watching."
    lines = ["🕵️ *Recently scanned tokens:*"]
    for entry in reversed(list(recent_history)[-15:]):
        age = int(time.time() - entry["time"])
        lines.append(
            f"• *{entry['name']}* (${entry['symbol']}) — {age}s ago\n"
            f"  https://pump.fun/{entry['mint']}"
        )
    return "\n".join(lines)


def _fetch_telegram_updates(offset: int) -> list:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 25, "offset": offset}
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code != 200:
        log.error("getUpdates failed: %s %s", resp.status_code, resp.text)
        return []
    return resp.json().get("result", [])


async def telegram_command_listener() -> None:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — command listener disabled.")
        return

    next_offset = 0
    log.info("Listening for Telegram commands (e.g. /recent)...")
    while True:
        try:
            updates = await asyncio.to_thread(_fetch_telegram_updates, next_offset)
            for update in updates:
                next_offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message") or {}
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = (message.get("text") or "").strip()

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text.startswith("/recent"):
                    send_telegram_message(build_recent_message())
                elif text.startswith("/start") or text.startswith("/help"):
                    send_telegram_message(
                        "Commands:\n/recent — show the last tokens scanned"
                    )
        except Exception as e:
            log.error("Telegram command listener error: %s", e)
            await asyncio.sleep(5)


async def main() -> None:
    await asyncio.gather(run_bot(), telegram_command_listener())


if __name__ == "__main__":
    send_telegram_message(
        "✅ pump.fun scout bot started. Watching for early-momentum launches.\n"
        "Send /recent anytime to see the latest scanned tokens.\n"
        "Reminder: alerts are heuristic screens, not buy signals."
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user.")
