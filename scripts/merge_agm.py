#!/usr/bin/env python3
"""
今週取得した招集通知のCSVを、これまでの蓄積データ(data/agm_all.csv)に統合する。

TDnetは31日分しか残らないため、毎週の結果を貯めていくことで
総会シーズン全体のデータを保持する。

使い方:
    python scripts/merge_agm.py --new data/this_week.csv --all data/agm_all.csv
"""
import argparse
import csv
import datetime as dt
from pathlib import Path


def read(path):
    """(列名のリスト, 行のリスト) を返す。ファイルがなければ空"""
    p = Path(path)
    if not p.exists():
        return [], []
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", required=True)
    ap.add_argument("--all", required=True)
    ap.add_argument("--keep-days", type=int, default=400,
                    help="開催日がこの日数より前の総会は削除する(既定400日)")
    args = ap.parse_args()

    old_fields, old_rows = read(args.all)
    new_fields, new_rows = read(args.new)

    # 同じ開示資料(pdf_url)は新しい取得結果で上書き
    merged = {r["pdf_url"]: r for r in old_rows if r.get("pdf_url")}
    added = sum(1 for r in new_rows if r.get("pdf_url") and r["pdf_url"] not in merged)
    for r in new_rows:
        if r.get("pdf_url"):
            merged[r["pdf_url"]] = r

    # 古すぎる総会を削除(開催日が不明なものは開示日で判断)
    cutoff = (dt.date.today() - dt.timedelta(days=args.keep_days)).isoformat()
    rows = [r for r in merged.values()
            if (r.get("meeting_date") or r.get("disclosed_date") or "9999") >= cutoff]

    # 開示が古い順に並べる(地図用データを作るとき、訂正版など新しい開示が優先されるように)
    rows.sort(key=lambda r: (r.get("disclosed_date", ""), r.get("disclosed_time", "")))

    fields = list(new_fields)
    fields += [k for k in old_fields if k not in fields]
    Path(args.all).parent.mkdir(parents=True, exist_ok=True)
    with open(args.all, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"統合完了: 今回 {len(new_rows)} 件(新規 {added} 件) → 蓄積 {len(rows)} 件")


if __name__ == "__main__":
    main()
