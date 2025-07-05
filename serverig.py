
# Fixed serverig.py (keep_alive server)
from flask import Flask
from threading import Thread
import os

app = Flask('')

@app.route('/')
def home():
    return "Discord Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True  # Make thread daemon so it doesn't prevent shutdown
    t.start()
