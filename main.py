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

# Sensitivity tiers, selectable in Telegram via /tier. These are NOT
# scientific predictions that a token will actually hit that multiple —
# they're just stricter/looser versions of the same momentum filter.
TIER_PRESETS = {
    "2x":  {"min_buyers": 4,  "min_volume_sol": 1.5,  "min_ratio": 1.1},
    "5x":  {"min_buyers": 6,  "min_volume_sol": 3.0,  "min_ratio": 1.3},
    "10x": {"min_buyers": 8,  "min_volume_sol": 5.0,  "min_ratio": 1.5},
    "20x": {"min_buyers": 12, "min_volume_sol": 8.0,  "min_ratio": 1.8},
    "30x": {"min_buyers": 15, "min_volume_sol": 12.0, "min_ratio": 2.0},
    "40x": {"min_buyers": 18, "min_volume_sol": 16.0, "min_ratio": 2.2},
    "50x": {"min_buyers": 22, "min_volume_sol": 20.0, "min_ratio": 2.5},
}

active_tier = {"name": "10x", **TIER_PRESETS["10x"]}

TIER_KEYBOARD_MARKUP = {
    "inline_keyboard": [
        [{"text": t, "callback_data": f"tier_{t}"} for t in ["2x", "5x", "10x"]],
        [{"text": t, "callback_data": f"tier_{t}"} for t in ["20x", "30x", "40x"]],
        [{"text": "50x", "callback_data": "tier_50x"}],
    ]
}

NAME_BLOCKLIST_SUBSTRINGS = ["test", "scam", "airdrop claim"]
MAX_TRACKED_TOKENS = 500
SEND_ACTIVITY_LOG_TO_TELEGRAM = False
RECENT_HISTORY_MAX = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pumpfun_scout")

CLOSE_BUTTON_MARKUP = {
    "inline_keyboard": [[{"text": "❌ Close", "callback_data": "close"}]]
}


def send_telegram_message(text: str, with_close_button: bool = False) -> None:
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
    if with_close_button:
        payload["reply_markup"] = json.dumps(CLOSE_BUTTON_MARKUP)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("Telegram send exception: %s", e)


def delete_telegram_message(chat_id, message_id) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
    except requests.RequestException as e:
        log.error("Telegram delete exception: %s", e)


def answer_callback_query(callback_query_id: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id}, timeout=10)
    except requests.RequestException as e:
        log.error("Telegram answerCallbackQuery exception: %s", e)


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
    if len(state.buyers) < active_tier["min_buyers"]:
        return None
    if state.buy_volume_sol < active_tier["min_volume_sol"]:
        return None
    if state.buy_sell_ratio() < active_tier["min_ratio"]:
        return None

    state.alerted = True
    pumpfun_link = f"https://pump.fun/{state.mint}"
    solscan_link = f"https://solscan.io/token/{state.mint}"
    return (
        f"🚨 *Early momentum flag ({active_tier['name']} filter)* 🚨\n"
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
        send_telegram_message(alert, with_close_button=True)


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


def send_tier_picker() -> None:
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — cannot send tier picker.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": (
            f"Pick a filter strength (current: *{active_tier['name']}*).\n"
            f"Higher = stricter filter, not a guaranteed multiple."
        ),
        "parse_mode": "Markdown",
        "reply_markup": json.dumps(TIER_KEYBOARD_MARKUP),
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("Telegram send exception: %s", e)


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

                callback = update.get("callback_query")
                if callback:
                    cb_chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
                    data = callback.get("data", "")
                    if cb_chat_id != str(TELEGRAM_CHAT_ID):
                        continue

                    if data == "close":
                        message_id = callback.get("message", {}).get("message_id")
                        await asyncio.to_thread(delete_telegram_message, cb_chat_id, message_id)
                        await asyncio.to_thread(answer_callback_query, callback.get("id"))
                    elif data.startswith("tier_"):
                        tier_name = data[len("tier_"):]
                        if tier_name in TIER_PRESETS:
                            active_tier["name"] = tier_name
                            active_tier.update(TIER_PRESETS[tier_name])
                            send_telegram_message(
                                f"🎯 Filter set to *{tier_name}*.\n"
                                f"Min buyers: {active_tier['min_buyers']} | "
                                f"Min volume: {active_tier['min_volume_sol']} SOL | "
                                f"Min buy/sell ratio: {active_tier['min_ratio']}x\n\n"
                                f"Reminder: this doesn't guarantee a {tier_name} outcome — "
                                f"it just changes how strict the momentum filter is."
                            )
                        await asyncio.to_thread(answer_callback_query, callback.get("id"))
                    continue

                message = update.get("message") or update.get("edited_message") or {}
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = (message.get("text") or "").strip()

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text.startswith("/recent"):
                    send_telegram_message(build_recent_message())
                elif text.startswith("/tier"):
                    await asyncio.to_thread(send_tier_picker)
                elif text.startswith("/start") or text.startswith("/help"):
                    send_telegram_message(
                        "Commands:\n"
                        "/recent — show the last tokens scanned\n"
                        "/tier — choose how strict the momentum filter is (2x–50x)\n\n"
                        f"Current filter: *{active_tier['name']}*"
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
