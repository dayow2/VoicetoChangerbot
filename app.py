import os
import io
import logging
import asyncio
from flask import Flask, request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from pydub import AudioSegment
import pedalboard as pb
import numpy as np
import soundfile as sf

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("No TELEGRAM_TOKEN environment variable set")

# Flask app for health checks
flask_app = Flask(__name__)

# Global bot application instance (will be initialized after webhook)
bot_app = None

# Effect presets using pedalboard
EFFECTS = {
    "chipmunk": [pb.PitchShift(semitones=7)],
    "deep_voice": [pb.PitchShift(semitones=-5)],
    "robot": [pb.PhoneModeling()],
    "echo": [pb.Delay(delay_seconds=0.3, feedback=0.5, mix=0.4)],
    "reverb": [pb.Reverb(room_size=0.7, wet_level=0.3)],
    "fast": [pb.TimeStretch(rate=1.5)],
    "slow": [pb.TimeStretch(rate=0.75)],
    "reverse": []  # handled separately
}

async def convert_voice(voice_file_path, effect_name):
    """Apply selected effect to audio file"""
    try:
        # Load audio
        audio = AudioSegment.from_file(voice_file_path)
        
        # Convert to numpy array for pedalboard
        samples = np.array(audio.get_array_of_samples())
        if audio.channels == 2:
            samples = samples.reshape(-1, 2)
        
        sample_rate = audio.frame_rate
        
        # Special case for reverse
        if effect_name == "reverse":
            reversed_audio = audio.reverse()
            return reversed_audio
        
        # Apply effects
        if effect_name in EFFECTS and EFFECTS[effect_name]:
            processed = pb.process(samples, sample_rate, EFFECTS[effect_name])
            
            # Convert back to AudioSegment
            processed_int16 = (processed * 32767).astype(np.int16)
            if processed.ndim == 1 and audio.channels == 2:
                processed_int16 = np.column_stack((processed_int16, processed_int16))
            
            result = AudioSegment(
                processed_int16.tobytes(),
                frame_rate=sample_rate,
                sample_width=2,
                channels=audio.channels
            )
            return result
        
        return audio
        
    except Exception as e:
        logger.error(f"Error converting voice: {e}")
        return None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🎤 *Welcome to VoiceToChangerBot!*\n\n"
        "Send me any voice message or audio file, and I'll transform your voice!\n\n"
        "Available effects:\n"
        "🔹 `/chipmunk` - High pitch squeaky voice\n"
        "🔹 `/deep` - Low giant/demon voice\n"
        "🔹 `/robot` - Futuristic robot voice\n"
        "🔹 `/echo` - Stadium echo effect\n"
        "🔹 `/reverb` - Cave/concert hall\n"
        "🔹 `/fast` - Speed up\n"
        "🔹 `/slow` - Slow motion\n"
        "🔹 `/reverse` - Play backwards\n\n"
        "Just send a voice message, then tap the effect you want!"
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store the voice message for later processing"""
    voice = update.message.voice
    
    # Download the voice file
    file = await context.bot.get_file(voice.file_id)
    file_path = f"voice_{update.effective_user.id}.ogg"
    await file.download_to_drive(file_path)
    
    # Store path in user data
    context.user_data['last_audio'] = file_path
    
    # Show effect options
    keyboard = [
        ['/chipmunk', '/deep', '/robot'],
        ['/echo', '/reverb', '/fast'],
        ['/slow', '/reverse']
    ]
    
    reply_text = "Voice received! Choose an effect:"
    await update.message.reply_text(reply_text, reply_markup=create_keyboard(keyboard))

def create_keyboard(buttons):
    """Create inline keyboard for effects"""
    from telegram import ReplyKeyboardMarkup
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)

async def apply_effect(update: Update, context: ContextTypes.DEFAULT_TYPE, effect_name):
    """Apply selected effect to the last voice message"""
    user_id = update.effective_user.id
    
    if 'last_audio' not in context.user_data:
        await update.message.reply_text("Please send a voice message first!")
        return
    
    file_path = context.user_data['last_audio']
    
    if not os.path.exists(file_path):
        await update.message.reply_text("Voice file not found. Please send a new voice message.")
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text(f"🎛️ Applying {effect_name} effect...")
    
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

async def robot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "robot")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "echo")

async def reverb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "reverb")

async def fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "fast")

async def slow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "slow")

async def reverse_effect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await apply_effect(update, context, "reverse")

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
        return jsonify({"status": "error", "message": str(e)}), 500

async def setup_webhook():
    """Set webhook for production"""
    app_name = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "localhost")
    webhook_url = f"https://{app_name}/{TOKEN}"
    
    await bot_app.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

def run_polling():
    """Run in polling mode for local development"""
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("chipmunk", chipmunk))
    application.add_handler(CommandHandler("deep", deep_voice))
    application.add_handler(CommandHandler("robot", robot))
    application.add_handler(CommandHandler("echo", echo))
    application.add_handler(CommandHandler("reverb", reverb))
    application.add_handler(CommandHandler("fast", fast))
    application.add_handler(CommandHandler("slow", slow))
    application.add_handler(CommandHandler("reverse", reverse_effect))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Check if running on Render (production) or locally
    is_render = os.environ.get("RENDER", False)
    
    if is_render:
        # Production: Setup Flask with webhook
        global bot_app
        bot_app = Application.builder().token(TOKEN).build()
        
        # Add handlers
        bot_app.add_handler(CommandHandler("start", start_command))
        bot_app.add_handler(CommandHandler("chipmunk", chipmunk))
        bot_app.add_handler(CommandHandler("deep", deep_voice))
        bot_app.add_handler(CommandHandler("robot", robot))
        bot_app.add_handler(CommandHandler("echo", echo))
        bot_app.add_handler(CommandHandler("reverb", reverb))
        bot_app.add_handler(CommandHandler("fast", fast))
        bot_app.add_handler(CommandHandler("slow", slow))
        bot_app.add_handler(CommandHandler("reverse", reverse_effect))
        bot_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
        
        # Set webhook on startup
        asyncio.run(setup_webhook())
        
        # Start Flask server
        port = int(os.environ.get("PORT", 10000))
        flask_app.run(host="0.0.0.0", port=port)
    else:
        # Local development: Use polling
        run_polling()
