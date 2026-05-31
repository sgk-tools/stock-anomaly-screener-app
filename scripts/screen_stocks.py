#!/usr/bin/env python3
"""Stock / ETF anomaly screener for GitHub Actions.

Purpose:
- Download or read Stooq daily bulk historical data.
- Screen Japan and U.S. stocks and ETFs.
- Calculate anomaly, momentum, trend, volume, 52-week high, and simplified CAN SLIM style signals.
- Write data/candidates.json for GitHub Pages.

Data source priority:
1. Local bulk zip files: data/raw/d_us_txt.zip, data/raw/d_jp_txt.zip
2. Stooq bulk links: https://stooq.com/db/d/?b=d_us_txt and d_jp_txt

Important:
Stooq bulk download can require authorization/captcha depending on access conditions.
If automated download is denied, the script keeps previous data when possible and writes an explicit error.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
OUT_FILE = DATA_DIR / "candidates.json"
LOG_FILE = DATA_DIR / "update_log.json"

STOOQ_BULK = {
    "US": "https://stooq.com/db/d/?b=d_us_txt",
    "JP": "https://stooq.com/db/d/?b=d_jp_txt",
}
LOCAL_ZIPS = {
    "US": RAW_DIR / "d_us_txt.zip",
    "JP": RAW_DIR / "d_jp_txt.zip",
}

MAX_PER_BUCKET = int(os.getenv("MAX_PER_BUCKET", "80"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "2600"))  # about 10 trading years
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))
USER_AGENT = "Mozilla/5.0 stock-anomaly-canslim-etf-screener/1.1"

MIN_DAYS = 260
MIN_AVG_VOL = {"US": 100_000, "JP": 50_000}
MIN_PRICE_STOCK = {"US": 5.0, "JP": 100.0}
MIN_PRICE_ETF = {"US": 5.0, "JP": 500.0}

BENCHMARK_SYMBOLS = {
    "US": ["SPY", "QQQ", "DIA", "IWM"],
    "JP": ["1306", "1321", "1308", "1475"],
}

Instrument = Literal["stock", "ETF"]


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Series:
    market: str
    symbol: str
    instrument: Instrument
    path: str
    bars: list[Bar]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pct(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value * 100.0, 2)


def safe_float(s: object) -> float | None:
    try:
        value = float(str(s).strip())
        if math.isfinite(value):
            return value
    except Exception:
        return None
    return None


def mean(values: list[float]) -> float | None:
    vals = [v for v in values if math.isfinite(v)]
    if not vals:
        return None
    return statistics.fmean(vals)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def download_zip(market: str) -> tuple[bytes | None, str]:
    local = LOCAL_ZIPS[market]
    if local.exists() and local.stat().st_size > 1_000_000:
        return local.read_bytes(), f"local:{local.as_posix()}"

    url = STOOQ_BULK[market]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            data = res.read()
            content_type = res.headers.get("content-type", "")
            # Valid bulk zips are large and begin with PK. Unauthorized responses are usually tiny text.
            if len(data) < 100_000 or not data[:2] == b"PK":
                preview = data[:140].decode("utf-8", "replace").replace("\n", " ")
                return None, f"download-denied:{market}:content_type={content_type}:preview={preview}"
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            return data, f"download:{url}"
    except urllib.error.HTTPError as e:
        return None, f"http-error:{market}:{e.code}:{e.reason}"
    except Exception as e:
        return None, f"download-error:{market}:{type(e).__name__}:{e}"


def infer_instrument(path: str) -> Instrument | None:
    low = path.lower().replace("\\", "/")
    if not low.endswith(".txt"):
        return None
    if any(x in low for x in ["indices", "options", "futures", "bonds", "rights", "warrants", "certificates", "forex", "crypto"]):
        return None
    if "etf" in low or "/etfs/" in low:
        return "ETF"
    if "stocks" in low:
        return "stock"
    return None


def is_target_file(path: str, market: str) -> bool:
    low = path.lower().replace("\\", "/")
    inst = infer_instrument(low)
    if inst is None:
        return False
    if market == "US":
        return ".us.txt" in low
    if market == "JP":
        return ".jp.txt" in low
    return False


def parse_symbol(path: str, market: str) -> str:
    name = Path(path.replace("\\", "/")).name.lower()
    if name.endswith(".txt"):
        name = name[:-4]
    if market == "US" and name.endswith(".us"):
        return name[:-3].upper()
    if market == "JP" and name.endswith(".jp"):
        return name[:-3]
    return name.upper()


def read_bars_from_text(raw: bytes) -> list[Bar]:
    text = raw.decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    bars: list[Bar] = []
    for row in reader:
        d = row.get("Date") or row.get("date") or row.get("DATE")
        if not d:
            continue
        o = safe_float(row.get("Open", row.get("open", "")))
        h = safe_float(row.get("High", row.get("high", "")))
        l = safe_float(row.get("Low", row.get("low", "")))
        c = safe_float(row.get("Close", row.get("close", "")))
        v = safe_float(row.get("Volume", row.get("volume", "0"))) or 0.0
        if o is None or h is None or l is None or c is None:
            continue
        bars.append(Bar(str(d), o, h, l, c, v))
    bars.sort(key=lambda b: b.date)
    if LOOKBACK_DAYS > 0 and len(bars) > LOOKBACK_DAYS:
        bars = bars[-LOOKBACK_DAYS:]
    return bars


def ret_from_index(bars: list[Bar], idx_back: int) -> float | None:
    if len(bars) <= idx_back:
        return None
    old = bars[-idx_back].close
    now = bars[-1].close
    if old <= 0:
        return None
    return now / old - 1.0


def same_month_stats(bars: list[Bar], target_month: int, latest_year: int) -> tuple[float | None, float | None, float | None, int]:
    by_year: dict[int, list[Bar]] = {}
    for b in bars:
        try:
            year = int(b.date[:4])
            month = int(b.date[5:7])
        except Exception:
            continue
        if month == target_month and year < latest_year:
            by_year.setdefault(year, []).append(b)
    returns: list[float] = []
    for arr in by_year.values():
        if len(arr) < 2:
            continue
        first = arr[0].close
        last = arr[-1].close
        if first > 0:
            returns.append(last / first - 1.0)
    if not returns:
        return None, None, None, 0
    win_rate = sum(1 for r in returns if r > 0) / len(returns)
    avg_return = statistics.fmean(returns)
    median_return = statistics.median(returns)
    return win_rate, avg_return, median_return, len(returns)


def calc_base_metrics(bars: list[Bar]) -> dict | None:
    if len(bars) < MIN_DAYS:
        return None
    latest = bars[-1]
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    ma20 = mean(closes[-20:])
    ma50 = mean(closes[-50:])
    ma200 = mean(closes[-200:])
    ma50_prev = mean(closes[-60:-10]) if len(closes) >= 260 else None
    ma200_prev = mean(closes[-220:-20]) if len(closes) >= 260 else None
    avg20_vol = mean(vols[-20:]) or 0.0
    high52 = max(b.high for b in bars[-252:])
    dist52 = latest.close / high52 - 1.0 if high52 > 0 else None
    is_new_52h = bool(dist52 is not None and dist52 >= -0.005)
    ret1m = ret_from_index(bars, 21)
    ret3m = ret_from_index(bars, 63)
    ret6m = ret_from_index(bars, 126)
    ret12m = ret_from_index(bars, 252)
    try:
        latest_year = int(latest.date[:4])
        latest_month = int(latest.date[5:7])
    except Exception:
        latest_year, latest_month = datetime.utcnow().year, datetime.utcnow().month
    win_rate, avg_month_ret, median_month_ret, sample_n = same_month_stats(bars, latest_month, latest_year)
    vol_ratio = latest.volume / avg20_vol if avg20_vol > 0 else None
    return {
        "latest": latest,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "ma50Prev": ma50_prev,
        "ma200Prev": ma200_prev,
        "avg20Vol": avg20_vol,
        "dist52": dist52,
        "isNew52wHigh": is_new_52h,
        "ret1m": ret1m,
        "ret3m": ret3m,
        "ret6m": ret6m,
        "ret12m": ret12m,
        "winRate": win_rate,
        "avgMonthRet": avg_month_ret,
        "medianMonthRet": median_month_ret,
        "sampleN": sample_n,
        "volRatio": vol_ratio,
    }


def calc_market_signal(series_map: dict[tuple[str, str], Series]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for market, symbols in BENCHMARK_SYMBOLS.items():
        chosen = None
        metrics = None
        for symbol in symbols:
            s = series_map.get((market, symbol))
            if not s:
                continue
            m = calc_base_metrics(s.bars)
            if m:
                chosen = symbol
                metrics = m
                break
        if not chosen or not metrics:
            out[market] = {"symbol": None, "score": 50, "label": "中立", "reason": "指数ETFデータなし"}
            continue
        score = 0.0
        latest = metrics["latest"]
        ma50 = metrics["ma50"]
        ma200 = metrics["ma200"]
        if ma50 and latest.close > ma50:
            score += 25
        if ma200 and latest.close > ma200:
            score += 25
        if ma50 and ma200 and ma50 > ma200:
            score += 20
        if metrics["ret1m"] is not None and metrics["ret1m"] > 0:
            score += 15
        if metrics["ret3m"] is not None and metrics["ret3m"] > 0:
            score += 15
        label = "良好" if score >= 70 else "中立" if score >= 45 else "弱い"
        out[market] = {"symbol": chosen, "score": round(score, 1), "label": label, "reason": f"{chosen}基準"}
    return out


def component_scores(market: str, instrument: Instrument, metrics: dict, market_signal: dict) -> dict:
    latest = metrics["latest"]
    ma50 = metrics["ma50"]
    ma200 = metrics["ma200"]
    ma50_prev = metrics["ma50Prev"]
    ma200_prev = metrics["ma200Prev"]
    win_rate = metrics["winRate"]
    avg_month_ret = metrics["avgMonthRet"]
    sample_n = metrics["sampleN"]
    ret1m = metrics["ret1m"]
    ret3m = metrics["ret3m"]
    ret6m = metrics["ret6m"]
    ret12m = metrics["ret12m"]
    dist52 = metrics["dist52"]
    vol_ratio = metrics["volRatio"]

    anomaly = 0.0
    if win_rate is not None and avg_month_ret is not None and sample_n >= 3:
        anomaly += clamp(win_rate * 55, 0, 55)
        anomaly += clamp(avg_month_ret / 0.08 * 35, 0, 35)
        anomaly += clamp(sample_n / 8 * 10, 0, 10)

    trend = 0.0
    if ma50 and latest.close > ma50:
        trend += 25
    if ma200 and latest.close > ma200:
        trend += 25
    if ma50 and ma200 and ma50 > ma200:
        trend += 25
    if ma50 and ma50_prev and ma50 > ma50_prev:
        trend += 12.5
    if ma200 and ma200_prev and ma200 > ma200_prev:
        trend += 12.5

    momentum = 0.0
    for r, weight, cap in [(ret1m, 25, 0.12), (ret3m, 35, 0.25), (ret6m, 25, 0.40), (ret12m, 15, 0.70)]:
        if r is not None:
            momentum += clamp(r / cap * weight, 0, weight)

    high = 0.0
    if dist52 is not None:
        if dist52 >= -0.02:
            high = 100
        elif dist52 >= -0.05:
            high = 85
        elif dist52 >= -0.10:
            high = 65
        elif dist52 >= -0.15:
            high = 45
        elif dist52 >= -0.25:
            high = 20

    volume = 0.0
    if vol_ratio is not None:
        if vol_ratio >= 2.0:
            volume = 100
        elif vol_ratio >= 1.5:
            volume = 75
        elif vol_ratio >= 1.1:
            volume = 50
        elif vol_ratio >= 0.8:
            volume = 25

    # Simplified CAN SLIM using only price/volume data.
    # C/A/I are not available from Stooq daily bars, so they are marked for manual review.
    n_score = high  # N = new high / 52w high proximity
    s_score = volume  # S = demand/volume change
    l_score = momentum  # L = leader / relative strength proxy
    m_score = market_signal.get("score", 50)  # M = market direction proxy
    canslim_simple = (n_score * 0.25) + (s_score * 0.20) + (l_score * 0.30) + (m_score * 0.25)

    if instrument == "ETF":
        # ETFs are not operating companies, so C/A/I should not be treated as CAN SLIM.
        overall = (anomaly * 0.25) + (momentum * 0.30) + (trend * 0.25) + (high * 0.12) + (volume * 0.08)
    else:
        overall = (anomaly * 0.25) + (canslim_simple * 0.50) + (trend * 0.15) + (volume * 0.10)

    return {
        "anomaly": round(anomaly, 1),
        "trend": round(trend, 1),
        "momentum": round(momentum, 1),
        "high": round(high, 1),
        "volume": round(volume, 1),
        "canslimSimple": round(canslim_simple, 1),
        "market": round(m_score, 1),
        "overall": round(overall, 1),
    }


def compute_candidate(series: Series, market_signal: dict) -> dict | None:
    metrics = calc_base_metrics(series.bars)
    if not metrics:
        return None
    latest: Bar = metrics["latest"]
    min_price = MIN_PRICE_ETF[series.market] if series.instrument == "ETF" else MIN_PRICE_STOCK[series.market]
    if latest.close < min_price:
        return None
    if metrics["avg20Vol"] < MIN_AVG_VOL[series.market]:
        return None

    scores = component_scores(series.market, series.instrument, metrics, market_signal)
    score = scores["overall"]
    if series.instrument == "ETF":
        if score >= 72:
            judgement = "注目ETF"
        elif score >= 58:
            judgement = "監視"
        else:
            judgement = "見送り"
    else:
        if score >= 72:
            judgement = "買い候補"
        elif score >= 58:
            judgement = "監視"
        else:
            judgement = "見送り"

    reasons: list[str] = []
    if metrics["winRate"] is not None and metrics["avgMonthRet"] is not None and metrics["sampleN"] >= 3:
        reasons.append(f"同月勝率{metrics['winRate']*100:.0f}%・平均{metrics['avgMonthRet']*100:.1f}%")
    if metrics["ma50"] and metrics["ma200"] and latest.close > metrics["ma50"] > metrics["ma200"]:
        reasons.append("50日線・200日線の上")
    if metrics["dist52"] is not None and metrics["dist52"] >= -0.08:
        reasons.append("52週高値圏")
    if metrics["volRatio"] is not None and metrics["volRatio"] >= 1.5:
        reasons.append(f"出来高{metrics['volRatio']:.1f}倍")
    if series.instrument == "stock" and scores["canslimSimple"] >= 70:
        reasons.append("簡易CAN SLIM強め")
    if series.instrument == "ETF" and scores["trend"] >= 70:
        reasons.append("ETFトレンド良好")
    if not reasons:
        reasons.append("条件不足")

    return {
        "market": series.market,
        "symbol": series.symbol,
        "instrument": series.instrument,
        "latestDate": latest.date,
        "close": round(latest.close, 4),
        "score": score,
        "judgement": judgement,
        "reason": " / ".join(reasons[:5]),
        "scores": scores,
        "canslim": {
            "simpleScore": scores["canslimSimple"],
            "C": "要確認: 四半期EPS成長",
            "A": "要確認: 年間EPS成長",
            "N": "52週高値接近" if metrics["dist52"] is not None and metrics["dist52"] >= -0.08 else "弱い/要確認",
            "S": "出来高増" if metrics["volRatio"] is not None and metrics["volRatio"] >= 1.5 else "通常",
            "L": "強い" if scores["momentum"] >= 65 else "中立/弱い",
            "I": "要確認: 機関投資家保有",
            "M": market_signal.get("label", "中立"),
        } if series.instrument == "stock" else None,
        "metrics": {
            "ret1mPct": pct(metrics["ret1m"]),
            "ret3mPct": pct(metrics["ret3m"]),
            "ret6mPct": pct(metrics["ret6m"]),
            "ret12mPct": pct(metrics["ret12m"]),
            "sameMonthWinRatePct": pct(metrics["winRate"]),
            "sameMonthAvgReturnPct": pct(metrics["avgMonthRet"]),
            "sameMonthMedianReturnPct": pct(metrics["medianMonthRet"]),
            "sameMonthSamples": metrics["sampleN"],
            "dist52HighPct": pct(metrics["dist52"]),
            "volumeRatio": round(metrics["volRatio"], 2) if metrics["volRatio"] is not None and math.isfinite(metrics["volRatio"]) else None,
            "ma50": round(metrics["ma50"], 4) if metrics["ma50"] else None,
            "ma200": round(metrics["ma200"], 4) if metrics["ma200"] else None,
        },
    }


def collect_series(market: str, zip_bytes: bytes) -> tuple[list[Series], dict]:
    results: list[Series] = []
    scanned = 0
    excluded = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if is_target_file(n, market)]
        for idx, name in enumerate(names, 1):
            scanned += 1
            try:
                instrument = infer_instrument(name)
                if instrument is None:
                    excluded += 1
                    continue
                raw = zf.read(name)
                bars = read_bars_from_text(raw)
                if len(bars) < MIN_DAYS:
                    excluded += 1
                    continue
                symbol = parse_symbol(name, market)
                results.append(Series(market=market, symbol=symbol, instrument=instrument, path=name, bars=bars))
            except Exception:
                excluded += 1
            if idx % 1000 == 0:
                print(f"{market}: read {idx}/{len(names)}", flush=True)
    return results, {"files": len(names), "scanned": scanned, "excluded": excluded, "series": len(results)}


def build_buckets(candidates_by_market: dict[str, list[dict]]) -> dict[str, list[dict]]:
    jp = sorted([x for x in candidates_by_market.get("JP", []) if x["instrument"] == "stock"], key=lambda x: x["score"], reverse=True)[:MAX_PER_BUCKET]
    us = sorted([x for x in candidates_by_market.get("US", []) if x["instrument"] == "stock"], key=lambda x: x["score"], reverse=True)[:MAX_PER_BUCKET]
    etf = sorted([x for arr in candidates_by_market.values() for x in arr if x["instrument"] == "ETF"], key=lambda x: x["score"], reverse=True)[:MAX_PER_BUCKET]
    all_items = sorted(jp + us + etf, key=lambda x: x["score"], reverse=True)[:MAX_PER_BUCKET]
    return {"ALL": all_items, "JP": jp, "US": us, "ETF": etf}


def write_json(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG_FILE.write_text(json.dumps({
        "generatedAtUtc": payload.get("generatedAtUtc"),
        "status": payload.get("status"),
        "sources": payload.get("sources"),
        "errors": payload.get("errors"),
        "summary": payload.get("summary"),
        "marketSignals": payload.get("marketSignals"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def load_existing_or_empty(errors: list[str]) -> dict:
    if OUT_FILE.exists():
        try:
            old = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            old["status"] = "stale"
            old["message"] = "今回の自動更新に失敗したため、前回保存データを表示している。"
            old["errors"] = errors
            old["generatedAtUtc"] = utc_now_iso()
            return old
        except Exception:
            pass
    return {
        "version": "1.1",
        "status": "error",
        "message": "データ取得に失敗。Stooq一括ダウンロードが認証で拒否された可能性がある。",
        "generatedAtUtc": utc_now_iso(),
        "sources": [],
        "errors": errors,
        "summary": {"US": 0, "JP": 0},
        "marketSignals": {},
        "rules": {},
        "candidates": {"US": [], "JP": [], "ETF": [], "ALL": []},
    }


def main() -> int:
    sources = []
    errors = []
    raw_series: list[Series] = []
    summary: dict[str, dict] = {}

    for market in ["US", "JP"]:
        print(f"Downloading/reading {market} bulk data...", flush=True)
        data, source = download_zip(market)
        sources.append({"market": market, "source": source})
        if not data:
            errors.append(source)
            continue
        try:
            series, stat = collect_series(market, data)
            raw_series.extend(series)
            summary[market] = stat
        except Exception as e:
            msg = f"process-error:{market}:{type(e).__name__}:{e}"
            errors.append(msg)
            summary[market] = {"error": msg}

    if not raw_series:
        payload = load_existing_or_empty(errors)
        write_json(payload)
        return 1

    series_map = {(s.market, s.symbol): s for s in raw_series}
    market_signals = calc_market_signal(series_map)
    candidates_by_market: dict[str, list[dict]] = {"US": [], "JP": []}

    for idx, s in enumerate(raw_series, 1):
        try:
            item = compute_candidate(s, market_signals.get(s.market, {"score": 50, "label": "中立"}))
            if item:
                candidates_by_market[s.market].append(item)
        except Exception:
            pass
        if idx % 1000 == 0:
            print(f"Scored {idx}/{len(raw_series)}", flush=True)

    buckets = build_buckets(candidates_by_market)
    status = "ok" if any(buckets[k] for k in ["US", "JP", "ETF", "ALL"]) else "error"

    if status == "error":
        payload = load_existing_or_empty(errors or ["no-candidates-after-screening"])
    else:
        payload = {
            "version": "1.1",
            "status": "ok" if not errors else "partial",
            "message": "自動スクリーニング完了。CAN SLIM簡易フィルターとETF表示を追加。これは売買推奨ではなく候補抽出。",
            "generatedAtUtc": utc_now_iso(),
            "sources": sources,
            "errors": errors,
            "marketSignals": market_signals,
            "rules": {
                "seasonality": "直近月と同じ月の過去勝率・平均騰落率・中央値",
                "canslimSimple": "N=52週高値接近、S=出来高倍率、L=モメンタム、M=指数ETFトレンド。C/A/Iは要個別確認。",
                "etf": "ETFはCAN SLIM銘柄判定ではなく、アノマリー・モメンタム・トレンド・出来高で評価。",
                "exclusions": "データ不足、低価格、低出来高を除外",
            },
            "summary": summary,
            "candidates": buckets,
        }
    write_json(payload)
    print(f"Wrote {OUT_FILE}", flush=True)
    if errors:
        print("Errors:", errors, flush=True)
    return 0 if status != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
