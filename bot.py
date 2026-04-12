from flask import Flask
import threading
import time
import os
import yfinance as yf
import pandas as pd
from alpaca.trading.client import TradingClient

# -----------------------
# ENV VARIABLES
# -----------------------
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

app = Flask(__name__)

# -----------------------
# BOT LOGIC
# -----------------------
def run_bot():
    print("Bot started...")

    while True:
        try:
            print("Fetching data...")

            df = yf.download("NBIS", period="5d", interval="1h")

            if df is None or df.empty or len(df) < 2:
                print("Not enough data, skipping...")
            else:
                latest = df.iloc[-1]
                prev = df.iloc[-2]

                print("Latest price:", latest["Close"])

                # Example condition (safe)
                if latest["Close"] > prev["Close"]:
                    print("Price going up 📈")
                else:
                    print("Price going down 📉")

            time.sleep(60)

        except Exception as e:
            print("Error:", e)
            time.sleep(60)

# -----------------------
# WEB SERVER (RENDER NEEDS THIS)
# -----------------------
@app.route("/")
def home():
    return "Bot is running"

# -----------------------
# START EVERYTHING
# -----------------------
if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    app.run(host="0.0.0.0", port=10000)
