import asyncio
import json
import os
import random
import re
import traceback
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
import requests
import yaml
from discord.ext import commands
from playwright.async_api import async_playwright

CONFIG_PATH = "config.yaml"
STATE_PATH = "state.json"
BOT_TOKEN_PATH = "bot_token.txt"


# ---------- Config / State ----------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {
            "seen_search_items": [],
            "product_store_status": {},
            "last_alert_times": {},
            "monitor_enabled": True,
            "last_cycle_started_at": None,
            "last_cycle_finished_at": None,
            "last_cycle_success": None,
            "last_cycle_error": None,
            "last_heartbeat_at": None,
        }
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("seen_search_items", [])
    data.setdefault("product_store_status", {})
    data.setdefault("last_alert_times", {})
    data.setdefault("monitor_enabled", True)
    data.setdefault("last_cycle_started_at", None)
    data.setdefault("last_cycle_finished_at", None)
    data.setdefault("last_cycle_success", None)
    data.setdefault("last_cycle_error", None)
    data.setdefault("last_heartbeat_at", None)
    return data


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_bot_token():
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token

    if os.path.exists(BOT_TOKEN_PATH):
        with open(BOT_TOKEN_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()

    raise RuntimeError(
        "No Discord bot token found. Put it in bot_token.txt "
        "or set DISCORD_BOT_TOKEN."
    )


# ---------- Webhooks ----------

def get_status_webhook(config):
    alerts = config.get("alerts", {}) if config else {}
    return alerts.get("status_webhook") or config.get("discord_webhook", "")


def get_error_webhook(config):
    alerts = config.get("alerts", {}) if config else {}
    return alerts.get("error_webhook") or config.get("discord_webhook", "")


def post_discord_message(webhook: str, message: str):
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": message}, timeout=20)
    except Exception as e:
        print(f"Discord webhook error: {e}")


def post_discord_embed(webhook: str, embed: dict):
    if not webhook:
        return
    try:
        requests.post(webhook, json={"embeds": [embed]}, timeout=20)
    except Exception as e:
        print(f"Discord embed error: {e}")


def report_error(config, title: str, error_text: str, location: str = ""):
    error_text = (error_text or "").strip()
    if len(error_text) > 3500:
        error_text = error_text[:3500] + "..."

    embed = {
        "title": f"⚠️ {title}",
        "description": error_text or "Unknown error",
        "color": 0xE74C3C,
        "timestamp": utc_now_iso(),
        "footer": {"text": "Pokemon monitor error reporter"},
    }
    if location:
        embed["fields"] = [{"name": "Location", "value": location, "inline": False}]

    post_discord_embed(get_error_webhook(config), embed)


# ---------- Monitoring helpers ----------

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def extract_quantity(status_obj):
    if isinstance(status_obj, dict):
        qty = status_obj.get("quantity")
        return qty if isinstance(qty, int) else 0
    return 0


def format_price(value):
    try:
        return f"${float(value):.2f}"
    except Exception:
        return "Unknown"


def status_is_available(status_obj) -> bool:
    if not isinstance(status_obj, dict):
        return False

    qty = extract_quantity(status_obj)
    orderable = bool(status_obj.get("orderable", False))
    pickup = bool(status_obj.get("pickup_enabled", False))
    ship = bool(status_obj.get("ship_enabled", False))

    return qty > 0 or orderable or pickup or ship


def previous_was_available(previous) -> bool:
    return status_is_available(previous)


def should_send_stock_alert(previous, current, send_initial_stock_alerts: bool) -> bool:
    current_available = status_is_available(current)
    previous_available = previous_was_available(previous)

    if previous is None:
        return send_initial_stock_alerts and current_available

    if not isinstance(current, dict):
        return False

    if not current_available:
        return False

    prev_qty = extract_quantity(previous)
    curr_qty = extract_quantity(current)

    if not previous_available and current_available:
        return True

    if prev_qty != curr_qty and curr_qty > 0:
        return True

    prev_orderable = bool(previous.get("orderable", False)) if isinstance(previous, dict) else False
    curr_orderable = bool(current.get("orderable", False))
    prev_pickup = bool(previous.get("pickup_enabled", False)) if isinstance(previous, dict) else False
    curr_pickup = bool(current.get("pickup_enabled", False))
    prev_ship = bool(previous.get("ship_enabled", False)) if isinstance(previous, dict) else False
    curr_ship = bool(current.get("ship_enabled", False))

    return (
        prev_orderable != curr_orderable
        or prev_pickup != curr_pickup
        or prev_ship != curr_ship
    )


def cooldown_ok(state: dict, key: str, cooldown_minutes: int) -> bool:
    last_map = state.setdefault("last_alert_times", {})
    last = last_map.get(key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last_dt >= timedelta(minutes=cooldown_minutes)


def mark_alert_time(state: dict, key: str):
    state.setdefault("last_alert_times", {})[key] = utc_now_iso()


def build_webhook_stock_embed(store_name: str, product: dict, current: dict):
    price_text = format_price(current.get("price"))
    qty = extract_quantity(current)

    embed = {
        "title": product["name"],
        "url": product["url"],
        "description": "Canadian Tire stock alert",
        "fields": [
            {"name": "Store", "value": store_name, "inline": True},
            {"name": "Price", "value": price_text, "inline": True},
            {"name": "Quantity", "value": str(qty), "inline": True},
            {"name": "Pickup", "value": "✅" if current.get("pickup_enabled") else "❌", "inline": True},
            {"name": "Ship", "value": "✅" if current.get("ship_enabled") else "❌", "inline": True},
            {"name": "Orderable", "value": "✅" if current.get("orderable") else "❌", "inline": True},
        ],
        "footer": {"text": "Pokemon monitor"},
        "timestamp": utc_now_iso(),
    }

    image_url = product.get("image_url")
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    return embed


def build_status_embed(product: dict, stores: list, state: dict):
    embed = discord.Embed(
        title=product["name"],
        url=product["url"],
        description="Current cached stock status",
        color=0x3498DB,
        timestamp=datetime.now(timezone.utc),
    )
    if product.get("image_url"):
        embed.set_thumbnail(url=product["image_url"])

    rows = []
    any_available = False

    for store in stores:
        key = f"{store['name']} || {product['name']}"
        current = state.get("product_store_status", {}).get(key, {})

        qty = current.get("quantity")
        price = current.get("price")
        pickup = current.get("pickup_enabled")
        ship = current.get("ship_enabled")
        orderable = current.get("orderable")

        qty_known = qty is not None
        qty_value = qty if isinstance(qty, int) else None
        price_text = f"${float(price):.2f}" if price is not None else "Unknown"

        # Status bucket and emoji
        if qty_value is not None and qty_value > 0:
            bucket = 0
            emoji = "🟢"
            any_available = True
        elif qty_known and qty_value == 0 and pickup is False and ship is False and orderable is False:
            bucket = 2
            emoji = "🔴"
        elif status_is_available(current):
            bucket = 1
            emoji = "🟢"
            any_available = True
        elif qty_known and qty_value == 0:
            bucket = 1
            emoji = "🟡"
        else:
            bucket = 1
            emoji = "🟡"

        qty_text = str(qty) if qty is not None else "Unknown"
        pickup_text = "✅" if pickup else "❌"
        ship_text = "✅" if ship else "❌"
        order_text = "✅" if orderable else "❌"

        line = (
            f"{emoji} **{store['name']}** — "
            f"Stock {qty_text} | "
            f"Price {price_text} | "
            f"Pickup {pickup_text} | "
            f"Ship {ship_text} | "
            f"Orderable {order_text}"
        )
        rows.append((bucket, store["name"].lower(), line))

    rows.sort(key=lambda x: (x[0], x[1]))
    lines = [row[2] for row in rows]

    chunk = ""
    field_index = 1
    for line in lines:
        if len(chunk) + len(line) + 1 > 1000:
            embed.add_field(name=f"Stores {field_index}", value=chunk, inline=False)
            field_index += 1
            chunk = line
        else:
            chunk = f"{chunk}\n{line}".strip()
    if chunk:
        embed.add_field(name=f"Stores {field_index}", value=chunk, inline=False)

    embed.add_field(
        name="Available Anywhere",
        value="✅ Yes" if any_available else "❌ No",
        inline=True,
    )
    embed.add_field(
        name="Product Code",
        value=str(product["pcode"]),
        inline=True,
    )
    embed.set_footer(text="Canadian Tire TCG Monitor")
    return embed


def item_to_pcode(item_or_pcode: str) -> str:
    raw = item_or_pcode.strip().lower()
    if raw.endswith("p"):
        return raw

    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 7:
        return digits[:7] + "p"

    raise ValueError("Could not convert item number to product code.")


def build_heartbeat_embed(config, state):
    cfg = config or {}
    st = state or {}

    embed = {
        "title": "✅ Bot heartbeat",
        "description": "Monitor and Discord control bot are running.",
        "color": 0x2ECC71 if st.get("last_cycle_success", True) else 0xF1C40F,
        "fields": [
            {"name": "Monitor enabled", "value": "✅" if st.get("monitor_enabled", True) else "❌", "inline": True},
            {"name": "Interval", "value": f"{cfg.get('check_interval_seconds', 30)} seconds", "inline": True},
            {"name": "Products", "value": str(len(cfg.get("products", []))), "inline": True},
            {"name": "Stores", "value": str(len(cfg.get("stores", []))), "inline": True},
            {"name": "Last cycle started", "value": str(st.get("last_cycle_started_at") or "Unknown"), "inline": False},
            {"name": "Last cycle finished", "value": str(st.get("last_cycle_finished_at") or "Unknown"), "inline": False},
            {"name": "Last cycle result", "value": "✅ Success" if st.get("last_cycle_success") else f"⚠️ {st.get('last_cycle_error') or 'Unknown'}", "inline": False},
        ],
        "timestamp": utc_now_iso(),
        "footer": {"text": "12-hour heartbeat"},
    }
    return embed


# ---------- Playwright ----------

async def dismiss_popups(page):
    candidates = [
        "button:has-text('Accept')",
        "button:has-text('I Accept')",
        "button:has-text('Close')",
        "[aria-label='Close']",
        "[data-testid='close']",
    ]
    for selector in candidates:
        try:
            await page.locator(selector).first.click(timeout=1500)
        except Exception:
            pass


async def extract_search_items(page, url: str):
    found = set()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        await dismiss_popups(page)

        links = page.locator("a")
        count = await links.count()

        for i in range(min(count, 400)):
            try:
                link = links.nth(i)
                href = await link.get_attribute("href")
                text = normalize_text(await link.inner_text())

                if not href or not text:
                    continue

                href_low = href.lower()
                text_low = text.lower()

                is_candidate = (
                    "/pdp/" in href_low
                    or "pokemon" in text_low
                    or "trading card" in text_low
                    or "booster" in text_low
                    or "blister" in text_low
                    or "checklane" in text_low
                )

                if is_candidate:
                    found.add(f"{text} | {href}")
            except Exception:
                continue

    except Exception as e:
        print(f"Search extraction error for {url}: {e}")

    return found


async def read_product_status(page, store_id: str, pcode: str):
    url = (
        "https://www.canadiantire.ca/api/v1/product/api/v2/product/sku/PriceAvailability"
        f"?lang=en_CA&storeId={store_id}&cache=true&pCode={pcode}"
    )

    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if not response:
            return {"error": "NO_RESPONSE"}

        text = await page.locator("body").inner_text()
        data = json.loads(text)
        sku = (data.get("skus") or [{}])[0]

        try:
            price = sku["currentPrice"]["value"]
        except Exception:
            price = None

        try:
            quantity = sku["fulfillment"]["availability"]["Corporate"]["quantity"]
        except Exception:
            quantity = None

        try:
            pickup_enabled = sku["fulfillment"]["storePickup"]["enabled"]
        except Exception:
            pickup_enabled = None

        try:
            ship_enabled = sku["fulfillment"]["shipToHome"]["enabled"]
        except Exception:
            ship_enabled = None

        return {
            "quantity": quantity,
            "orderable": sku.get("orderable"),
            "pickup_enabled": pickup_enabled,
            "ship_enabled": ship_enabled,
            "price": price,
        }

    except Exception as e:
        print(f"API stock read error: {e}")
        return {"error": str(e)}


# ---------- App globals ----------

config = load_config()
state = load_state()
monitor_lock = asyncio.Lock()
monitor_task = None
heartbeat_task = None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ---------- Monitor ----------

async def run_monitor_cycle(send_alerts=True):
    global config, state
    config = load_config()
    state = load_state()
    state["last_cycle_started_at"] = utc_now_iso()
    state["last_cycle_error"] = None
    save_state(state)

    async with monitor_lock:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=config.get("headless", True))
                page = await browser.new_page()

                search_urls = config.get("search_urls", [])
                old_seen = set(state.get("seen_search_items", []))
                new_seen = set(old_seen)
                listing_webhook = config.get("alerts", {}).get("new_listing_webhook") or config.get("discord_webhook", "")
                cooldown_minutes = int(config.get("alerts", {}).get("cooldown_minutes", 60))

                for search_url in search_urls:
                    items = await extract_search_items(page, search_url)
                    newly_found = items - old_seen
                    for item in sorted(newly_found):
                        alert_key = f"listing::{item}"
                        if send_alerts and cooldown_ok(state, alert_key, cooldown_minutes):
                            post_discord_message(listing_webhook, f"🆕 New Canadian Tire listing detected:\n{item}")
                            mark_alert_time(state, alert_key)
                    new_seen.update(items)

                state["seen_search_items"] = sorted(new_seen)
                await page.close()
                await browser.close()

                product_store_status = state.get("product_store_status", {})
                send_initial_stock_alerts = bool(config.get("alerts", {}).get("send_initial_stock_alerts", False))
                min_delay_ms = int(config.get("natural_delay_min_ms", 700))
                max_delay_ms = int(config.get("natural_delay_max_ms", 1400))

                for store in config["stores"]:
                    store_name = store["name"]
                    profile_dir = Path(store["profile_dir"])
                    profile_dir.mkdir(parents=True, exist_ok=True)

                    print(f"\nChecking store profile: {store_name}")

                    context = await p.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        headless=config.get("headless", True),
                        viewport={"width": 1440, "height": 1000},
                    )
                    page = context.pages[0] if context.pages else await context.new_page()

                    try:
                        await page.goto("https://www.canadiantire.ca/en.html", wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(random.randint(min_delay_ms, max_delay_ms))
                        await dismiss_popups(page)

                        for product in config["products"]:
                            await page.wait_for_timeout(random.randint(min_delay_ms, max_delay_ms))

                            key = f"{store_name} || {product['name']}"
                            current = await read_product_status(page, store["store_id"], product["pcode"])
                            previous = product_store_status.get(key)

                            if send_alerts and should_send_stock_alert(previous, current, send_initial_stock_alerts):
                                if cooldown_ok(state, key, cooldown_minutes):
                                    embed = build_webhook_stock_embed(store_name, product, current)
                                    post_discord_embed(config.get("discord_webhook", ""), embed)
                                    mark_alert_time(state, key)

                            product_store_status[key] = current

                    finally:
                        await context.close()

                state["product_store_status"] = product_store_status
                state["last_cycle_finished_at"] = utc_now_iso()
                state["last_cycle_success"] = True
                state["last_cycle_error"] = None
                save_state(state)

        except Exception as e:
            state["last_cycle_finished_at"] = utc_now_iso()
            state["last_cycle_success"] = False
            state["last_cycle_error"] = str(e)
            save_state(state)
            report_error(config, "Monitor cycle error", traceback.format_exc(), "run_monitor_cycle")
            raise


async def monitor_loop():
    global config, state
    while True:
        try:
            config = load_config()
            state = load_state()
            if state.get("monitor_enabled", True):
                print("\n=== Monitor cycle ===")
                await run_monitor_cycle(send_alerts=True)
            else:
                print("\n=== Monitor paused ===")
        except Exception as e:
            print(f"Monitor loop error: {e}")
            save_state(state)

        interval = int(load_config().get("check_interval_seconds", 30))
        print(f"Sleeping for {interval} seconds...")
        await asyncio.sleep(interval)


async def heartbeat_loop():
    global config, state
    while True:
        try:
            await asyncio.sleep(60)
            config = load_config()
            state = load_state()

            last_sent = state.get("last_heartbeat_at")
            should_send = True
            if last_sent:
                try:
                    last_dt = datetime.fromisoformat(last_sent)
                    should_send = (datetime.now(timezone.utc) - last_dt) >= timedelta(hours=12)
                except Exception:
                    should_send = True

            if should_send:
                post_discord_embed(get_status_webhook(config), build_heartbeat_embed(config, state))
                state["last_heartbeat_at"] = utc_now_iso()
                save_state(state)

        except Exception:
            report_error(load_config(), "Heartbeat error", traceback.format_exc(), "heartbeat_loop")


# ---------- Discord bot ----------

@bot.event
async def on_ready():
    global monitor_task, heartbeat_task
    print(f"Bot online as {bot.user}")
    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(monitor_loop())
    if heartbeat_task is None or heartbeat_task.done():
        heartbeat_task = asyncio.create_task(heartbeat_loop())


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"⚠️ Command error: {error}")
    report_error(load_config(), "Command error", traceback.format_exc(), f"command: {getattr(ctx.command, 'name', 'unknown')}")


@bot.command(name="helpme")
async def helpme(ctx):
    msg = (
        "**Commands**\n"
        "`!status` - cached status for all monitored products\n"
        "`!status <product words>` - cached status for matching product\n"
        "`!products` - list monitored products\n"
        "`!stores` - list monitored stores\n"
        "`!checknow` - run an immediate live stock check\n"
        "`!addproduct Name | Item# or PCode | URL | ImageURL`\n"
        "`!removeproduct <name or pcode>`\n"
        "`!monitorstart` / `!monitorstop`\n"
        "`!interval <seconds>` - change polling interval\n"
        "`!reloadconfig` - reload config from disk\n"
        "`!heartbeatnow` - send immediate bot health report\n"
    )
    await ctx.send(msg)


@bot.command(name="stores")
async def stores_cmd(ctx):
    cfg = load_config()
    text = "\n".join(f"- {s['name']} (`{s['store_id']}`)" for s in cfg["stores"])
    await ctx.send(f"**Tracked stores**\n{text}")


@bot.command(name="products")
async def products_cmd(ctx):
    cfg = load_config()
    for product in cfg["products"]:
        embed = discord.Embed(
            title=product["name"],
            description=f"[View Product]({product['url']})",
            color=0x3498DB
        )
        embed.add_field(name="Product Code", value=str(product["pcode"]), inline=True)
        if product.get("image_url"):
            embed.set_thumbnail(url=product["image_url"])
        await ctx.send(embed=embed)


@bot.command(name="status")
async def status_cmd(ctx, *, query: str = ""):
    cfg = load_config()
    current_state = load_state()

    products = cfg["products"]
    if query:
        q = query.lower().strip()
        products = [p for p in products if q in p["name"].lower() or q in str(p["pcode"]).lower()]
        if not products:
            await ctx.send("No matching product found.")
            return

    for product in products:
        await ctx.send(embed=build_status_embed(product, cfg["stores"], current_state))


@bot.command(name="checknow")
async def checknow_cmd(ctx):
    if monitor_lock.locked():
        await ctx.send("A check is already running.")
        return

    msg = await ctx.send("Running live stock check now...")
    await run_monitor_cycle(send_alerts=False)
    await msg.edit(content="Live stock check complete. Use `!status` to view results.")


@bot.command(name="monitorstart")
async def monitorstart_cmd(ctx):
    current_state = load_state()
    current_state["monitor_enabled"] = True
    save_state(current_state)
    await ctx.send("Monitor enabled.")


@bot.command(name="monitorstop")
async def monitorstop_cmd(ctx):
    current_state = load_state()
    current_state["monitor_enabled"] = False
    save_state(current_state)
    await ctx.send("Monitor paused after current cycle finishes.")


@bot.command(name="reloadconfig")
async def reloadconfig_cmd(ctx):
    global config
    config = load_config()
    await ctx.send("Config reloaded from disk.")


@bot.command(name="interval")
async def interval_cmd(ctx, seconds: int):
    if seconds < 10:
        await ctx.send("Use 10 seconds or more.")
        return

    cfg = load_config()
    cfg["check_interval_seconds"] = seconds
    save_config(cfg)
    await ctx.send(f"Check interval updated to {seconds} seconds.")


@bot.command(name="heartbeatnow")
async def heartbeatnow_cmd(ctx):
    cfg = load_config()
    st = load_state()
    embed = build_heartbeat_embed(cfg, st)
    await ctx.send(embed=discord.Embed.from_dict(embed))


@bot.command(name="addproduct")
async def addproduct_cmd(ctx, *, raw: str):
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 4:
        await ctx.send("Use: `!addproduct Name | Item# or PCode | URL | ImageURL`")
        return

    name, item_or_pcode, url, image_url = parts
    try:
        pcode = item_to_pcode(item_or_pcode)
    except ValueError as e:
        await ctx.send(str(e))
        return

    cfg = load_config()
    if any(str(p["pcode"]).lower() == pcode.lower() for p in cfg["products"]):
        await ctx.send("That product code is already being monitored.")
        return

    cfg["products"].append({
        "name": name,
        "pcode": pcode,
        "url": url,
        "image_url": image_url
    })
    save_config(cfg)

    embed = discord.Embed(
        title="Product added",
        description=f"**{name}**\n[View Product]({url})",
        color=0x2ECC71
    )
    embed.add_field(name="Product Code", value=pcode, inline=True)
    if image_url:
        embed.set_thumbnail(url=image_url)

    await ctx.send(embed=embed)


@bot.command(name="removeproduct")
async def removeproduct_cmd(ctx, *, query: str):
    cfg = load_config()
    q = query.lower().strip()

    kept = []
    removed = None
    for product in cfg["products"]:
        if removed is None and (q in product["name"].lower() or q == str(product["pcode"]).lower()):
            removed = product
        else:
            kept.append(product)

    if removed is None:
        await ctx.send("No matching product found.")
        return

    cfg["products"] = kept
    save_config(cfg)
    await ctx.send(f"Removed **{removed['name']}** (`{removed['pcode']}`).")


TOKEN = load_bot_token()
bot.run(TOKEN)
