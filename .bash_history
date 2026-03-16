rm -rf bot.py downloads
pkg update -y && pkg upgrade -y
pkg install python ffmpeg -y
pip install -U pyTelegramBotAPI yt-dlp
nano bot.py
python bot.py
