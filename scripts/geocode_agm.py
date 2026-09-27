#!/usr/bin/env python3
"""
tdnet_agm_scraper.py が出力したCSVの住所を、国土地理院の住所検索APIで
緯度経度に変換し、地図表示用のGeoJSONを作るスクリプト。

使い方:
    pip install requests
    python geocode_agm.py --in agm.csv --out agm.geojson

出力:
    agm.geojson          … 地図に載せるデータ(1社=1地点)
    agm_failed.csv       … 位置が特定できなかった行(手作業で直す用)
    geocode_cache.json   … 検索結果のキャッシュ(再実行時にAPIを呼ばない)

国土地理院の住所検索APIは無料・APIキー不要ですが、
連続アクセスで負担をかけないよう --sleep で間隔を空けています。
地図上などでデータを公開する際は「出典:国土地理院」などの表記を入れてください。
"""

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
HEADERS = {"User-Agent": "AGM-map geocoder"}

KANJI_NUM = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def kanji_to_int(s):
    """「二十三」→23 のような簡単な漢数字変換(丁目・番地用)"""
    if not s:
        return None
    total, cur = 0, 0
    for ch in s:
        if ch == "千":
            total += (cur or 1) * 1000; cur = 0
        elif ch == "百":
            total += (cur or 1) * 100; cur = 0
        elif ch == "十":
            total += (cur or 1) * 10; cur = 0
        elif ch in KANJI_NUM:
            cur = cur * 10 + KANJI_NUM[ch]
        else:
            return None
    return total + cur


def normalize_address(addr):
    """APIが理解しやすい形に住所を整える"""
    a = unicodedata.normalize("NFKC", addr or "").strip()
    a = re.sub(r"\s+", "", a)
    a = a.replace("−", "-").replace("ー", "-").replace("‐", "-").replace("―", "-")
    # 丁目・番・号の漢数字をアラビア数字に(例:二丁目七番二号 → 2丁目7番2号)
    a = re.sub(r"([〇一二三四五六七八九十百千]+)(?=丁目|番|号)",
               lambda m: str(kanji_to_int(m.group(1)) or m.group(1)), a)
    # 建物名・階数など住所以外の部分を落とす(番地の後ろ)
    m = re.match(r"(.+?\d+(?:丁目)?(?:[-の]?\d+(?:番地?|号)?)*)", a)
    return m.group(1) if m else a


def gsi_search(session, query, cache, sleep):
    if query in cache:
        return cache[query]
    try:
        resp = session.get(GSI_URL, params={"q": query}, timeout=20)
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[warn] 検索失敗: {query} ({e})", file=sys.stderr)
        return None  # 失敗はキャッシュしない(次回再試行)
    finally:
        time.sleep(sleep)

    hit = None
    if results:
        lon, lat = results[0]["geometry"]["coordinates"]
        hit = {"lat": lat, "lon": lon, "matched": results[0]["properties"].get("title", "")}
    cache[query] = hit
    return hit


def _addr_key(addr):
    """比較用に住所を正規化する(全角→半角、漢数字→数字、丁目・番・号の表記ゆれを統一)"""
    a = normalize_address(addr)
    a = re.sub(r"([〇一二三四五六七八九十百千]+)(?=丁目|番|号|$)",
               lambda m: str(kanji_to_int(m.group(1)) or m.group(1)), a)
    town = re.split(r"\d", a, maxsplit=1)[0]          # 数字より前(都道府県〜町名)
    nums = re.findall(r"\d+", a[len(town):])           # 丁目・番・号の数字の並び
    town = re.sub(r"^(?:東京都|北海道|(?:京都|大阪)府|.{2,3}県)", "", town)  # 都道府県の有無は無視
    return town, nums


def match_level(query, matched):
    """どこまで一致したかの目安
    high   … 番地・号まで一致
    medium … 丁目や番までは一致(数十〜百m程度のずれ)
    low    … 町名レベル以下、または一致が確認できない
    """
    if not matched:
        return "low"
    qt, qn = _addr_key(query)
    mt, mn = _addr_key(matched)
    if not (qt.endswith(mt) or mt.endswith(qt)):
        return "low"
    k = 0  # 先頭から何個の数字(丁目・番・号)が一致したか
    while k < min(len(qn), len(mn)) and qn[k] == mn[k]:
        k += 1
    if qn and k == len(qn):
        return "high"      # 番地・号まで全部一致
    if k == len(mn) and k >= 1:
        return "medium"    # 検索結果が粗い(丁目や番まで)だけで、食い違いはない
    return "low"           # 数字が食い違っている(別の場所の可能性)


def geocode_row(session, row, cache, sleep):
    """住所→(住所から市区町村まで削ったもの)→会場名 の順に試す"""
    address = normalize_address(row.get("address", ""))
    candidates = []
    if address:
        candidates.append(("address", address))
        # 番地が見つからないとき用に「〜丁目」までに短縮したもの
        short = re.sub(r"(\d+丁目|[^\d]+)(\d.*)$", r"\1", address)
        if short and short != address:
            candidates.append(("address_short", short))
    venue = (row.get("venue_name") or "").split()[0] if row.get("venue_name") else ""
    if venue:
        candidates.append(("venue", venue))

    for source, q in candidates:
        hit = gsi_search(session, q, cache, sleep)
        if hit:
            level = match_level(q, hit["matched"]) if source == "address" else (
                "medium" if source == "address_short" else "low")
            return hit, source, q, level
    return None, "", "", ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", default="agm_notices.csv")
    ap.add_argument("--out", default="agm.geojson")
    ap.add_argument("--cache", default="geocode_cache.json")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--failed", default="", help="位置を特定できなかった行の出力先(既定は <out>_failed.csv)")
    args = ap.parse_args()

    cache_path = Path(args.cache)
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    with open(args.inp, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    session = requests.Session()
    session.headers.update(HEADERS)

    features, failed = [], []
    seen = set()
    for i, row in enumerate(rows, 1):
        # 訂正版などで同じ会社・同じ総会日が重複したら後のもの(新しい方)を優先
        key = (row.get("code"), row.get("meeting_date"))

        if row.get("online_only") in ("True", "true", "1"):
            row["fail_reason"] = "バーチャルオンリー総会(会場なし)"
            failed.append(row)
            continue
        if not row.get("address") and not row.get("venue_name"):
            row["fail_reason"] = "CSVに住所・会場名がない"
            failed.append(row)
            continue

        hit, source, query, level = geocode_row(session, row, cache, args.sleep)
        if i % 20 == 0:  # 途中で止まってもキャッシュが残るように定期保存
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

        if not hit:
            row["fail_reason"] = "位置が見つからない"
            failed.append(row)
            print(f"[{i}/{len(rows)}] {row.get('code')} {row.get('company')} -> 失敗")
            continue

        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [hit["lon"], hit["lat"]]},
            "properties": {
                "code": row.get("code", ""),
                "company": row.get("company", ""),
                "meeting_date": row.get("meeting_date", ""),
                "meeting_time": row.get("meeting_time", ""),
                "venue_name": row.get("venue_name", ""),
                "address": row.get("address", ""),
                "pdf_url": row.get("pdf_url", ""),
                "geocode_source": source,     # address / address_short / venue
                "geocode_query": query,
                "geocode_matched": hit["matched"],
                "geocode_level": level,       # high / medium / low(lowは要確認)
            },
        }
        if key in seen:
            features = [f for f in features
                        if (f["properties"]["code"], f["properties"]["meeting_date"]) != key]
        seen.add(key)
        features.append(feature)
        print(f"[{i}/{len(rows)}] {row.get('code')} {row.get('company')} -> {hit['matched']} ({level})")

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    geojson = {"type": "FeatureCollection", "features": features}
    Path(args.out).write_text(json.dumps(geojson, ensure_ascii=False, indent=1), encoding="utf-8")

    failed_path = Path(args.failed) if args.failed else Path(args.out).with_name(Path(args.out).stem + "_failed.csv")
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    if failed:
        fields = list(failed[0].keys())
        for r in failed:
            fields += [k for k in r if k not in fields]
        with failed_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(failed)

    levels = {}
    for f in features:
        lv = f["properties"]["geocode_level"]
        levels[lv] = levels.get(lv, 0) + 1
    print(f"\n完了: {args.out} に {len(features)} 地点(精度内訳 {levels})")
    if failed:
        print(f"位置を特定できなかった {len(failed)} 件は {failed_path} に出力しました")


if __name__ == "__main__":
    main()
