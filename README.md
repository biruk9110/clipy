---
title: Clipy AI Auto Clipper
emoji: ✂️
colorFrom: purple
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
---

# Clipy - AI Stream & Short Video Auto-Clipper

An automated, mobile-friendly AI video clipper and social media scheduler.
Paste a YouTube or TikTok link (or upload a video directly from your phone), and Clipy will:
1. **Intelligently detect length**:
   - **Short Videos (< 2.5 min)**: Polishes the video, adds animated word-by-word subtitles, adds a viral hook bar, normalizes audio to -14 LUFS, and generates viral captions & hashtags.
   - **Long Streams / Videos (> 2.5 min)**: Scans the entire stream, finds the most viral moments using **Gemini 2.0 Flash**, and cuts 3 or more 9:16 vertical clips.
2. **Auto-Post on Schedule**: Staggers uploads (e.g. 1 clip now, 1 clip in 3 hours, 1 clip in 6 hours) to **TikTok** and **Instagram Reels**.
3. **100% Free**: Uses local speech recognition (`faster-whisper`), local video editing (`ffmpeg`), and the free tier of Google's Gemini API.

---

## 📱 How to Run on Your Phone

### Option A: 24/7 Free Cloud (PC Can Be Turned Off)
To use Clipy from your phone anywhere while your PC is off:
1. Create a free account at [Hugging Face](https://huggingface.co/).
2. Click **New Space** -> Name it `clipy` -> Select **Docker** (Blank).
3. Upload all files from this `clipy` folder to your Space repo.
4. Hugging Face will automatically build and launch it 24/7 for **$0**.
5. Open your Space URL on your phone's browser and tap:
   - **iPhone**: Share button -> **Add to Home Screen**.
   - **Android**: Menu (3 dots) -> **Install App**.

### Option B: Run Locally on Your PC
1. Double-click `start.bat` (or run `python -m uvicorn main:app --host 0.0.0.0 --port 8000`).
2. Open on your PC: `http://localhost:8000`
3. To open on your phone while on the same Wi-Fi, open: `http://<YOUR_PC_IP>:8000` (e.g. `http://192.168.1.5:8000`).

---

## 🔑 Adding Your Free Gemini API Key
1. Go to [Google AI Studio](https://aistudio.google.com/) and click **Get API Key** (it's 100% free).
2. Open Clipy -> Go to the **Settings** tab.
3. Paste your Gemini API key and tap **Save**.

