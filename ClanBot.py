import discord
from discord.ext import commands
from discord import app_commands
from discord.utils import get
import os
import sqlite3
from dotenv import load_dotenv
import asyncio

load_dotenv("token.env")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

CLAN_CATEGORY_NAME = "CLANS"
DB_FILE = "clans.db"

# --------------------- DATABASE SETUP ---------------------
db_lock = asyncio.Lock()
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
c = conn.cursor()

# Create tables if not exist
c.execute("""
CREATE TABLE IF NOT EXISTS clans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    leader_id INTEGER,
    text_channel_id INTEGER,
    voice_channel_id INTEGER
)
""")

c.execute("""
CREATE TABLE IF NOT EXISTS clan_members (
    clan_id INTEGER,
    member_id INTEGER,
    PRIMARY KEY(clan_id, member_id)
)
""")
conn.commit()

# --------------------- HELPER FUNCTIONS ---------------------
def get_clan_by_name(name):
    c.execute("SELECT * FROM clans WHERE name = ?", (name,))
    return c.fetchone()

def get_members(clan_id):
    c.execute("SELECT member_id FROM clan_members WHERE clan_id = ?", (clan_id,))
    return [row[0] for row in c.fetchall()]

async def add_clan_to_db(name, leader_id, text_id, voice_id):
    async with db_lock:
        c.execute("INSERT INTO clans (name, leader_id, text_channel_id, voice_channel_id) VALUES (?, ?, ?, ?)",
                  (name, leader_id, text_id, voice_id))
        clan_id = c.lastrowid
        c.execute("INSERT INTO clan_members (clan_id, member_id) VALUES (?, ?)", (clan_id, leader_id))
        conn.commit()
    return clan_id

async def remove_clan_from_db(clan_id):
    async with db_lock:
        c.execute("DELETE FROM clans WHERE id = ?", (clan_id,))
        c.execute("DELETE FROM clan_members WHERE clan_id = ?", (clan_id,))
        conn.commit()

async def update_clan_permissions(guild, clan_id):
    """Update permissions for all current clan members."""
    c.execute("SELECT name, leader_id, text_channel_id, voice_channel_id FROM clans WHERE id = ?", (clan_id,))
    clan = c.fetchone()
    if not clan:
        return

    clan_name, leader_id, text_channel_id, voice_channel_id = clan

    role = get(guild.roles, name=clan_name)
    text_channel = guild.get_channel(text_channel_id)
    voice_channel = guild.get_channel(voice_channel_id)
    leader = guild.get_member(leader_id)

    if not role or not text_channel or not voice_channel or not leader:
        return

    # Build overwrites
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True),
        leader: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True, manage_channels=True)
    }

    # Explicitly allow each member in DB
    members_ids = get_members(clan_id)
    for member_id in members_ids:
        member = guild.get_member(member_id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True)

    # Apply permissions safely
    try:
        await text_channel.edit(overwrites=overwrites)
        await voice_channel.edit(overwrites=overwrites)
    except discord.Forbidden:
        print(f"[WARN] Bot missing access to edit channels for clan {clan_name}")
    except Exception as e:
        print(f"[ERROR] Failed updating permissions for clan {clan_name}: {e}")

async def get_or_create_category_for_clan(guild, clan_name: str):
    """Each clan gets its own category: CLAN - <clan_name>"""
    category_name = f"CLAN - {clan_name}"
    category = get(guild.categories, name=category_name)
    if category:
        return category
    try:
        category = await guild.create_category(category_name)
        return category
    except discord.Forbidden:
        print(f"[ERROR] Bot missing permission to create category {category_name}")
        return None

def get_user_clan(user_id):
    """Return clan id if user is already in any clan, else None"""
    c.execute("SELECT clan_id FROM clan_members WHERE member_id = ?", (user_id,))
    result = c.fetchone()
    return result[0] if result else None

# --------------------- EVENTS ---------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    GUILD_ID = 1457233647001403442
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=guild)
    print("Slash commands synced!")

    # Debug: list all registered commands
    commands_list = await bot.tree.fetch_commands(guild=guild)
    print("Registered commands:")
    for cmd in commands_list:
        print(f"- {cmd.name}")

@bot.event
async def on_member_update(before, after):
    """Update clan permissions when roles change."""
    guild = after.guild
    added_roles = [r for r in after.roles if r not in before.roles]
    removed_roles = [r for r in before.roles if r not in after.roles]

    for role in added_roles + removed_roles:
        c.execute("SELECT id FROM clans WHERE name = ?", (role.name,))
        result = c.fetchone()
        if result:
            clan_id = result[0]
            bot.loop.create_task(update_clan_permissions(guild, clan_id))

@bot.event
async def on_member_remove(member):
    """Update clan permissions and remove member from DB when leaving server."""
    guild = member.guild
    c.execute("SELECT clan_id FROM clan_members WHERE member_id = ?", (member.id,))
    for row in c.fetchall():
        clan_id = row[0]
        async with db_lock:
            c.execute("DELETE FROM clan_members WHERE clan_id = ? AND member_id = ?", (clan_id, member.id))
            conn.commit()
        bot.loop.create_task(update_clan_permissions(guild, clan_id))

# --------------------- GUILD ID FOR TESTING ---------------------
GUILD_ID = 1457233647001403442
GUILD = discord.Object(id=GUILD_ID)

# --------------------- COMMANDS ---------------------

# CREATE CLAN
@bot.tree.command(name="create_clan", description="Create your own clan!", guild=GUILD)
@app_commands.describe(name="The name of your clan")
async def create_clan(interaction: discord.Interaction, name: str):
    guild = interaction.guild

    # Check if user already in any clan
    existing_clan_id = get_user_clan(interaction.user.id)
    if existing_clan_id:
        await interaction.response.send_message("You are already in a clan! Leave it before creating a new one.", ephemeral=True)
        return

    # Check DB for clan name
    if get_clan_by_name(name):
        await interaction.response.send_message(f"A clan called **{name}** already exists!", ephemeral=True)
        return

    # Check for existing role
    role = get(guild.roles, name=name)
    if role:
        await interaction.response.send_message(f"A role called **{name}** already exists!", ephemeral=True)
        return

    # Create role
    try:
        role = await guild.create_role(name=name)
    except discord.Forbidden:
        await interaction.response.send_message("Bot cannot create role. Check permissions.", ephemeral=True)
        return

    # Create unique category for this clan
    category = await get_or_create_category_for_clan(guild, name)
    if not category:
        await interaction.response.send_message("Bot cannot access or create the clan category.", ephemeral=True)
        await role.delete()
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, speak=True, manage_channels=True)
    }

    # Create channels
    try:
        text_channel = await guild.create_text_channel(name=name.lower() + "-text", category=category, overwrites=overwrites)
        voice_channel = await guild.create_voice_channel(name=name.lower() + "-voice", category=category, overwrites=overwrites)
    except discord.Forbidden:
        await interaction.response.send_message("Bot cannot create channels. Check permissions.", ephemeral=True)
        await role.delete()
        if text_channel:
            await text_channel.delete()
        if voice_channel:
            await voice_channel.delete()
        return

    # Add role to creator
    await interaction.user.add_roles(role)

    # Add to DB
    try:
        clan_id = await add_clan_to_db(name, interaction.user.id, text_channel.id, voice_channel.id)
    except sqlite3.IntegrityError:
        await interaction.response.send_message("Clan already exists in database!", ephemeral=True)
        await role.delete()
        await text_channel.delete()
        await voice_channel.delete()
        return

    await interaction.response.send_message(f"Clan **{name}** created! You are now the leader.", ephemeral=True)


# INVITE MEMBER
@bot.tree.command(name="invite_clan", description="Invite a user to your clan", guild=GUILD)
@app_commands.describe(member="User to invite", clan_name="Your clan name")
async def invite_clan(interaction: discord.Interaction, member: discord.Member, clan_name: str):
    guild = interaction.guild
    clan = get_clan_by_name(clan_name)
    if not clan:
        await interaction.response.send_message("Clan not found!", ephemeral=True)
        return

    clan_id, name, leader_id, text_channel_id, voice_channel_id = clan
    if interaction.user.id != leader_id:
        await interaction.response.send_message("Only the clan leader can invite members!", ephemeral=True)
        return

    role = get(guild.roles, name=clan_name)
    await member.add_roles(role)

    async with db_lock:
        c.execute("INSERT OR IGNORE INTO clan_members (clan_id, member_id) VALUES (?, ?)", (clan_id, member.id))
        conn.commit()

    await update_clan_permissions(guild, clan_id)
    await interaction.response.send_message(f"{member.mention} has been added to **{clan_name}**!", ephemeral=True)

# KICK MEMBER
@bot.tree.command(name="kick_clan", description="Kick a member from your clan", guild=GUILD)
@app_commands.describe(member="User to remove", clan_name="Your clan name")
async def kick_clan(interaction: discord.Interaction, member: discord.Member, clan_name: str):
    guild = interaction.guild
    clan = get_clan_by_name(clan_name)
    if not clan:
        await interaction.response.send_message("Clan not found!", ephemeral=True)
        return

    clan_id, name, leader_id, text_channel_id, voice_channel_id = clan
    if interaction.user.id != leader_id:
        await interaction.response.send_message("Only the clan leader can kick members!", ephemeral=True)
        return

    role = get(guild.roles, name=clan_name)
    await member.remove_roles(role)

    async with db_lock:
        c.execute("DELETE FROM clan_members WHERE clan_id = ? AND member_id = ?", (clan_id, member.id))
        conn.commit()

    await update_clan_permissions(guild, clan_id)
    await interaction.response.send_message(f"{member.mention} has been removed from **{clan_name}**.", ephemeral=True)

# DISBAND CLAN
@bot.tree.command(name="disband_clan", description="Disband your clan", guild=GUILD)
@app_commands.describe(clan_name="The clan you want to disband")
async def disband_clan(interaction: discord.Interaction, clan_name: str):
    guild = interaction.guild
    clan = get_clan_by_name(clan_name)
    if not clan:
        await interaction.response.send_message("Clan not found!", ephemeral=True)
        return

    clan_id, name, leader_id, text_channel_id, voice_channel_id = clan

    if interaction.user.id != leader_id:
        await interaction.response.send_message("Only the clan leader can disband the clan!", ephemeral=True)
        return

    # Remove role from all members and delete role
    role = get(guild.roles, name=clan_name)
    if role:
        for member in role.members:
            await member.remove_roles(role)
        try:
            await role.delete()
        except discord.Forbidden:
            print(f"[WARN] Missing permission to delete role {role.name}")

    # Delete channels
    for ch_id in [text_channel_id, voice_channel_id]:
        channel = guild.get_channel(ch_id)
        if channel:
            try:
                await channel.delete()
            except discord.Forbidden:
                print(f"[WARN] Missing permission to delete channel {channel.name}")

    # Delete category if empty
    category_name = f"CLAN - {clan_name}"
    category = get(guild.categories, name=category_name)
    if category and len(category.channels) == 0:
        try:
            await category.delete()
        except discord.Forbidden:
            print(f"[WARN] Missing permission to delete category {category_name}")

    # Remove from DB
    await remove_clan_from_db(clan_id)

    await interaction.response.send_message(f"Clan **{clan_name}** has been disbanded.", ephemeral=True)


# CLAN INFO
@bot.tree.command(name="clan_info", description="Show information about a clan", guild=GUILD)
@app_commands.describe(clan_name="The clan you want info about")
async def clan_info(interaction: discord.Interaction, clan_name: str):
    guild = interaction.guild
    clan = get_clan_by_name(clan_name)
    if not clan:
        await interaction.response.send_message("Clan not found!", ephemeral=True)
        return

    clan_id, name, leader_id, text_channel_id, voice_channel_id = clan
    leader = guild.get_member(leader_id)
    members_ids = get_members(clan_id)
    members = [guild.get_member(mid) for mid in members_ids]

    text_channel = guild.get_channel(text_channel_id)
    voice_channel = guild.get_channel(voice_channel_id)

    member_mentions = ', '.join([m.mention for m in members if m])

    embed = discord.Embed(title=f"Clan Info: {name}", color=discord.Color.green())
    embed.add_field(name="Leader", value=leader.mention if leader else "Unknown", inline=False)
    embed.add_field(name="Members", value=member_mentions or "None", inline=False)
    embed.add_field(name="Text Channel", value=text_channel.mention if text_channel else "Deleted", inline=True)
    embed.add_field(name="Voice Channel", value=voice_channel.mention if voice_channel else "Deleted", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# --------------------- RUN BOT ---------------------
bot.run(os.getenv("DISCORD_TOKEN"))
