
                await asyncio.sleep(delay)

            # Try to connect with timeout
            voice_client = await asyncio.wait_for(
                channel.connect(reconnect=False, timeout=30.0),
                timeout=35.0
            )

            logger.info(f"Successfully connected to {channel}")
            connection_retries[guild_id] = 0  # Reset retry counter

            # Brief delay to ensure connection is stable
            await asyncio.sleep(CONNECT_DELAY)
            return voice_client

        except asyncio.TimeoutError:
            logger.warning(f"Connection timeout for {channel} (attempt {attempt + 1})")
        except discord.errors.ConnectionClosed as e:
            logger.warning(f"Connection closed for {channel}: {e} (attempt {attempt + 1})")
        except Exception as e:
            logger.error(f"Unexpected error connecting to {channel}: {e} (attempt {attempt + 1})")

        # Store retry count
        connection_retries[guild_id] = attempt + 1

    logger.error(f"Failed to connect to {channel} after {max_retries} attempts")
    return None


async def should_disconnect(guild_id, user_who_left_id=None):
    """Check if bot should disconnect based on queue state and user departures"""
    queue = get_queue(guild_id)
    current_track = currently_playing.get(guild_id)
    host_id = current_player.get(guild_id)

    # Always disconnect if queue is empty and nothing playing
    if not current_track and not queue:
        return True

    # If the HOST left the VC AND only host songs remain, disconnect
    if user_who_left_id and host_id and user_who_left_id == host_id:
        host_songs_only = True

        # Check current track
        if current_track and current_track.user_id != host_id:
            host_songs_only = False

        # Check queue
        if host_songs_only:
            for track in queue:
                if track.user_id != host_id:
                    host_songs_only = False
                    break

        if host_songs_only:
            return True

    # If someone left, check if we should disconnect
    if user_who_left_id:
        # If only one item total (current track) and that person left
        if current_track and not queue and current_track.user_id == user_who_left_id:
            return True

        # If only one item in queue and that person left (and nothing currently playing)
        if not current_track and len(queue) == 1 and queue[0].user_id == user_who_left_id:
            return True

    return False


async def play_next_track(guild, voice_client):
    queue = get_queue(guild.id)

    # Enhanced voice client validation
    if not voice_client:
        logger.warning("Voice client is None")
        await cleanup_voice_client(guild.id)
        return

    if not voice_client.is_connected():
        logger.warning("Voice client is not connected")
        await cleanup_voice_client(guild.id)
        return

    # Check if we should disconnect
    if await should_disconnect(guild.id):
        logger.info("Should disconnect - queue empty or host left")
        await asyncio.sleep(DISCONNECT_DELAY)

        # Check again in case something was added during the delay
        if await should_disconnect(guild.id):
            await cleanup_voice_client(guild.id)
            logger.info("Disconnected - queue empty or host left")
        return

    if not queue:
        logger.info("Queue is empty, nothing to play")
        return

    track = queue.popleft()
    currently_playing[guild.id] = track

    # Fixed yt-dlp options - stream directly instead of downloading
    ydl_opts = {
        "format": "bestaudio[ext=webm]/bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractaudio": False,  # Don't extract audio, stream directly
        "audioformat": "best",
        "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    }

    try:
        logger.info(f"Getting stream URL for track: {track.title}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track.url, download=False)
            # Get the direct stream URL
            if 'url' in info:
                stream_url = info['url']
            elif 'formats' in info and info['formats']:
                # Find best audio format
                audio_formats = [f for f in info['formats'] if f.get('acodec') != 'none']
                if audio_formats:
                    stream_url = audio_formats[0]['url']
                else:
                    stream_url = info['formats'][0]['url']
            else:
                raise Exception("Could not extract stream URL")

            track.filename = stream_url  # Store the stream URL
            logger.info(f"Got stream URL: {stream_url[:100]}...")

    except Exception as e:
        logger.error(f"Failed to get stream URL: {e}")
        # Try to play next track
        await play_next_track(guild, voice_client)
        return

    def after_playing(error):
        if error:
            logger.error(f"Player error: {error}")

        # No file cleanup needed since we're streaming

        # Schedule next track
        fut = asyncio.run_coroutine_threadsafe(play_next_track(guild, voice_client), bot.loop)
        try:
            fut.result()
        except Exception as e:
            logger.error(f"Error in next track task: {e}")

    try:
        # Fixed FFmpeg options - removed invalid options
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn'  # Removed invalid audio bitrate option for streaming
        }

        logger.info(f"Starting playback: {track.title}")
        # Use the stream URL directly
        voice_client.play(
            discord.FFmpegPCMAudio(source=stream_url, **ffmpeg_options),
            after=after_playing
        )
        logger.info("Playback started successfully")

    except Exception as e:
        logger.error(f"Error starting playback: {e}")
        # Try next track
        await play_next_track(guild, voice_client)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}!")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")


@bot.event
async def on_disconnect():
    logger.warning("Bot disconnected from Discord")


@bot.event
async def on_resumed():
    logger.info("Bot connection resumed")


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    guild = member.guild
    host_id = current_player.get(guild.id)
    voice_client = discord.utils.get(bot.voice_clients, guild=guild)

    # Enhanced voice client disconnect handling
    if voice_client and not voice_client.is_connected():
        logger.warning(f"Voice client disconnected unexpectedly in guild {guild.id}")
        if voice_client.is_playing():
            voice_client.stop()
        await cleanup_voice_client(guild.id)
        return

    # If the user who started playback left or switched channels
    if host_id and member.id == host_id:
        if before.channel is not None and after.channel != before.channel:
            await asyncio.sleep(1)  # slight delay for state to settle
            if voice_client and voice_client.is_connected():
                # Check if we should disconnect based on new logic
                if await should_disconnect(guild.id, member.id):
                    await cleanup_voice_client(guild.id)
                    logger.info(f"Host {member} left VC, bot disconnected")

HUGGINGFACE_API_KEY = os.environ.get("HUGGINGFACE_API_KEY")

# Assuming you have a bot instance
# bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

@bot.tree.command(name="generate-image", description="creates a ass ah art pls dont warn me")
@app_commands.describe(prompt="idk u do it")
async def generate_image(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()

    # Hugging Face Stable Diffusion API
    api_url = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"
    headers = {"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"}

    # Simplified payload - HF Inference API has limited parameter support
    payload = {
        "inputs": f"{prompt}, high quality, detailed, sharp, professional, masterpiece, best quality, ultra detailed, 8k resolution",
        "parameters": {
            "num_inference_steps": 30,  # Reduced for faster generation
            "guidance_scale": 7.5,  # Standard value
        }
    }

    retry_attempts = 3
    delay = 10

    for attempt in range(retry_attempts):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:  # Increased timeout
                response = await client.post(api_url, headers=headers, json=payload)

                print(f"Status: {response.status_code}")

                # Check for specific status codes
                if response.status_code == 503:
                    # Model is loading, wait and retry
                    print(f"Model loading, retrying in {delay} seconds...")
                    await interaction.followup.send(
                        f"afjadsfknasdfuasdnf trying again in {delay} seconds 💀💀💀 (attempt {attempt + 1}/3)",
                    )
                    await asyncio.sleep(delay)
                    delay += 15
                    continue

                elif response.status_code == 429:
                    # Rate limited
                    await interaction.followup.send(
                        f"llll imagine spamming the bot 💀💀💀 {interaction.user.mention} ez",
                    )
                    return

                elif response.status_code == 401:
                    # Unauthorized
                    await interaction.followup.send(
                        "idk you'll never see this",
                    )
                    return

                elif response.status_code == 400:
                    # Bad request
                    print(f"Bad request: {response.text}")
                    await interaction.followup.send(
                        f"lll prompt failed somehow {interaction.user.mention} is gay btw 💀💀💀💀",
                    )
                    return

                # Check if request was successful
                if response.status_code == 200:
                    # The response should be image bytes
                    image_bytes = response.content

                    # Validate we got image data
                    if len(image_bytes) > 0 and image_bytes.startswith(b'\x89PNG') or image_bytes.startswith(
                            b'\xff\xd8\xff'):
                        # Create Discord file from image bytes
                        image_file = discord.File(
                            io.BytesIO(image_bytes),
                            filename=f"generated_image_{interaction.user.id}.png"
                        )

                        # Send the image
                        await interaction.followup.send(
                            f"heres the ass ah art that {interaction.user.mention} created also heres the prompt: {prompt}",
                            file=image_file
                        )
                        return
                    else:
                        print(f"Invalid image data received. Length: {len(image_bytes)}")
                        print(f"First few bytes: {image_bytes[:20]}")
                        # Try to parse as JSON error message
                        try:
                            error_data = response.json()
                            print(f"API Error: {error_data}")
                            await interaction.followup.send(
                                f"lll api returned a err {interaction.user.mention} is gay btw 💀💀💀: {error_data.get('error', 'Unknown error')}",
                            )
                            return
                        except:
                            pass
                else:
                    # Other HTTP errors
                    print(f"HTTP Error {response.status_code}: {response.text}")
                    if attempt < retry_attempts - 1:
                        await asyncio.sleep(delay)
                        delay += 10
                    else:
                        await interaction.followup.send(
                            f"idk what to even say at this point why tf ai makes things cringe {response.status_code}). Please try again later.",
                        )
                        return

        except httpx.TimeoutException:
            print(f"Request timeout (attempt {attempt + 1})")
            if attempt < retry_attempts - 1:
                await interaction.followup.send(
                    f"lll it somehow timed out 💀💀. (attempt {attempt + 1}/3)",
                )
                await asyncio.sleep(delay)
                delay += 10
            else:
                await interaction.followup.send(
                    f"haha {interaction.user.mention} is gay so it failed 💀💀💀",
                )
                return

        except httpx.ConnectError as e:
            print(f"Connection failed (attempt {attempt + 1}): {e}")
            if attempt < retry_attempts - 1:
                await asyncio.sleep(delay)
                delay += 10
            else:
                await interaction.followup.send(
                    "💀💀💀💀",
                )
                return

        except Exception as e:
            print(f"Unexpected error: {e}")
            await interaction.followup.send(
                f"💀💀💀💀 {type(e).__name__}",
                ephemeral=True
            )
            return

    # If all attempts failed
    await interaction.followup.send(
        "💀💀💀💀💀💀💀.",
        ephemeral=True
    )

@bot.tree.command(name="sbtd-ban", description="bans someone from sbtd but u gotta be a mod to do this so skill issue (real)")
@app_commands.describe(
    username="the roblox user to ban",
    duration="ban duration (1m, 1h, 1d, 1w, 1y)",
    reason="self explaintory"
)
async def ban_command(interaction: discord.Interaction, username: str, duration: str, reason: str):
    if interaction.user.id not in ALLOWED_USER_IDS:
        await interaction.response.send_message("bros not a mod 💀💀", ephemeral=True)
        return

    # Defer the response immediately to prevent timeout
    await interaction.response.defer()

    duration = duration.lower()
    if not any(duration.endswith(suffix) for suffix in ["m", "h", "d", "w", "y"]):
        duration = 0

    payload = {
        "username": username,
        "duration": duration,
        "reason": reason,
        "mod": interaction.user.id,
        "token": os.environ.get("BAN_TOKEN")
    }

    try:
        # Use async http client with timeout
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(WEBHOOK_URL, json=payload)
            
            if response.status_code == 200:
                await interaction.followup.send(f"the dumbah {username} has been banned from sbtd 💀")
            else:
                await interaction.followup.send(f"skill issue: {response.status_code}", ephemeral=True)
                
    except httpx.TimeoutException:
        await interaction.followup.send(f"request timed out 💀💀💀", ephemeral=True)
    except httpx.ConnectError:
        await interaction.followup.send(f"connection failed 💀💀💀", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"request err: 💀💀💀 {type(e).__name__}", ephemeral=True)


# Fixed /remove command - properly handles queue management
@bot.tree.command(name="remove", description="removes ur own songs from the queue")
async def remove_command(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    
    if not queue:
        await interaction.response.send_message("theres nothing in the queue to remove 💀", ephemeral=True)
        return

    # Find user's tracks in the queue
    user_tracks = []
    for i, track in enumerate(queue):
        if track.user_id == interaction.user.id:
            user_tracks.append((i, track))

    if not user_tracks:
        await interaction.response.send_message("u dont have any songs in the queue 💀", ephemeral=True)
        return

    # If user has only one song, remove it directly
    if len(user_tracks) == 1:
        queue_pos, track = user_tracks[0]
        queue_list = list(queue)
        removed_track = queue_list.pop(queue_pos)
        queue.clear()
        queue.extend(queue_list)
        
        await interaction.response.send_message(f"removed from queue: **{removed_track.title}**", ephemeral=True)
        return

    # If user has multiple songs, show selection interface
    embed = discord.Embed(
        title="select which song to remove:",
        description="choose which of ur songs to remove from the queue:",
        color=0xff0000
    )

    for i, (queue_pos, track) in enumerate(user_tracks[:5]):  # Show max 5 songs
        embed.add_field(
            name=f"{i + 1}. {track.title}",
            value=f"position in queue: {queue_pos + 1}",
            inline=False
        )

    if len(user_tracks) > 5:
        embed.add_field(
            name="note:",
            value=f"showing first 5 of {len(user_tracks)} songs",
            inline=False
        )

    view = TrackRemovalView(interaction.user.id, queue, user_tracks)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# Fixed /skip command - enhanced permission checking and error handling
@bot.tree.command(name="skip", description="skips to the next track (requires manage messages permission or being the host)")
async def skip_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("bot is not connected to a voice channel 💀", ephemeral=True)
        return

    if not voice_client.is_playing():
        await interaction.response.send_message("nothing is playing 💀💀💀", ephemeral=True)
        return

    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Check if user can skip:
    # 1. Has manage messages permission (moderator)
    # 2. Is the host (started the music session)
    # 3. Current song is their own (can skip their own songs)
    can_skip = False
    skip_reason = ""

    if perms.manage_messages:
        can_skip = True
        skip_reason = "moderator"
    elif host_id == interaction.user.id:
        can_skip = True
        skip_reason = "host"
    elif current_track and current_track.user_id == interaction.user.id:
        can_skip = True
        skip_reason = "own song"

    if not can_skip:
        await interaction.response.send_message(
            "u cant skip this track 💀 (need manage messages permission, be the host, or skip ur own song)",
            ephemeral=True
        )
        return

    track_name = current_track.title if current_track else "current track"
    
    # Stop current track (this will trigger the after_playing callback)
    voice_client.stop()
    
    # Check if there's anything in queue
    queue = get_queue(interaction.guild.id)
    if queue:
        await interaction.response.send_message(f"⏭️ skipped: **{track_name}**")
    else:
        await interaction.response.send_message(f"⏭️ skipped: **{track_name}** (queue is now empty)")


# Fixed /leave command - enhanced permission checking and cleanup
@bot.tree.command(name="leave", description="makes the bot leave the voice channel")
async def leave_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("bot is not in a voice channel 💀", ephemeral=True)
        return

    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Check if there's music playing or in queue
    has_music = current_track is not None or len(queue) > 0
    
    # Permission check - allow if:
    # 1. User has manage messages permission (moderator)
    # 2. User is the host and no one else has songs
    # 3. No music is playing/queued
    can_leave = False
    
    if perms.manage_messages:
        can_leave = True
    elif not has_music:
        can_leave = True
    elif host_id == interaction.user.id:
        # Host can leave if only their songs remain
        only_host_songs = True
        
        # Check current track
        if current_track and current_track.user_id != host_id:
            only_host_songs = False
        
        # Check queue
        if only_host_songs:
            for track in queue:
                if track.user_id != host_id:
                    only_host_songs = False
                    break
        
        if only_host_songs:
            can_leave = True

    if not can_leave:
        await interaction.response.send_message(
            "cant make the bot leave while music is playing 💀 (need manage messages permission or be the host with only ur songs)",
            ephemeral=True
        )
        return

    # Enhanced cleanup
    try:
        await cleanup_voice_client(interaction.guild.id)
        await interaction.response.send_message("👋 left the voice channel")
    except Exception as e:
        logger.error(f"Error during leave command: {e}")
        await interaction.response.send_message("left the voice channel (with some errors 💀)", ephemeral=True)


# Fixed /stop command - enhanced permission checking and queue management
@bot.tree.command(name="stop", description="stops the current audio and clears the queue")
async def stop_command(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("bot is not in a voice channel 💀", ephemeral=True)
        return

    if not voice_client.is_playing():
        await interaction.response.send_message("nothing is playing 💀", ephemeral=True)
        return

    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)
    host_id = current_player.get(interaction.guild.id)
    perms = interaction.user.guild_permissions

    # Permission check - allow if:
    # 1. User has manage messages permission (moderator)
    # 2. User is the host
    # 3. Only user's own songs are playing/queued
    can_stop = False
    
    if perms.manage_messages:
        can_stop = True
    elif host_id == interaction.user.id:
        can_stop = True
    else:
        # Check if only user's songs are in queue/playing
        only_user_songs = True
        
        # Check current track
        if current_track and current_track.user_id != interaction.user.id:
            only_user_songs = False
        
        # Check queue
        if only_user_songs:
            for track in queue:
                if track.user_id != interaction.user.id:
                    only_user_songs = False
                    break
        
        if only_user_songs:
            can_stop = True

    if not can_stop:
        await interaction.response.send_message(
            "cant stop the music 💀 (need manage messages permission, be the host, or only have ur own songs)",
            ephemeral=True
        )
        return

    # Count items being cleared
    queue_count = len(queue)
    current_song = current_track.title if current_track else "unknown"
    
    # Clear the queue and stop playback
    queue.clear()
    currently_playing.pop(interaction.guild.id, None)
    
    # Stop the current track
    voice_client.stop()
    
    if queue_count > 0:
        await interaction.response.send_message(
            f"⏹️ stopped: **{current_song}** and cleared {queue_count} songs from queue"
        )
    else:
        await interaction.response.send_message(f"⏹️ stopped: **{current_song}**")


# Enhanced play_next_track function with better error handling
async def play_next_track(guild, voice_client):
    queue = get_queue(guild.id)

    # Enhanced voice client validation
    if not voice_client or not voice_client.is_connected():
        logger.warning("Voice client is None or not connected")
        await cleanup_voice_client(guild.id)
        return

    # Check if we should disconnect
    if await should_disconnect(guild.id):
        logger.info("Should disconnect - queue empty or host left")
        await asyncio.sleep(DISCONNECT_DELAY)
        if await should_disconnect(guild.id):
            await cleanup_voice_client(guild.id)
            logger.info("Disconnected - queue empty or host left")
        return

    if not queue:
        logger.info("Queue is empty, nothing to play")
        currently_playing.pop(guild.id, None)  # Clear currently playing
        return

    track = queue.popleft()
    currently_playing[guild.id] = track

    # Enhanced yt-dlp options for streaming
    ydl_opts = {
        "format": "bestaudio[ext=webm]/bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractaudio": False,
        "audioformat": "best",
        "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
        # Enhanced options to handle sign-in detection
        "cookiefile": None,
        "extractor_args": {
            "youtube": {
                "skip": ["dash", "hls"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
    }

    try:
        logger.info(f"Getting stream URL for track: {track.title}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(track.url, download=False)
            
            # Get the direct stream URL
            stream_url = None
            if 'url' in info:
                stream_url = info['url']
            elif 'formats' in info and info['formats']:
                # Find best audio format
                audio_formats = [f for f in info['formats'] if f.get('acodec') != 'none']
                if audio_formats:
                    stream_url = audio_formats[0]['url']
                else:
                    stream_url = info['formats'][0]['url']
            
            if not stream_url:
                raise Exception("Could not extract stream URL")

            logger.info(f"Got stream URL for: {track.title}")

    except yt_dlp.utils.ExtractorError as e:
        error_msg = str(e).lower()
        if "sign in" in error_msg or "confirm" in error_msg or "bot" in error_msg:
            logger.error(f"YouTube sign-in required for track: {track.title}")
        else:
            logger.error(f"Extractor error for track {track.title}: {e}")
        # Try to play next track
        await play_next_track(guild, voice_client)
        return
    except Exception as e:
        logger.error(f"Failed to get stream URL for track {track.title}: {e}")
        # Try to play next track
        await play_next_track(guild, voice_client)
        return

    def after_playing(error):
        if error:
            logger.error(f"Player error: {error}")

        # Schedule next track
        fut = asyncio.run_coroutine_threadsafe(play_next_track(guild, voice_client), bot.loop)
        try:
            fut.result()
        except Exception as e:
            logger.error(f"Error in next track task: {e}")

    try:
        # Enhanced FFmpeg options for better streaming
        ffmpeg_options = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -http_persistent false',
            'options': '-vn -bufsize 1024k'
        }

        logger.info(f"Starting playback: {track.title}")
        voice_client.play(
            discord.FFmpegPCMAudio(source=stream_url, **ffmpeg_options),
            after=after_playing
        )
        logger.info("Playback started successfully")

    except Exception as e:
        logger.error(f"Error starting playback for {track.title}: {e}")
        # Try next track
        await play_next_track(guild, voice_client)


class QueueView(discord.ui.View):
    def __init__(self, user_id, queue):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.queue = list(queue)
        self.current_page = 0
        self.max_per_page = 5
        self.total_pages = (len(self.queue) - 1) // self.max_per_page + 1 if self.queue else 1
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.total_pages > 1:
            if self.current_page > 0:
                self.add_item(self.prev_button)
            if self.current_page < self.total_pages - 1:
                self.add_item(self.next_button)

    def _get_page_items(self):
        start = self.current_page * self.max_per_page
        end = start + self.max_per_page
        return self.queue[start:end]

    async def update_message(self, interaction):
        page_tracks = self._get_page_items()
        embed = discord.Embed(title="🗑️ garbage can queue 💀", color=0x00ff00)
        total_tracks = len(self.queue)
        embed.description = f"page {self.current_page + 1}/{self.total_pages} — total tracks: {total_tracks}\n\n"

        for i, track in enumerate(page_tracks, start=self.current_page * self.max_per_page + 1):
            embed.description += f"**{i}. {track.title}** — requested by <@{track.user_id}> — length: {format_duration(track.duration)}\n"

        self.update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="previous", style=discord.ButtonStyle.primary, row=0)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("bro clicked on another bro's menu 💀", ephemeral=True)
            return
        if self.current_page > 0:
            self.current_page -= 1
            await self.update_message(interaction)

    @discord.ui.button(label="next", style=discord.ButtonStyle.primary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("bro clicked on another bro's menu 💀", ephemeral=True)
            return
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await self.update_message(interaction)


@bot.tree.command(name="queue", description="shows ur garbage ah sounds and videos in a queue 💀")
async def queue_command(interaction: discord.Interaction):
    queue = get_queue(interaction.guild.id)
    current_track = currently_playing.get(interaction.guild.id)

    if not current_track and not queue:
        await interaction.response.send_message("theres nothing in the queue thank fucking god 💀💀💀", ephemeral=True)
        return

    # Create initial embed
    embed = discord.Embed(title="🗑️ garbage can queue 💀", color=0x00ff00)

    # Show currently playing track
    if current_track:
        embed.add_field(
            name="now playing: 🗑️💀",
            value=f"**{current_track.title}** — requested by <@{current_track.user_id}> — length: {format_duration(current_track.duration)}",
            inline=False,
        )

    # Show queue items if any exist
    if queue:
        view = QueueView(interaction.user.id, queue)
        page_tracks = view._get_page_items()
        total_tracks = len(queue)

        queue_text = f"page {view.current_page + 1}/{view.total_pages} — total tracks: {total_tracks}\n\n"

        for i, track in enumerate(page_tracks, start=view.current_page * view.max_per_page + 1):
            queue_text += f"**{i}. {track.title}** — requested by <@{track.user_id}> — length: {format_duration(track.duration)}\n"

        embed.add_field(
            name="up next:",
            value=queue_text,
            inline=False
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    else:
        # No queue items, just show currently playing
        await interaction.response.send_message(embed=embed, ephemeral=True)

if token:
    try:
        bot.run(token)
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
else:
    logger.error("ERROR: DISCORD_TOKEN not found in environment variables.")

serverig.keep_alive()
