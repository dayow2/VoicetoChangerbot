import os
import io
import logging
import asyncio
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pydub import AudioSegment
from pydub.effects import normalize
import numpy as np

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("No TELEGRAM_TOKEN environment variable set")

# Flask app for health checks
flask_app = Flask(__name__)

# Global bot application instance
bot_app = None

async def change_pitch(audio_segment, semitones):
    """Change pitch by semitones using pydub's built-in method"""
    # pydub doesn't have direct pitch shift, so we use speed + sample rate trick
    # This is a simplified version that works without pedalboard
    new_sample_rate = int(audio_segment.frame_rate * (2 ** (semitones / 12.0)))
    return audio_segment._spawn(audio_segment.raw_data, overrides={'frame_rate': new_sample_rate}).set_frame_rate(audio_segment.frame_rate)

async def speed_change(audio_segment, speed=1.0):
    """Change speed without changing pitch (simple version)"""
    if speed == 1.0:
        return audio_segment
    
    # Simple speed change (affects pitch too)
    new_sample_rate = int(audio_segment.frame_rate * speed)
    return audio_segment._spawn(audio_segment.raw_data, overrides={'frame_rate': new_sample_rate}).set_frame_rate(audio_segment.frame_rate)

async def convert_voice(voice_file_path, effect_name):
    """Apply selected effect to audio file"""
    try:
        # Load audio
        audio = AudioSegment.from_file(voice_file_path)
        
        # Apply effects based on selection
        if effect_name == "chipmunk":
            # Higher pitch
            result = await change_pitch(audio, 7)
        elif effect_name == "deep_voice":
            # Lower pitch
            result = await change_pitch(audio, -5)
        elif effect_name == "fast":
            # 1.5x faster
            result = audio.speedup(playback_speed=1.5)
        elif effect_name == "slow":
            # 0.75x slower - use change_pitch with negative semitones and adjust
            result = await speed_change(audio, 0.75)
        elif effect_name == "reverse":
            # Play backwards
            result = audio.reverse()
        elif effect_name == "echo":
            # Simple echo by overlaying delayed version
            echo = audio - 10  # Reduce volume
            result = audio.overlay(echo, position=200)  # 200ms delay
        else:
            result = audio
        
        # Normalize volume
        result = normalize(result)
        return result
        
    except Exception as e:
        logger.error(f"Error converting voice: {e}")
        return None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🎤 *Welcome to VoiceToChangerBot!*\n\n"
        "Send me any voice message, then choose an effect:\n\n"
        "🔹 `/chipmunk` - High pitched voice\n"
        "🔹 `/deep` - Low deep voice\n"
        "🔹 `/fast` - Speed up\n"
        "🔹 `/slow` - Slow down\n"
        "🔹 `/reverse` - Play backwards\n"
        "🔹 `/echo` - Add echo effect\n\n"
        "Just send a voice message first!"
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store the voice message for later processing"""
    voice = update.message.voice
    
    # Send acknowledgement
    await update.message.reply_text("📥 Voice received! Now send one of these commands:\n`/chipmunk` `/deep` `/fast` `/slow` `/reverse` `/echo`", parse_mode='Markdown')
    
    # Download the voice file
    file = await context.bot.get_file(voice.file_id)
    file_path = f"voice_{update.effective_user.id}.ogg"
    await file.download_to_drive(file_path)
    
    # Store path in user data
    context.user_data['last_audio'] = file_path

async def apply_effect(update: Update, context: ContextTypes.DEFAULT_TYPE, effect_name):
    """Apply selected effect to the last voice message"""
    user_id = update.effective_user.id
    
    if 'last_audio' not in context.user_data:
        await update.message.reply_text("❌ Please send a voice message first!")
        return
    
    file_path = context.user_data['last_audio']
    
    if not os.path.exists(file_path):
        await update.message.reply_text("❌ Voice file expired. Please send a new voice message.")
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text(f"🎛️ Applying *{effect_name}* effect...", parse_mode='Markdown')
    
    # Convert the voice
    converted = await convert_voice(file_path, effect_name)
    
    if converted:
        # Save converted file
        output_path = f"converted_{user_id}_{effect_name}.ogg"
        converted.export(output_path, format="ogg")
        
        # Send back to user
        with open(output_path, 'rb') as audio_file:
            await update.message.reply_voice(voice=audio_file, caption=f"✅ Your voice with *{effect_name}* effect!", parse_mode='Markdown')
        
        # Cleanup
        os.remove(output_path)
        await processing_msg.delete()
    else:
        await processing_msg.edit_text("❌ Failed to apply effect. Please try again.")

async def chipmunk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "chipmunk")

async def deep_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "deep_voice")

async def fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "fast")

async def slow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "slow")

async def reverse_effect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "reverse")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "echo")

# Flask routes for health checks
@flask_app.route('/')
def index():
    return jsonify({"status": "Bot is running", "bot": "@VoicetoChangerbot"})

@flask_app.route('/health')
def health():
    return jsonify({"status": "ok"})

@flask_app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    """Handle incoming Telegram updates"""
    try:
        if bot_app:
            update = Update.de_json(request.get_json(force=True), bot_app.bot)
            asyncio.run(bot_app.process_update(update))
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error"}), 500

async def setup_webhook():
    """Set webhook for production"""
    app_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")
    webhook_url = f"https://{app_name}/{TOKEN}"
    
    await bot_app.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

if __name__ == "__main__":
    # Check if running on Render (production) or locally
    is_render = os.environ.get("RENDER", False)
    
    if is_render:
        # Production: Setup Flask with webhook
        bot_app = Application.builder().token(TOKEN).build()
        
        # Add handlers
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(CommandHandler("chipmunk", chipmunk))
        bot_app.add_handler(CommandHandler("deep", deep_voice))
        bot_app.add_handler(CommandHandler("fast", fast))
        bot_app.add_handler(CommandHandler("slow", slow))
        bot_app.add_handler(CommandHandler("reverse", reverse_effect))
        bot_app.add_handler(CommandHandler("echo", echo))
        bot_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
        
        # Set webhook on startup
        asyncio.run(setup_webhook())
        
        # Start Flask server
        port = int(os.environ.get("PORT", 10000))
        flask_app.run(host="0.0.0.0", port=port)
    else:
        # For local testing
        print("Run locally with: python app.py")
