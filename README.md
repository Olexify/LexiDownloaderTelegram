# 📥 LexiDownloaderTelegram [LexiDownloaderBot](https://t.me/LexiDownloaderBot)

Host your own Telegram bot for downloading videos, audio, and files using `yt-dlp` and `FFmpeg`.

Launch your own downloader bot in a few minutes and keep full control over hosting and administration.

---

## ✨ Features

* Download videos and audio through Telegram
* Powered by `yt-dlp`
* Uses `FFmpeg` for media processing
* Admin system through `.env`
* Lightweight Python setup
* Supports hosting on your own machine or server
* Optional launcher `.bat` file included

---

## ⚙️ Installation

### Step 1 - Install required dependencies

You need:

* Python
* `yt-dlp`
* `FFmpeg`

Install `yt-dlp`:

```bash
py -m pip install -U yt-dlp
```

---

## 🎬 Install FFmpeg

Guide:

https://www.realityframeworks.com/how-to-install-ffmpeg-for-yt-dlp/#download-ffmpeg

Download:

https://github.com/BtbN/FFmpeg-Builds/releases

For Windows:

Find and download:

```text
ffmpeg-master-latest-win64-lgpl.zip
```

Extract it and add FFmpeg to your Windows PATH.

Screenshots:

![FFmpeg Step 1](https://github.com/user-attachments/assets/40534e6c-bc9e-4195-8912-6542bd15b32e)

![FFmpeg Step 2](https://github.com/user-attachments/assets/8b0ff6be-dc88-4b9f-9414-f213f029295f)

![FFmpeg Step 3](https://github.com/user-attachments/assets/16369ba9-29fd-4781-bcb7-4a8dec6dfa56)

![FFmpeg Step 4](https://github.com/user-attachments/assets/3dbd65a4-d4df-4c14-b11e-17c4e9328119)

---

## 🤖 Create your Telegram bot

Open Telegram and go to:

`@BotFather`

Steps:

1. Send:

```text
/newbot
```

2. Create your bot

3. Copy the bot token

4. Extract `envEXTRACT`

GitHub blocks uploading `.env` files, so you must manually create and populate your `.env`.

Example:

```env
BOT_TOKEN=your_bot_token_here
ADMIN_ID=your_user_id_here
```

---

## 👑 Become Admin

After launching LexiDownloader:

Run:

```text
/getMyUserID
```

inside your bot.

Copy your Telegram user ID and place it inside:

```env
ADMIN_ID=
```

Now your account becomes administrator.

---

## 🚀 Launching

Run:

```bash
python LexiDownloader.py
```

or use the included `.bat` launcher.

If using the batch file, change:

```text
D:\Projects\TelegramBots\YT_LexiDownloader
```

to your own directory.

You can also assign the included icon to make it behave like a normal desktop application.

![LexiDownloader Icon](https://github.com/user-attachments/assets/8ff573bb-73ab-4671-93f1-be596f3f5975)

---

## 💡 Optional hosting ideas

LexiDownloader can run on:

* Your PC
* VPS
* Home server
* Raspberry Pi
* Cloud hosting

---

## ⚠️ Important notes

* GitHub does not upload `.env` files automatically
* FFmpeg must be correctly installed and added to PATH
* Make sure your bot token remains private
* Never commit `.env` to public repositories

---

## 🧠 Philosophy

Downloaders should be simple:

No account walls.
No subscriptions.
No browser extensions fighting each other.
No mysterious background processes eating your RAM.

Just send a link and let the bot do its thing.

---

## 💙 Summary

* Telegram downloader bot
* Self-hosted
* yt-dlp powered
* FFmpeg support
* Admin system included
* Lightweight and customizable

---

> Download stuff. Host stuff. Stay based.
