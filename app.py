import re
from collections import defaultdict

import os

import requests
from flask import Flask, render_template, jsonify, request

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
CBOE_HEADERS = {"User-Agent": "Mozilla/5.0"}


def parse_option_symbol(sym):
    """Parse CBOE option symbol like NVDA260311C00182500."""
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", sym)
    if not m:
        return None
    ticker, date_str, cp, strike_raw = m.groups()
    exp = f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}"
    strike = int(strike_raw) / 1000
    return {"exp": exp, "type": cp, "strike": strike}


def fetch_cboe_options(symbol):
    """Fetch full option chain from CBOE delayed quotes API."""
    r = requests.get(CBOE_URL.format(symbol=symbol.upper()), headers=CBOE_HEADERS)
    r.raise_for_status()
    data = r.json()
    current_price = data["data"]["close"]
    raw_options = data["data"]["options"]

    # Group by expiration
    by_exp = defaultdict(list)
    for o in raw_options:
        p = parse_option_symbol(o["option"])
        if not p:
            continue
        by_exp[p["exp"]].append({
            "type": p["type"],
            "strike": p["strike"],
            "oi": o.get("open_interest") or 0,
            "volume": o.get("volume") or 0,
        })

    return current_price, dict(by_exp)


def _calc_pain(strikes, call_w, put_w):
    """Compute pain curve given strike->weight mappings. Returns (pain_list, max_pain_strike)."""
    pain = []
    for price in strikes:
        cp = sum(max(price - s, 0) * call_w[s] for s in strikes)
        pp = sum(max(s - price, 0) * put_w[s] for s in strikes)
        pain.append(cp + pp)
    min_idx = pain.index(min(pain))
    return pain, strikes[min_idx]


def calc_max_pain(options_for_exp):
    """Calculate OI-based and Volume-based max pain."""
    call_oi = defaultdict(float)
    put_oi = defaultdict(float)
    call_vol = defaultdict(float)
    put_vol = defaultdict(float)
    for o in options_for_exp:
        if o["type"] == "C":
            call_oi[o["strike"]] += o["oi"]
            call_vol[o["strike"]] += o["volume"]
        else:
            put_oi[o["strike"]] += o["oi"]
            put_vol[o["strike"]] += o["volume"]

    strikes = sorted(set(call_oi.keys()) | set(put_oi.keys()) | set(call_vol.keys()) | set(put_vol.keys()))

    pain_oi, mp_oi = _calc_pain(strikes, call_oi, put_oi)
    pain_vol, mp_vol = _calc_pain(strikes, call_vol, put_vol)

    total_vol = sum(call_vol.values()) + sum(put_vol.values())

    return {
        "strikes": strikes,
        "pain": pain_oi,
        "max_pain": mp_oi,
        "pain_vol": pain_vol,
        "max_pain_vol": mp_vol,
        "total_volume": total_vol,
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/expirations")
def expirations():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "Missing symbol"}), 400
    try:
        price, by_exp = fetch_cboe_options(symbol)
        exps = sorted(by_exp.keys())
        if not exps:
            return jsonify({"error": f"No options found for {symbol}"}), 404
        return jsonify({"expirations": exps, "price": price, "symbol": symbol})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/maxpain")
def maxpain():
    symbol = request.args.get("symbol", "").strip().upper()
    exp = request.args.get("expiration", "").strip()
    if not symbol or not exp:
        return jsonify({"error": "Missing symbol or expiration"}), 400
    try:
        price, by_exp = fetch_cboe_options(symbol)
        if exp not in by_exp:
            return jsonify({"error": f"No data for expiration {exp}"}), 404
        result = calc_max_pain(by_exp[exp])
        result["price"] = price
        result["symbol"] = symbol
        result["expiration"] = exp
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/quick")
def quick():
    """Return nearest expiration max pain for a symbol (for favorites bar)."""
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "Missing symbol"}), 400
    try:
        price, by_exp = fetch_cboe_options(symbol)
        exps = sorted(by_exp.keys())
        if not exps:
            return jsonify({"error": f"No options for {symbol}"}), 404
        nearest = exps[0]
        result = calc_max_pain(by_exp[nearest])
        diff = price - result["max_pain"]
        return jsonify({
            "symbol": symbol,
            "price": price,
            "max_pain": result["max_pain"],
            "expiration": nearest,
            "diff": diff,
            "diff_pct": diff / result["max_pain"] * 100 if result["max_pain"] else 0,
        })
    except Exception as e:
        return jsonify({"error": str(e), "symbol": symbol}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5555)
