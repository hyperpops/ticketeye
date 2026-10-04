"""
EYE ticket bot v2
-----------------
- Panel dropdown: purchase / support tickets
- Purchase tickets: guided steps (product -> duration / username -> payment), then pings the owner
- Support tickets: quick form (topic + details) before the channel is created
- Ticket numbers, claim button, close confirmation, optional transcript log
- /panel (admins), /add and /remove (staff)

Run:  pip install -r requirements.txt
      put DISCORD_TOKEN in a .env file (see .env.example)
      python bot.py
Then run /panel in your server.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
import traceback
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from keep_alive import keep_alive

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1500924795477430272
PANEL_CHANNEL_ID = 1556097636761608262      # where /panel posts the panel
TICKET_CATEGORY_ID = 1556095650175778909    # tickets are created in here
OWNER_ID = 1492532153437913378              # pinged when an order is submitted
STAFF_ROLE_IDS: list[int] = []              # optional: roles that can see/claim/close tickets

LOG_CHANNEL_ID: int | None = None           # optional: closed tickets are logged here with a transcript
READ_MESSAGE_CONTENT = False                # True = transcripts include message text (also enable
                                            # "Message Content Intent" for the bot in the Developer Portal)

PURCHASE_NAME = "purchase"                  # channel name becomes purchase-username
SUPPORT_NAME = "support"                    # channel name becomes support-username
APPEND_USERNAME = True                      # adds the username of whoever opened the ticket
COOLDOWN_SECONDS = 15                       # anti-spam between ticket creations

# Discord attachment links expire. Host these two images somewhere permanent
# (for example on your website) and paste the new links here.
PANEL_IMAGE = (
    "https://media.discordapp.net/attachments/1551693461147746328/1556115924321439815/"
    "eye-banner-tickets-1920x1080.png?backend=b2&ex=6ac2fd63&is=6ac1abe3&"
    "hm=f61374b66ddf963092ac9c174d910e7ca5280047ab9b97f959f9fbab028038c3&=&format=webp"
    "&quality=lossless&width=1536&height=864"
)
LOGO = (
    "https://media.discordapp.net/attachments/1551693461147746328/1556103325022429225/"
    "eye-logo-white-1024.png?backend=b2&ex=6ac2f1a7&is=6ac1a027&"
    "hm=b09867f6bf9af76b35711877b19c602b731c76f74d7b9a3b5a84f0f8bb8e423b&=&format=webp"
    "&quality=lossless"
)
FOOTER_TEXT = "Provided by @ Eye all rights reserved."
EMBED_COLOR = discord.Color.from_rgb(255, 255, 255)

DATA_FILE = Path(__file__).with_name("ticket_data.json")
SEP = "▬" * 16

# --------------------------------------------------------------------------
# CATALOG
# --------------------------------------------------------------------------
PRODUCTS = {
    "dma": dict(name="DMA", emoji="🖥️", type="Product", kind="physical", price=399),
    "hunter": dict(
        name="Hunter", emoji="🏹", type="Product", kind="keys",
        durations=[("3 Day Key", 50), ("Weekly Key", 80), ("1 Month Key", 175), ("Lifetime Key", 250)],
    ),
    "eclipse": dict(
        name="Eclipse", emoji="🌑", type="Product", kind="keys",
        durations=[("3 Day Key", 35), ("Weekly Key", 60), ("Monthly Key", 90), ("Lifetime Key", 140)],
    ),
    "oneclick": dict(
        name="Oneclick", emoji="⚡", type="Serial", kind="keys",
        durations=[("3 Day Key", 40), ("1 Week Key", 70), ("Monthly Key", 130), ("Lifetime Key", 250)],
    ),
    "emulator": dict(
        name="Emulator", emoji="💽", type="Serial", kind="keys",
        durations=[("3 Day Key", 30), ("Weekly Key", 50), ("Monthly Key", 85), ("Lifetime Key", 120)],
    ),
    "unban_valorant": dict(name="Valorant Account Unban", emoji="🔺", type="Account unban", kind="unban", price=120),
    "unban_fortnite": dict(name="Fortnite Account Unban", emoji="🪂", type="Account unban", kind="unban", price=90),
    "unban_roblox": dict(name="Roblox Account Unban", emoji="🧱", type="Account unban", kind="unban", price=70),
}

PAYMENTS = {
    "crypto": ("Cryptocurrency", "🪙", "Any cryptocurrency"),
    "card": ("Credit / Debit card", "💳", "Pay by credit or debit card"),
    "paypal": ("PayPal", "🅿️", "Pay with PayPal"),
    "revolut": ("Revolut", "🔵", "Pay with Revolut"),
    "giftcards": ("Gift cards", "🎁", "Pay with gift cards"),
}

LABELS = {"product": "Product", "duration": "Duration", "username": "Username", "payment": "Payment"}
PROMPTS = {
    "product": ("Choose your product", "Pick what you would like from the menu below."),
    "duration": ("Choose your duration", "Pick how long you want your key to last."),
    "username": (
        "Enter the account username",
        "Press the button below and type the username of the banned account.\n"
        "**We never need a password or login. Please do not send one.**",
    ),
    "payment": ("Choose your payment method", "Pick how you would like to pay. The payment is completed here in the ticket."),
}

# --------------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------------
COOLDOWNS: dict[int, float] = {}
_owner_cache: dict[int, discord.Member] = {}


def make_embed(title: str | None = None, description: str | None = None, *, image: str | None = None) -> discord.Embed:
    """Every embed goes through here so color, header and footer are always identical."""
    e = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    e.set_author(name="EYE", icon_url=LOGO)
    e.set_footer(text=FOOTER_TEXT, icon_url=LOGO)
    if image:
        e.set_image(url=image)
    return e


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "user"


def is_staff(member: discord.Member) -> bool:
    return (
        member.id == OWNER_ID
        or member.guild_permissions.manage_channels
        or any(r.id in STAFF_ROLE_IDS for r in member.roles)
    )


def parse_topic(topic: str | None) -> dict | None:
    """Ticket info lives in the channel topic: 'eye-ticket | owner:ID | type:kind | n:NUMBER'."""
    if not topic or "eye-ticket" not in topic:
        return None
    owner = re.search(r"owner:(\d+)", topic)
    kind = re.search(r"type:(\w+)", topic)
    number = re.search(r"n:(\d+)", topic)
    return {
        "owner": int(owner.group(1)) if owner else None,
        "type": kind.group(1) if kind else None,
        "n": int(number.group(1)) if number else 0,
    }


def next_ticket_number() -> int:
    try:
        data = json.loads(DATA_FILE.read_text())
    except Exception:
        data = {"counter": 0}
    data["counter"] = int(data.get("counter", 0)) + 1
    try:
        DATA_FILE.write_text(json.dumps(data))
    except OSError:
        pass
    return data["counter"]


def find_existing(category: discord.CategoryChannel, user_id: int, kind: str):
    for ch in category.text_channels:
        info = parse_topic(ch.topic)
        if info and info["owner"] == user_id and info["type"] == kind:
            return ch
    return None


async def get_owner_member(guild: discord.Guild):
    cached = _owner_cache.get(guild.id)
    if cached:
        return cached
    member = guild.get_member(OWNER_ID)
    if member is None:
        try:
            member = await guild.fetch_member(OWNER_ID)
        except discord.HTTPException:
            return None
    _owner_cache[guild.id] = member
    return member


async def reply(interaction: discord.Interaction, embed: discord.Embed, **kwargs):
    """Send an ephemeral message whether or not the interaction was already answered."""
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True, **kwargs)


# --------------------------------------------------------------------------
# ORDER FLOW (purchase tickets)
# --------------------------------------------------------------------------
class Order:
    def __init__(self, user_id: int, number: int):
        self.user_id = user_id
        self.number = number
        self.product: str | None = None
        self.duration: int | None = None
        self.username: str | None = None
        self.payment: str | None = None

    @property
    def item(self) -> dict:
        return PRODUCTS[self.product]

    @property
    def steps(self) -> list[str]:
        kind = self.item["kind"] if self.product else "keys"
        if kind == "physical":
            return ["product", "payment"]
        if kind == "unban":
            return ["product", "username", "payment"]
        return ["product", "duration", "payment"]

    @property
    def price(self) -> int | None:
        it = self.item
        if it["kind"] in ("physical", "unban"):
            return it["price"]
        return None if self.duration is None else it["durations"][self.duration][1]

    def rows(self) -> list[tuple[str, str]]:
        it = self.item
        rows = [("Product", it["name"])]
        if it["kind"] == "keys" and self.duration is not None:
            rows.append(("Duration", it["durations"][self.duration][0]))
        if it["kind"] == "unban" and self.username:
            rows.append(("Username", self.username))
        if self.price is not None:
            rows.append(("Price", f"${self.price}"))
        if self.payment:
            rows.append(("Payment", PAYMENTS[self.payment][0]))
        return rows


def stepper(steps: list[str], current: str) -> str:
    idx = steps.index(current)
    parts = []
    for i, s in enumerate(steps):
        if i < idx:
            parts.append(f"● {LABELS[s]}")
        elif i == idx:
            parts.append(f"◉ **{LABELS[s]}**")
        else:
            parts.append(f"○ {LABELS[s]}")
    return "  ─  ".join(parts)


def receipt(order: Order) -> str:
    rows = order.rows()
    width = max(len(k) for k, _ in rows)
    body = "\n".join(f"{k.upper().ljust(width)}   {v}" for k, v in rows)
    return f"```\n{body}\n```"


def step_embed(order: Order, step: str) -> discord.Embed:
    title, prompt = PROMPTS[step]
    lines = [stepper(order.steps, step), SEP, ""]
    if order.product and step != "product":
        lines += [f"> **{k}:** {v}" for k, v in order.rows() if k != "Payment"] + [""]
    lines.append(prompt)
    return make_embed(title, "\n".join(lines))


async def show_step(interaction: discord.Interaction, order: Order, step: str):
    await interaction.response.edit_message(embed=step_embed(order, step), view=OrderView(order, step))


class ProductSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for key, p in PRODUCTS.items():
            if p["kind"] == "keys":
                desc = f"{p['type']} - from ${min(d[1] for d in p['durations'])}"
            else:
                desc = f"{p['type']} - ${p['price']}"
            options.append(discord.SelectOption(label=p["name"], value=key, emoji=p["emoji"], description=desc))
        super().__init__(placeholder="Select a product", options=options)

    async def callback(self, interaction: discord.Interaction):
        order: Order = self.view.order
        order.product, order.duration, order.username = self.values[0], None, None
        await show_step(interaction, order, order.steps[1])


class DurationSelect(discord.ui.Select):
    def __init__(self, product: str):
        options = [
            discord.SelectOption(label=name, value=str(i), description=f"${price}")
            for i, (name, price) in enumerate(PRODUCTS[product]["durations"])
        ]
        super().__init__(placeholder="Select a duration", options=options)

    async def callback(self, interaction: discord.Interaction):
        order: Order = self.view.order
        order.duration = int(self.values[0])
        await show_step(interaction, order, "payment")


class UsernameModal(discord.ui.Modal, title="Account username"):
    username = discord.ui.TextInput(
        label="Username of the banned account",
        placeholder="Username only. Never your password.",
        min_length=1, max_length=64,
    )

    def __init__(self, order: Order):
        super().__init__()
        self.order = order

    async def on_submit(self, interaction: discord.Interaction):
        self.order.username = self.username.value.strip()
        await show_step(interaction, self.order, "payment")


class UsernameButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Enter username", style=discord.ButtonStyle.secondary, emoji="✏️")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UsernameModal(self.view.order))


class PaymentSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name, value=key, emoji=emoji, description=desc)
            for key, (name, emoji, desc) in PAYMENTS.items()
        ]
        super().__init__(placeholder="Select a payment method", options=options)

    async def callback(self, interaction: discord.Interaction):
        order: Order = self.view.order
        order.payment = self.values[0]

        done = make_embed(
            "Order submitted",
            f"{receipt(order)}\nYour order was sent to the team. Someone will be with you shortly to finish the payment.",
        )
        summary = make_embed(f"New order #{order.number:04d}", f"A customer finished the steps and is waiting.\n{receipt(order)}")
        summary.add_field(name="Customer", value=f"<@{order.user_id}>", inline=True)
        summary.add_field(name="Ticket", value=interaction.channel.mention, inline=True)
        summary.timestamp = discord.utils.utcnow()

        await interaction.response.edit_message(embed=done, view=None)
        await interaction.channel.send(
            content=f"<@{OWNER_ID}>",
            embed=summary,
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=OWNER_ID)]),
        )


class BackButton(discord.ui.Button):
    def __init__(self, prev: str):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️")
        self.prev = prev

    async def callback(self, interaction: discord.Interaction):
        await show_step(interaction, self.view.order, self.prev)


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel order", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        e = make_embed("Order cancelled", "Nothing was sent. Use the close button above to close this ticket, or tell the team what you need.")
        await interaction.response.edit_message(embed=e, view=None)


class OrderView(discord.ui.View):
    def __init__(self, order: Order, step: str):
        super().__init__(timeout=None)
        self.order = order
        if step == "product":
            self.add_item(ProductSelect())
        elif step == "duration":
            self.add_item(DurationSelect(order.product))
        elif step == "username":
            self.add_item(UsernameButton())
        else:
            self.add_item(PaymentSelect())
        steps = order.steps
        if steps.index(step) > 0:
            self.add_item(BackButton(steps[steps.index(step) - 1]))
        self.add_item(CancelButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.order.user_id:
            await interaction.response.send_message(
                embed=make_embed("Not your order", "Only the person who opened this ticket can use these steps."), ephemeral=True
            )
            return False
        return True


# --------------------------------------------------------------------------
# TICKET CONTROLS (claim / close)
# --------------------------------------------------------------------------
async def build_transcript(channel: discord.TextChannel) -> discord.File:
    lines = []
    async for m in channel.history(limit=None, oldest_first=True):
        text = m.content or ""
        if m.embeds:
            titles = [e.title for e in m.embeds if e.title]
            if titles:
                text += f" [embed: {' | '.join(titles)}]"
        for a in m.attachments:
            text += f" [file: {a.url}]"
        stamp = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{stamp}] {m.author} ({m.author.id}): {text}")
    return discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}-transcript.txt")


async def close_ticket(channel: discord.TextChannel, closer: discord.abc.User):
    info = parse_topic(channel.topic) or {}
    await channel.send(embed=make_embed("Closing ticket", "Saving the log and deleting this channel in a few seconds."))
    log = channel.guild.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
    if log:
        try:
            e = make_embed(f"Ticket #{info.get('n', 0):04d} closed")
            e.add_field(name="Type", value=str(info.get("type", "unknown")).title(), inline=True)
            e.add_field(name="Opened by", value=f"<@{info.get('owner')}>", inline=True)
            e.add_field(name="Closed by", value=closer.mention, inline=True)
            e.timestamp = discord.utils.utcnow()
            await log.send(embed=e, file=await build_transcript(channel))
        except Exception:
            traceback.print_exc()
    await asyncio.sleep(3)
    await channel.delete(reason=f"Ticket closed by {closer}")


class ConfirmClose(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_embed("Closing", "One moment."), view=None)
        await close_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="Keep open", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=make_embed("Ticket kept open", "Nothing was closed."), view=None)


class TicketControls(discord.ui.View):
    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            self.claim.label = "Claimed"
            self.claim.disabled = True

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, emoji="🙋", custom_id="eye:ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await reply(interaction, make_embed("Staff only", "Only the team can claim tickets."))
            return
        embed = interaction.message.embeds[0]
        embed.add_field(name="Claimed by", value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=embed, view=TicketControls(claimed=True))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="eye:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = parse_topic(interaction.channel.topic)
        if not info or (interaction.user.id != info["owner"] and not is_staff(interaction.user)):
            await reply(interaction, make_embed("Not allowed", "You can't close this ticket."))
            return
        await interaction.response.send_message(
            embed=make_embed("Close this ticket?", "The channel will be deleted."), view=ConfirmClose(), ephemeral=True
        )


# --------------------------------------------------------------------------
# TICKET CREATION
# --------------------------------------------------------------------------
def precheck(interaction: discord.Interaction, kind: str) -> str | None:
    """Fast checks before we do any real work. Returns a message if the user should be stopped."""
    last = COOLDOWNS.get(interaction.user.id, 0.0)
    if time.monotonic() - last < COOLDOWN_SECONDS:
        return "Slow down a little and try again in a few seconds."
    category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
    if isinstance(category, discord.CategoryChannel):
        existing = find_existing(category, interaction.user.id, kind)
        if existing:
            return f"You already have an open ticket: {existing.mention}"
    return None


async def create_ticket(interaction: discord.Interaction, kind: str, *, topic: str | None = None, details: str | None = None):
    """The interaction must already be deferred (ephemeral)."""
    guild, user = interaction.guild, interaction.user
    try:
        category = guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(embed=make_embed("Ticket error", "The ticket category could not be found. Please contact the owner."), ephemeral=True)
            return

        allow = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True,
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: allow,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                embed_links=True, attach_files=True, manage_channels=True,
            ),
        }
        owner = await get_owner_member(guild)
        if owner:
            overwrites[owner] = allow
        for rid in STAFF_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = allow

        number = next_ticket_number()
        base = PURCHASE_NAME if kind == "purchase" else SUPPORT_NAME
        name = f"{base}-{slug(user.name)}" if APPEND_USERNAME else base
        channel = await guild.create_text_channel(
            name=name, category=category, overwrites=overwrites,
            topic=f"eye-ticket | owner:{user.id} | type:{kind} | n:{number}",
            reason=f"{kind} ticket #{number} for {user}",
        )
        COOLDOWNS[user.id] = time.monotonic()

        async def populate():
            if kind == "purchase":
                welcome = make_embed(
                    f"Ticket #{number:04d}",
                    f"Welcome {user.mention}\nFollow the steps below. Your order goes straight to the team when you finish.",
                )
                welcome.add_field(name="Type", value="Purchase", inline=True)
                await channel.send(content=user.mention, embed=welcome, view=TicketControls())
                order = Order(user.id, number)
                await channel.send(embed=step_embed(order, "product"), view=OrderView(order, "product"))
            else:
                welcome = make_embed(
                    f"Ticket #{number:04d}",
                    f"Welcome {user.mention}\nA team member will reply here as soon as possible.",
                )
                welcome.add_field(name="Type", value="Support", inline=True)
                welcome.add_field(name="Topic", value=(topic or "None")[:256], inline=False)
                welcome.add_field(name="Details", value=(details or "None")[:1024], inline=False)
                await channel.send(content=user.mention, embed=welcome, view=TicketControls())

        confirm = make_embed("Ticket created", f"Your ticket is ready: {channel.mention}")
        await asyncio.gather(populate(), interaction.followup.send(embed=confirm, ephemeral=True))
    except Exception:
        traceback.print_exc()
        await interaction.followup.send(embed=make_embed("Something went wrong", "The ticket could not be created. Please try again in a moment."), ephemeral=True)


# --------------------------------------------------------------------------
# PANEL
# --------------------------------------------------------------------------
class SupportModal(discord.ui.Modal, title="Support ticket"):
    topic = discord.ui.TextInput(label="Topic", placeholder="What do you need help with?", max_length=80)
    details = discord.ui.TextInput(
        label="Details", style=discord.TextStyle.paragraph,
        placeholder="Tell us what happened so we can help faster.", max_length=900,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await create_ticket(interaction, "support", topic=self.topic.value, details=self.details.value)


async def reset_panel(message: discord.Message | None):
    """Re-sending the view clears the selected option so it can be picked again."""
    if message is None:
        return
    try:
        await message.edit(view=PanelView())
    except discord.HTTPException:
        pass


class TicketSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Select a ticket type",
            custom_id="eye:panel:select",
            options=[
                discord.SelectOption(label="Purchase ticket", value="purchase", emoji="🛒", description="Order a product, serial or account unban"),
                discord.SelectOption(label="Support ticket", value="support", emoji="🛠️", description="Get help from our team"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        kind = self.values[0]
        problem = precheck(interaction, kind)
        if problem:
            await reply(interaction, make_embed("One moment", problem))
        elif kind == "support":
            await interaction.response.send_modal(SupportModal())
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await create_ticket(interaction, "purchase")
        asyncio.create_task(reset_panel(interaction.message))


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


def panel_embed() -> discord.Embed:
    return make_embed(
        "Tickets",
        "Pick a ticket type from the menu below.\n"
        f"{SEP}\n"
        "🛒 **Purchase**\nOrder a product, serial or account unban.\n\n"
        "🛠️ **Support**\nSomething wrong? Tell us and we will help.\n"
        f"{SEP}\n"
        "Your ticket is private. Only you and our team can see it.",
        image=PANEL_IMAGE,
    )


# --------------------------------------------------------------------------
# BOT
# --------------------------------------------------------------------------
class EyeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents(guilds=True)          # the leanest setup that still works
        intents.message_content = READ_MESSAGE_CONTENT
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            activity=discord.Activity(type=discord.ActivityType.watching, name="your tickets"),
            chunk_guilds_at_startup=False,
        )

    async def setup_hook(self):
        self.add_view(PanelView())        # panel dropdown keeps working after restarts
        self.add_view(TicketControls())   # claim / close buttons keep working after restarts
        await self.tree.sync(guild=discord.Object(id=GUILD_ID))

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")


bot = EyeBot()
GUILD = discord.Object(id=GUILD_ID)


@bot.tree.command(name="panel", description="Send the ticket panel", guild=GUILD)
@app_commands.default_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await reply(interaction, make_embed("Administrators only"))
        return
    channel = interaction.guild.get_channel(PANEL_CHANNEL_ID) or interaction.channel
    await channel.send(embed=panel_embed(), view=PanelView())
    await reply(interaction, make_embed("Panel sent", f"The ticket panel is live in {channel.mention}."))


@bot.tree.command(name="add", description="Add a user to this ticket", guild=GUILD)
@app_commands.describe(user="The user to add")
async def add_user(interaction: discord.Interaction, user: discord.Member):
    if not parse_topic(getattr(interaction.channel, "topic", None)) or not is_staff(interaction.user):
        await reply(interaction, make_embed("Not allowed", "Staff can use this inside a ticket."))
        return
    await interaction.channel.set_permissions(
        user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True
    )
    await interaction.response.send_message(embed=make_embed("User added", f"{user.mention} can now see this ticket."))


@bot.tree.command(name="remove", description="Remove a user from this ticket", guild=GUILD)
@app_commands.describe(user="The user to remove")
async def remove_user(interaction: discord.Interaction, user: discord.Member):
    info = parse_topic(getattr(interaction.channel, "topic", None))
    if not info or not is_staff(interaction.user):
        await reply(interaction, make_embed("Not allowed", "Staff can use this inside a ticket."))
        return
    if user.id == info["owner"]:
        await reply(interaction, make_embed("Can't remove", "That is the person who opened the ticket."))
        return
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(embed=make_embed("User removed", f"{user.mention} can no longer see this ticket."))


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    traceback.print_exception(type(error), error, error.__traceback__)
    await reply(interaction, make_embed("Something went wrong", "Please try again in a moment."))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your environment or in a .env file.")

    keep_alive()
    bot.run(TOKEN)