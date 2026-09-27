#!/usr/bin/env python3
"""
TDnet(適時開示情報閲覧サービス)から株主総会招集通知のPDFを取得し、
開催日時・会場名・住所を抽出してCSVに保存するスクリプト。

使い方:
    pip install requests beautifulsoup4 pdfplumber
    python tdnet_agm_scraper.py --start 2026-05-20 --end 2026-06-20 --out agm.csv

注意:
    - TDnetの公開ページで見られるのは直近31日分のみです。
    - サーバーに負担をかけないよう、リクエスト間隔(--sleep)は短くしすぎないでください。
    - 利用前にJPXのサイト利用規約を確認してください。
    - 抽出は正規表現ベースなので100%正確ではありません。status列が "ok" 以外の行は目視確認を推奨します。
"""

import argparse
import csv
import datetime as dt
import re
import sys
import time
import unicodedata
from pathlib import Path

import pdfplumber
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.release.tdnet.info/inbs/"
LIST_URL = BASE_URL + "I_list_{page:03d}_{date}.html"
HEADERS = {"User-Agent": "Mozilla/5.0 (AGM-map research script)"}

# 対象にするタイトル
#  (a) 招集通知そのもの
TITLE_NOTICE = ("招集通知", "招集ご通知", "電子提供措置", "株主総会資料")
#  (b) 「臨時株主総会の開催(日時・場所)に関するお知らせ」系。本文に日時・場所が書かれていることが多い
TITLE_OPEN = ("開催", "日時", "場所")
# 総会が終わった後の報告や、会場が書かれない開示は除外
TITLE_EXCLUDES = (
    "決議結果", "不成立", "取下げ", "取り下げ", "招集請求", "招集許可", "質疑", "議決権行使",
    "承認可決", "決議省略", "取締役会", "英文", "English", "Notice",
)

CSV_COLUMNS = [
    "disclosed_date", "disclosed_time", "code", "company", "title", "pdf_url",
    "meeting_date", "meeting_time", "venue_name", "address",
    "online_only", "is_correction", "status", "raw_datetime", "raw_place",
]


# ---------------------------------------------------------------------------
# 1. TDnetの一覧ページから招集通知を探す
# ---------------------------------------------------------------------------
def fetch_list_for_date(session, date, sleep):
    """指定日の開示一覧(複数ページ)を取得して行のリストを返す"""
    rows = []
    date_str = date.strftime("%Y%m%d")
    for page in range(1, 100):
        url = LIST_URL.format(page=page, date=date_str)
        resp = session.get(url, timeout=30)
        time.sleep(sleep)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        page_rows = parse_list_html(resp.text, date)
        if not page_rows:
            break
        rows.extend(page_rows)
    return rows


def parse_list_html(html, date):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for tr in soup.find_all("tr"):
        title_td = tr.find("td", class_=re.compile("kjTitle"))
        if not title_td:
            continue
        link = title_td.find("a")
        if not link or not link.get("href"):
            continue

        def cell(cls):
            td = tr.find("td", class_=re.compile(cls))
            return td.get_text(strip=True) if td else ""

        results.append({
            "disclosed_date": date.isoformat(),
            "disclosed_time": cell("kjTime"),
            "code": cell("kjCode")[:4],  # 5桁表示(末尾0)を4桁に
            "company": cell("kjName"),
            "title": title_td.get_text(strip=True),
            "pdf_url": BASE_URL + link["href"],
        })
    return results


def is_pro_market(company):
    """TDnetではTOKYO PRO Market銘柄の社名に「Ｐ－」が付く(グロースは「Ｇ－」)"""
    return company.startswith(("Ｐ－", "P-", "Ｐ-", "P－"))


def is_agm_notice(title):
    if any(e in title for e in TITLE_EXCLUDES):
        return False
    if any(k in title for k in TITLE_NOTICE):
        return True
    return "総会" in title and any(k in title for k in TITLE_OPEN)


# ---------------------------------------------------------------------------
# 2. PDFのダウンロードとテキスト抽出
# ---------------------------------------------------------------------------
def download_pdf(session, url, pdf_dir, sleep):
    path = pdf_dir / url.rsplit("/", 1)[-1]
    if path.exists() and path.stat().st_size > 0:
        return path  # キャッシュ済み
    resp = session.get(url, timeout=60)
    time.sleep(sleep)
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def extract_text(pdf_path, max_pages=4):
    """招集通知の日時・場所は冒頭数ページにあるので先頭だけ読む"""
    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:max_pages]:
            texts.append(page.extract_text() or "")
    return normalize("\n".join(texts))


def normalize(text):
    text = unicodedata.normalize("NFKC", text)
    # 「日 時」「場 所」のような字間スペースを詰める
    text = re.sub(r"(?<=[\u3040-\u30ff\u4e00-\u9fff])[ \u3000]+(?=[\u3040-\u30ff\u4e00-\u9fff])", "", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    # 「10- 32」「13番 14号」「四丁目 13番」のような住所内のスペースを詰める
    text = re.sub(r"(\d)\s*([-‐−ー])\s*(\d)", r"\1-\3", text)
    text = re.sub(r"(丁目|番地?|号)\s+(?=[\d一二三四五六七八九十])", r"\1", text)
    text = re.sub(r"(\d)\s+(?=[番号丁])", r"\1", text)
    return text


# ---------------------------------------------------------------------------
# 3. 日時・場所の抽出
# ---------------------------------------------------------------------------
# 「1. 日時」「1 日時」「(1) 開催日時」などの見出しの後ろを取る
# 見出しとして書かれた「日時」だけを対象にする(本文中の「開催日及び付議議案を決定」などは無視)
DATETIME_LABEL = re.compile(
    r"^\s*(?:\d+[.\s]|[((]\d+[))]|[一二三四五六七八九十]+[、.]|[IVX]{1,4}[.\s])?\s*(?:\S{0,12}?の)?"
    r"(?:開催)?(?:予定)?(?:日時|開催日|日程)(?:及び場所|および場所|・場所)?\s*[::]?\s*(.*)"
)
# 行頭の見出し(「2. 場所」「(2) 開催場所」など)だけを対象にする
PLACE_LABEL = re.compile(
    # 「2. 場所」「(2) 開催場所」「2. 臨時株主総会の開催場所」「会場」などに対応
    r"^\s*(?:\d+[.\s]|[((]\d+[))]|[一二三四五六七八九十]+[、.])?\s*(?:\S{0,12}?の)?(?:開催)?(?:場所|会場)\s*[::]?\s*(.*)"
)
# 巻末の「会場ご案内図」などは見出しとして扱わない
PLACE_NOT_LABEL = re.compile(r"^(ご案内|案内図|へのアクセス|のご案内|周辺|地図)")
# 次の項目の見出し(ここで場所の取得を止める)
NEXT_SECTION = re.compile(
    r"^\s*(?:\d+[.\s]|[((]\d+[))]|[一二三四五六七八九十]+[、.]|[IVX]{1,4}[.\s]|【)?\s*(?:\S{0,15}?の)?"
    r"(目的事項|会議の目的|報告事項|決議事項|付議議案|議案|基準日|招集にあたって|議決権|その他|受付|交通|最寄|日時|開催日)"
)
# 「2026 年 10 月 28 日」のようにスペースが入っても拾う
DATE_RE = re.compile(r"(令和|20)\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 「午前10時」「10時30分」「10:00」のどれにも対応
TIME_RE = re.compile(r"(午前|午後)?\s*(\d{1,2})\s*(?:時\s*(\d{1,2}\s*分|半)?|:(\d{2}))")
PREFECTURES = (
    "北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|"
    "東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|"
    "滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|"
    "香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県"
)
ADDRESS_RE = re.compile(
    rf"((?:{PREFECTURES})?[^\s、,()()]{{1,10}}?[市区町村郡][^\s()()]*?\d+(?:[-−ー丁目番地号の]+\d*)*(?:番地?|号)?)"
)
ONLINE_ONLY_RE = re.compile(r"バーチャルオンリー|場所の定めのない")


def first_meeting_date(s):
    """「7月31日付」のような開示日や、基準日の日付を除いた最初の日付"""
    for dm in DATE_RE.finditer(s):
        after = s[dm.end():dm.end() + 8]
        before = s[max(0, dm.start() - 8):dm.start()]
        if (after.startswith("付")
                or re.search(r"基準日\s*(?:は|を|:)?\s*$", before)       # 「基準日 2026年…」
                or re.match(r"\s*(?:\(.{0,5}\))?\s*を?基準日", after)):  # 「2026年…を基準日」
            continue
        return dm
    return None


def parse_datetime(text):
    raw = ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = DATETIME_LABEL.match(line)
        if not m:
            continue
        # 見出しと日付が別の行に分かれている場合は次の2行まで見る
        parts = [m.group(1)]
        for nxt in lines[i + 1:i + 3]:
            # 次の項目(「(2)開催場所」など)に入ったら止める。そこの数字を時刻と誤認しないため
            if PLACE_LABEL.match(nxt) or NEXT_SECTION.search(nxt):
                break
            parts.append(nxt)
        cand = " ".join(parts)
        dm0 = first_meeting_date(cand)
        if dm0 and dm0.start() < 40:
            raw = cand[dm0.start():dm0.start() + 60].strip()
            break
    if not raw:  # 見出しが見つからない場合は本文中の最初の「日付+時刻」
        for m in re.finditer(DATE_RE.pattern + r"(?!\s*付).{0,20}?" + TIME_RE.pattern, text):
            if not re.search(r"基準日|付で|開示", text[max(0, m.start() - 15):m.start()]):
                raw = m.group(0)
                break
    if not raw:
        return "", "", ""

    date_str = ""
    dm = DATE_RE.search(raw)
    if dm:
        era, y, mo, d = dm.groups()
        year = 2018 + int(y) if era == "令和" else 2000 + int(y)
        try:
            date_str = dt.date(year, int(mo), int(d)).isoformat()
        except ValueError:
            pass

    time_str = ""
    after_date = raw[dm.end():] if dm else raw
    # 次の項目(開催場所など)が同じ行に続いている場合はその手前まで
    after_date = re.split(r"[((]?\d[))]?\.?\s*(?:開催)?(?:場所|会場)|\d\.\s*\S{0,12}?場所", after_date)[0]
    tm = TIME_RE.search(after_date)
    if not tm and "正午" in after_date:
        time_str = "12:00"
    if tm:
        ampm, h, mi, colon_mi = tm.groups()
        hour = int(h)
        if ampm == "午後" and hour < 12:
            hour += 12
        if colon_mi:
            minute = int(colon_mi)
        elif mi == "半":
            minute = 30
        else:
            minute = int(re.sub(r"\D", "", mi)) if mi else 0
        if 0 <= hour < 24 and 0 <= minute < 60:
            time_str = f"{hour:02d}:{minute:02d}"
    return date_str, time_str, raw


def parse_place(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = PLACE_LABEL.match(line)
        if not m or PLACE_NOT_LABEL.match(m.group(1).strip()):
            continue
        chunk = [m.group(1).strip()] if m.group(1).strip() else []
        for nxt in lines[i + 1:i + 5]:
            if NEXT_SECTION.search(nxt) or DATETIME_LABEL.match(nxt.strip()):
                break
            if nxt.strip():
                chunk.append(nxt.strip())
        raw = " ".join(chunk).strip()
        # 長すぎる・文章になっているものは会場情報ではない(本文を誤って拾った)と判断
        if raw and len(raw) <= 150 and not re.search(r"(ます|です|予定|次第)[。.]", raw):
            return raw
    return ""


def place_from_datetime_block(text):
    """「日時及び場所」のように日時と場所が1つの項目にまとまっている場合の予備処理"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"日時(?:及び|および|・)場所", line):
            parts = [line.strip()]
            for nxt in lines[i + 1:i + 5]:
                if NEXT_SECTION.search(nxt):
                    break
                parts.append(nxt.strip())
            block = " ".join(parts)
            am = ADDRESS_RE.search(block)
            if am:
                return block[am.start():am.start() + 100].strip()
    return ""


def place_near_datetime(text):
    """最後の手段:日時の見出しの後ろ8行以内にある最初の住所を会場とみなす"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if DATETIME_LABEL.search(line):
            for j in range(i, min(i + 8, len(lines))):
                am = ADDRESS_RE.search(lines[j])
                if am and not re.search(r"本店所在地|所在地|住所", lines[j][:am.start()]):
                    parts = [lines[j][am.start():].strip()]
                    for nxt in lines[j + 1:j + 3]:
                        if NEXT_SECTION.search(nxt):
                            break
                        parts.append(nxt.strip())
                    return " ".join(parts)[:120]
            break
    return ""


def split_venue_address(raw_place):
    """生テキストを会場名と住所に分ける"""
    if not raw_place:
        return "", ""
    am = ADDRESS_RE.search(raw_place)
    address = am.group(1) if am else ""
    venue = raw_place.replace(address, "") if address else raw_place
    # 住所の頭に見出し(「開催場所」など)が付いてしまった場合は外す
    address = re.sub(r"^.*?(?:場所|会場)\s*[::]?\s*", "", address)
    # 注記(「末尾の会場ご案内図をご参照ください」など)を落とす
    venue = re.sub(r"[((※【].*$", "", venue)
    venue = re.sub(
        r"\s*(?:\d{1,2}|[IVX]{1,4})\s*[.]?\s*(?:本?臨時|本?定時|本?株主総会|付議|決議|議案|監査役|基準日).*$",
        "", venue)
    venue = re.sub(r"\s+", " ", venue).strip(" 、,、")
    return venue, address


def extract_meeting_info(text):
    date_str, time_str, raw_dt = parse_datetime(text)
    raw_place = parse_place(text) or place_from_datetime_block(text) or place_near_datetime(text)
    venue, address = split_venue_address(raw_place)
    online_only = bool(ONLINE_ONLY_RE.search(text))

    if online_only and not address:
        venue = ""  # 「場所の定めのない〜」を会場名として拾わない
        status = "online_only"
    elif date_str and venue and address:
        status = "ok"
    elif date_str or venue:
        status = "partial"
    else:
        status = "not_found"

    return {
        "meeting_date": date_str,
        "meeting_time": time_str,
        "venue_name": venue,
        "address": address,
        "online_only": online_only,
        "status": status,
        "raw_datetime": raw_dt,
        "raw_place": raw_place,
    }


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------
def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def main():
    today = dt.date.today()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=dt.date.fromisoformat, default=today - dt.timedelta(days=30))
    ap.add_argument("--end", type=dt.date.fromisoformat, default=today)
    ap.add_argument("--out", default="agm_notices.csv")
    ap.add_argument("--pdf-dir", default="pdf_cache")
    ap.add_argument("--sleep", type=float, default=2.0, help="リクエスト間隔(秒)")
    ap.add_argument("--show-skipped", action="store_true", help="「総会」を含むが対象外にしたタイトルを表示")
    ap.add_argument("--include-pro", action="store_true",
                    help="TOKYO PRO Market(社名が「Ｐ－」で始まる銘柄)も含める。既定では除外")
    ap.add_argument("--limit", type=int, default=0, help="処理件数の上限(テスト用、0で無制限)")
    args = ap.parse_args()

    pdf_dir = Path(args.pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. 一覧から招集通知を集める
    notices = []
    for date in daterange(args.start, args.end):
        try:
            rows = fetch_list_for_date(session, date, args.sleep)
        except requests.RequestException as e:
            print(f"[warn] {date} の一覧取得に失敗: {e}", file=sys.stderr)
            continue
        hits = [r for r in rows if is_agm_notice(r["title"])
                and (args.include_pro or not is_pro_market(r["company"]))]
        print(f"{date}: 開示 {len(rows)} 件 / 招集通知 {len(hits)} 件")
        if args.show_skipped:  # 「総会」を含むのに対象外にしたタイトルを表示(フィルタ確認用)
            for r in rows:
                if "総会" in r["title"] and r not in hits:
                    print(f"    [対象外] {r['code']} {r['company']}: {r['title']}")
        notices.extend(hits)

    if args.limit:
        notices = notices[: args.limit]

    # 2-3. PDFを取得して情報を抽出
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)  # 初回は data/ フォルダがないため
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:  # Excelで文字化けしないようBOM付き
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for n, row in enumerate(notices, 1):
            record = dict(row, is_correction="訂正" in row["title"])
            try:
                pdf_path = download_pdf(session, row["pdf_url"], pdf_dir, args.sleep)
                text = extract_text(pdf_path)
                record.update(extract_meeting_info(text))
            except Exception as e:  # 1件の失敗で全体を止めない
                print(f"[warn] {row['code']} {row['company']}: {e}", file=sys.stderr)
                record["status"] = f"error: {type(e).__name__}"
            writer.writerow(record)
            print(f"[{n}/{len(notices)}] {row['code']} {row['company']} -> {record.get('status')}")

    print(f"\n完了: {out_path} に {len(notices)} 件を書き出しました")


if __name__ == "__main__":
    main()
