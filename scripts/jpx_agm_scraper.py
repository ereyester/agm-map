#!/usr/bin/env python3
"""
東証上場会社情報サービス(JPX)から、定時株主総会などの招集通知PDFを取得し、
開催日時・会場名・住所を抽出してCSVに保存するスクリプト。

招集通知そのものは「縦覧書類」で、TDnetの適時開示一覧には載らない。
そこで上場会社情報サービスの詳細検索で、株主総会が近い会社を一覧にし、
各社の縦覧書類から招集通知を取る。
(検索画面の項目名は「電子提供措置開始日」だが、実際には各社の
 「株主総会開催日(予定)」で絞り込まれる。2026年11月に確認)

使い方:
    pip install requests beautifulsoup4 pdfplumber
    python scripts/jpx_agm_scraper.py --out data/jpx_week.csv --known data/agm_all.csv

出力の列は tdnet_agm_scraper.py と同じなので、merge_agm.py でそのまま統合できる。
"""

import argparse
import csv
import datetime as dt
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# 日時・会場の抽出処理は TDnet 用スクリプトと共通
from tdnet_agm_scraper import CSV_COLUMNS, extract_meeting_info, extract_text

BASE = "https://www2.jpx.co.jp"
SEARCH_URL = BASE + "/tseHpFront/JJK010010Action.do"
HEADERS = {"User-Agent": "Mozilla/5.0 (AGM-map research script)"}
MARKETS = ("011", "012", "013")  # プライム・スタンダード・グロース(TOKYO PRO Market は除外)
PAGE_SIZE = 200

# 縦覧書類のうち「株主総会招集通知/株主総会資料」の表の行には id="1105_連番" が付いている
NOTICE_ROW_ID = re.compile(r"^1105_\d+$")
# 英訳版(「NOTICE OF CONVOCATION」など)は対象外
ENGLISH_TITLE = re.compile(r"notice|convocation|english|英文|英訳", re.IGNORECASE)
# 一度に試す書類の数の上限(本体以外の資料しか読めない会社で何度もダウンロードしないため)
MAX_TRIES = 3
STATUS_RANK = {"ok": 0, "online_only": 1, "partial": 2, "not_found": 3}


def form_fields(html, form_name):
    """フォームの hidden / select の初期値を集める(チェックボックスは除く)"""
    form = BeautifulSoup(html, "html.parser").find("form", attrs={"name": form_name})
    data = {}
    for el in form.find_all(["input", "select"]):
        name = el.get("name")
        if not name or el.get("type") in ("button", "checkbox"):
            continue
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            data[name] = opt.get("value") if opt else ""
        else:
            data[name] = el.get("value") or ""
    return form.get("action"), data


def parse_company_list(html):
    """検索結果の表から (コード5桁, 銘柄名) のリストを返す"""
    soup = BeautifulSoup(html, "html.parser")
    companies = []
    for tr in soup.find_all("tr"):
        code = tr.find("input", attrs={"name": re.compile(r"\.eqMgrCd$")})
        name = tr.find("input", attrs={"name": re.compile(r"\.eqMgrNm$")})
        if code and name:
            companies.append((code["value"], name["value"]))
    total = re.search(r"／\s*([\d,]+)\s*件中", soup.get_text(" ", strip=True))
    return companies, int(total.group(1).replace(",", "")) if total else len(companies)


def search_companies(session, start, end, sleep):
    """株主総会開催日(予定)が start〜end の会社をすべて取得する"""
    session.get(SEARCH_URL, params={"Show": "Show"}, timeout=30)
    time.sleep(sleep)
    resp = session.post(SEARCH_URL, data={"Switch": "Switch"}, timeout=30)  # 詳細検索に切り替え
    time.sleep(sleep)
    action, data = form_fields(resp.text, "JJK010020Form")
    data.pop("Show", None)
    data.pop("Switch", None)
    data.update({
        "dspSsuPd": str(PAGE_SIZE),
        "eqnsskKaisiDayFrmYearPd": f"{start.year}", "eqnsskKaisiDayFrmTskPd": f"{start.month:02d}",
        "eqnsskKaisiDayFrmDayPd": f"{start.day:02d}",
        "eqnsskKaisiDayToYearPd": f"{end.year}", "eqnsskKaisiDayToTskPd": f"{end.month:02d}",
        "eqnsskKaisiDayToDayPd": f"{end.day:02d}",
    })
    resp = session.post(BASE + action, data=list(data.items()) + [("szkbuChkbx", m) for m in MARKETS],
                        timeout=60)
    time.sleep(sleep)
    resp.raise_for_status()
    companies, total = parse_company_list(resp.text)

    page = 1
    while len(companies) < total:  # 200件を超える分はページ送り
        page += 1
        action, data = form_fields(resp.text, "JJK010030Form")
        for k in ("Show", "Return", "BaseJh", "Sort"):
            data.pop(k, None)
        data["lstDspPg"] = str(page)
        resp = session.post(BASE + action, data=data, timeout=60)
        time.sleep(sleep)
        resp.raise_for_status()
        more, _ = parse_company_list(resp.text)
        if not more:
            break
        companies.extend(more)
    return companies, total


def parse_date(s):
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", s or "")
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def fetch_detail(session, code, sleep):
    """会社の詳細ページから (総会開催日(予定), 招集通知の一覧[(縦覧日, 表題, PDFのURL)]) を返す"""
    resp = session.post(BASE + "/tseHpFront/JJK010030Action.do",
                        data={"BaseJh": "BaseJh", "mgrCd": code, "jjHisiFlg": "1", "sniMtGmnId": "JJK010030"},
                        timeout=60)
    time.sleep(sleep)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    planned = None
    label = soup.find(string=re.compile("株主総会開催日"))
    if label:
        head = label.find_parent("tr")
        values = head.find_next_sibling("tr") if head else None
        if values:
            cells = [c.get_text(strip=True) for c in head.find_all(["th", "td"])]
            vals = [c.get_text(strip=True) for c in values.find_all(["th", "td"])]
            for c, v in zip(cells, vals):
                if "株主総会開催日" in c:
                    planned = parse_date(v)

    notices = []
    for tr in soup.find_all("tr", id=NOTICE_ROW_ID):
        tds = tr.find_all("td")
        link = tr.find("a", href=True)
        if len(tds) < 2 or not link:
            continue
        notices.append((parse_date(tds[0].get_text(strip=True)), link.get_text(strip=True),
                        BASE + link["href"]))
    return planned, notices


def rank_notices(notices, since):
    """since 以降に縦覧された招集通知を、本体らしい順に並べる
    1. 「招集(ご)通知」を表題に含むもの  2. 「株主総会資料」「電子提供措置事項」など  3. 訂正
    同じ順位なら新しいものを先に"""
    def priority(n):
        title = n[1]
        if "訂正" in title:
            return 2
        return 0 if re.search(r"招集(?:ご)?通知", title) else 1
    recent = [n for n in notices if n[0] and n[0] >= since and not ENGLISH_TITLE.search(n[1])]
    return sorted(recent, key=lambda n: (priority(n), -n[0].toordinal()))


def download_pdf(session, url, pdf_dir, sleep):
    path = pdf_dir / ("jpx_" + url.rsplit("/", 1)[-1])
    if path.exists() and path.stat().st_size > 0:
        return path  # キャッシュ済み
    resp = session.get(url, timeout=120)
    time.sleep(sleep)
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def known_codes(path, since):
    """すでに招集通知を取得済みの会社コード(毎週同じ会社を取り直さないため)"""
    p = Path(path) if path else None
    if not p or not p.exists():
        return set()
    with p.open(encoding="utf-8-sig", newline="") as f:
        return {r["code"] for r in csv.DictReader(f)
                if "jpx.co.jp/disc/" in r.get("pdf_url", "")
                and r.get("status") in ("ok", "online_only")
                and (r.get("disclosed_date") or "") >= since.isoformat()}


def main():
    today = dt.date.today()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--back-days", type=int, default=3,
                    help="総会開催日(予定)が今日から何日前までの会社を対象にするか(既定3日)")
    # 招集通知は総会の3週間前までに出るので、35日先まで見れば毎週の実行で取りこぼさない
    ap.add_argument("--ahead-days", type=int, default=35,
                    help="総会開催日(予定)が今日から何日後までの会社を対象にするか(既定35日)")
    ap.add_argument("--out", default="jpx_notices.csv")
    ap.add_argument("--pdf-dir", default="pdf_cache")
    ap.add_argument("--known", default="", help="取得済みデータのCSV(ここで取得済みの会社は飛ばす)")
    ap.add_argument("--sleep", type=float, default=2.0, help="リクエスト間隔(秒)")
    ap.add_argument("--limit", type=int, default=0, help="処理社数の上限(テスト用、0で無制限)")
    args = ap.parse_args()

    start = today - dt.timedelta(days=args.back_days)
    end = today + dt.timedelta(days=args.ahead_days)
    since = today - dt.timedelta(days=60)  # 招集通知の縦覧日はこれ以降のもの(=今回の総会の分)を採用

    pdf_dir = Path(args.pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    companies, total = search_companies(session, start, end, args.sleep)
    skip = known_codes(args.known, since)
    targets = [(c, n) for c, n in companies if c[:4] not in skip]
    print(f"総会開催日(予定) {start}〜{end}: {total} 社(取得済み {len(companies) - len(targets)} 社を除き "
          f"{len(targets)} 社を確認)")
    if args.limit:
        targets = targets[: args.limit]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    found = 0
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for i, (code5, name) in enumerate(targets, 1):
            code = code5[:4]
            try:
                planned, notices = fetch_detail(session, code5, args.sleep)
                ranked = rank_notices(notices, since)
                if not ranked:
                    print(f"[{i}/{len(targets)}] {code} {name} -> 招集通知なし(まだ縦覧されていない)")
                    continue
                # 本体らしい書類から順に読み、日時・会場が取れたところで止める
                record = None
                for disclosed, title, url in ranked[:MAX_TRIES]:
                    cand = {
                        "disclosed_date": disclosed.isoformat(), "disclosed_time": "", "code": code,
                        "company": name, "title": title, "pdf_url": url, "is_correction": "訂正" in title,
                    }
                    try:
                        cand.update(extract_meeting_info(extract_text(download_pdf(session, url, pdf_dir, args.sleep))))
                    except requests.RequestException:
                        raise
                    except Exception as e:  # PDFが読めない(画像だけ等)場合は次の書類へ
                        print(f"[warn] {code} {name}: PDF読み取り失敗 {e}", file=sys.stderr)
                        cand["status"] = f"error: {type(e).__name__}"
                    if record is None or STATUS_RANK.get(cand["status"], 9) < STATUS_RANK.get(record["status"], 9):
                        record = cand
                    if record["status"] in ("ok", "online_only"):
                        break
                if not record.get("meeting_date") and planned:
                    record["meeting_date"] = planned.isoformat()  # PDFから読めなければ会社情報の予定日
            except requests.RequestException as e:  # 1社の失敗で全体を止めない
                print(f"[warn] {code} {name}: {e}", file=sys.stderr)
                continue
            writer.writerow(record)
            found += 1
            print(f"[{i}/{len(targets)}] {code} {name} -> {record.get('status')}")

    print(f"\n完了: {out_path} に {found} 件を書き出しました")


if __name__ == "__main__":
    main()
