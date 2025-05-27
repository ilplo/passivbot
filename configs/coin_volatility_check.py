import requests
import time
import json
from datetime import datetime, timedelta
import os

INPUT_FILE = "top_20_coins_1000_mcap_5_usd_bybit.json"
OUTPUT_FILE = f"{INPUT_FILE}_filtered.json"

MAX_VOLATILITY = 1000.0

def load_coin_list(filename):
    try:
        with open(filename, 'r') as f:
            data = json.load(f)

        if isinstance(data, list):
            return [str(symbol).upper() for symbol in data]
        else:
            print("Invalid format: expected a list in JSON file.")
            return []
    except Exception as e:
        print(f"Error reading coin list from {filename}: {e}")
        return []

def fetch_top_symbols(n_coins=100):
    print(f"Fetching top {n_coins} coins by market cap...")
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        'vs_currency': 'usd',
        'order': 'market_cap_desc',
        'per_page': n_coins,
        'page': 1
    }

    while True:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            break
        print(f"Error fetching market cap list: {response.status_code}. Retrying in 15s...")
        time.sleep(15)

    top_coins = response.json()
    return {coin['symbol'].upper(): coin['id'] for coin in top_coins}

def fetch_price_data(coin_id, retries=10):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
    today = int(time.time())
    one_year_ago = int((datetime.now() - timedelta(days=365)).timestamp())

    params = {
        'vs_currency': 'usd',
        'from': one_year_ago,
        'to': today
    }

    for attempt in range(retries):
        try:
            response = requests.get(url, params=params)
            if response.status_code == 200:
                return response.json().get('prices', [])
            elif response.status_code == 429:
                print("Rate limit hit. Waiting 15 seconds...")
                time.sleep(15)
            else:
                print(f"Error {response.status_code} for {coin_id}: {response.text}")
                time.sleep(15)
        except Exception as e:
            print(f"Request error for {coin_id}: {e}")
            time.sleep(15)
    return []

# Step 1: Load coin symbols
symbols = load_coin_list(INPUT_FILE)
if not symbols:
    print("No valid symbols to process. Exiting.")
    exit(1)

# Step 2: Get top coin IDs
symbol_to_id = fetch_top_symbols()

# Step 3: Filter valid symbols
valid_symbols = [s for s in symbols if s in symbol_to_id]
invalid_symbols = [s for s in symbols if s not in symbol_to_id]
if invalid_symbols:
    print(f"Skipped invalid or non-top150 symbols: {', '.join(invalid_symbols)}")

results = {}

# Step 4: Process each valid symbol
for i, symbol in enumerate(valid_symbols):
    coin_id = symbol_to_id[symbol]
    print(f"[{i+1}/{len(valid_symbols)}] Fetching historical data for {symbol} ({coin_id})")

    prices = fetch_price_data(coin_id)
    if not prices:
        print(f"No price data for {symbol}. Skipping.")
        continue

    price_values = [p[1] for p in prices]
    max_price = max(price_values)
    min_price = min(price_values)
    percent_diff = ((max_price - min_price) / min_price) * 100
    print(f"Max: ${max_price:.2f}, Min: ${min_price:.2f}, Diff: {percent_diff:.2f}%")

    results[symbol] = {
        'coin_id': coin_id,
        'max': max_price,
        'min': min_price,
        'diff_percent': round(percent_diff, 2)
    }

    time.sleep(2)  # Respect CoinGecko rate limits

# Step 5: Output results
print("\n--- 1-Year Price Range: Max vs Min (%) ---")
filtered_symbols = []
for symbol, data in results.items():
    print(f"{symbol} ({data['coin_id']}): Max ${data['max']:.2f}, Min ${data['min']:.2f}, Diff: {data['diff_percent']}%")
    if data['diff_percent'] < MAX_VOLATILITY:
        filtered_symbols.append(symbol)

removed_symbols = set(symbols) - set(filtered_symbols)
if removed_symbols:
    print(f"\nRemoved symbols with >{MAX_VOLATILITY}% price difference: {', '.join(removed_symbols)}")

# Step 6: Write a filtered list to file
try:
    with open(OUTPUT_FILE, "w") as f:
        json.dump(filtered_symbols, f, indent=2)
    print(f"Saved filtered coin list to '{OUTPUT_FILE}' with {len(filtered_symbols)} symbols.")
except Exception as e:
    print(f"Failed to save filtered coin list: {e}")
