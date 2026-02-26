import os
import telebot

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

if ":" not in TOKEN:
    raise ValueError("Token must contain a colon")

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=["start", "help"])
def start(m):
    bot.reply_to(m, "✅ البوت شغال على السيرفر! ابعت لينك بعدين نركب نسخة التحميل.")

print("Bot running...", flush=True)
bot.infinity_polling()
