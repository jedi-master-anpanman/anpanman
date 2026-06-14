#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ホテルオークラ神戸 アンパンマンスイートルーム 空き監視スクリプト（カレンダー一括版）
-------------------------------------------------------------------
tripla の在庫カレンダーAPI(inventory)を使い、今日から指定日数先までの
空き状況を月ごとにまとめて取得します。
前回からの差分を見て「新しく空きが出た日」だけを ntfy で通知します。
（すでに通知済みで空きが続いている日は、繰り返し通知しません）

※空きの確認と通知だけを行います。予約確定・決済は手動で行ってください。
※サーバに負荷をかけないよう、実行間隔は数分おき程度にしてください。
"""

import os
import sys
import json
import datetime
import requests

# ============================================================
# 設定（ここを編集してください）
# ============================================================
RANGE_DAYS = 150      # 今日から何日先まで監視するか（予約は150日前から開放）
MIN_LEAD_DAYS = 0     # 今日から何日未満の近すぎる日を対象外にするか
                      #   5日前締切による誤検知（6/19のような例）を避けたいなら 6 程度に
ADULTS = 2
CHILDREN = 0
NIGHTS = 1

# 状態保存ファイル（前回の空き状況を覚えて繰り返し通知を防ぐ）。実行フォルダに作られます。
STATE_FILE = os.environ.get("STATE_FILE", "okura_state.json")

# ntfy 設定（環境変数で渡す）
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# tripla のセッションCookie（基本は不要。401/403で弾かれる場合のみ設定）
TRIPLA_COOKIE = os.environ.get("TRIPLA_COOKIE", "")

# ============================================================
# 固定値（基本そのままでOK）
# ============================================================
HOTEL_ID = "5929"
PLAN_CODE = "14061049"
INVENTORY_URL = f"https://book.okura.com/api/book/hotels/{HOTEL_ID}/rooms/inventory"
JST = datetime.timezone(datetime.timedelta(hours=9))

HEADERS = {
    "accept": "*/*",
    "accept-language": "ja",
    "app-version": "tripla-booking-widget/1.0",
    "tripla-locale": "ja",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    ),
    "x-request-origin": "https://book.okura.com",
    "x-site-controller": "tl_lincon",
    "referer": "https://book.okura.com/booking/result",
}


def month_ranges(start_date, end_date):
    """start_date～end_date を月ごとに区切って (year_month, 月初, 月末) を返す。"""
    ranges = []
    y, m = start_date.year, start_date.month
    while (y < end_date.year) or (y == end_date.year and m <= end_date.month):
        first = datetime.date(y, m, 1)
        nxt = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
        last = nxt - datetime.timedelta(days=1)
        ranges.append((f"{y:04d}-{m:02d}", first, last))
        y, m = nxt.year, nxt.month
    return ranges


def fetch_month(year_month, first, last):
    """1ヶ月分の在庫カレンダーを取得し、{日付文字列: 残室数(int)} を返す。"""
    params = {
        "cache": "true",
        "start_date": first.strftime("%Y-%m-%d"),
        "end_date": last.strftime("%Y-%m-%d"),
        "nights": NIGHTS,
        "rooms[][adults]": ADULTS,
        "rooms[][children]": CHILDREN,
        "rooms[][hotel_plan_code]": PLAN_CODE,
        "year_month": year_month,
    }
    headers = dict(HEADERS)
    if TRIPLA_COOKIE:
        headers["cookie"] = TRIPLA_COOKIE

    resp = requests.get(INVENTORY_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    out = {}
    for date_str, count in (data.get("inventory") or {}).items():
        try:
            n = int(count)
        except (TypeError, ValueError):
            n = 0
        if n >= 1:
            out[date_str] = n
    return out


def get_available_dates():
    """今日(JST)を基準に、監視範囲内で空きのある {日付文字列: 残室数} を返す。"""
    today = datetime.datetime.now(JST).date()
    win_start = today + datetime.timedelta(days=MIN_LEAD_DAYS)
    win_end = today + datetime.timedelta(days=RANGE_DAYS)

    available = {}
    for year_month, first, last in month_ranges(win_start, win_end):
        try:
            month_av = fetch_month(year_month, first, last)
        except Exception as e:
            print(f"[!] {year_month} の取得でエラー: {e}", file=sys.stderr)
            continue
        for date_str, n in month_av.items():
            try:
                d = datetime.datetime.strptime(date_str, "%Y/%m/%d").date()
            except ValueError:
                continue
            if win_start <= d <= win_end:
                available[date_str] = n
    return available


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_state(dates):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(dates), f, ensure_ascii=False)
    except Exception as e:
        print(f"[!] 状態の保存に失敗: {e}", file=sys.stderr)


def booking_link(date_str):
    """その日付の予約結果ページURLを組み立てる。"""
    co = (datetime.datetime.strptime(date_str, "%Y/%m/%d").date()
          + datetime.timedelta(days=NIGHTS)).strftime("%Y/%m/%d")
    return (
        f"https://book.okura.com/booking/result?is_including_occupied=true"
        f"&hotel_plan_code={PLAN_CODE}&checkin={date_str}&checkout={co}"
        f"&type=plan&order=price_low_to_high&is_day_use=false"
        f"&hotel_plan_codes={PLAN_CODE}"
        f"&rooms=[{{%22adults%22:{ADULTS},%22children%22:{CHILDREN}}}]"
    )


def notify(new_dates, available):
    """新しく空いた日付（複数可）をまとめて1通の通知にして送る。"""
    if not NTFY_TOPIC:
        print("[!] NTFY_TOPIC が未設定のため通知をスキップしました。")
        return

    lines = ["アンパンマンスイートに空きが出ました！", ""]
    for d in sorted(new_dates):
        lines.append(f"・{d}（残{available.get(d, '?')}室）")
        lines.append(f"  {booking_link(d)}")
    body = "\n".join(lines)

    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    ntfy_headers = {
        "Title": "Okura Anpanman - OPEN",
        "Priority": "urgent",
        "Tags": "rotating_light",
        # タップ時は最も早い日付の予約ページを開く
        "Click": booking_link(sorted(new_dates)[0]),
    }
    try:
        resp = requests.post(url, data=body.encode("utf-8"),
                            headers=ntfy_headers, timeout=30)
        if resp.status_code == 200:
            print(f"[+] 通知を送信しました（新規: {sorted(new_dates)}）")
        else:
            print(f"[!] 通知の送信に失敗: HTTP {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[!] 通知の送信でエラー: {e}")


def main():
    now = datetime.datetime.now(JST)
    available = get_available_dates()
    current = set(available.keys())
    print(f"[{now:%Y-%m-%d %H:%M}] 監視範囲内の空き: {len(current)}日 -> {sorted(current)}")

    previous = load_state()
    new_dates = current - previous

    if new_dates:
        print(f"[+] 新しく空いた日: {sorted(new_dates)}")
        notify(new_dates, available)
    else:
        print("[-] 新しい空きはありません（繰り返し通知はしません）。")

    save_state(current)
    sys.exit(10 if new_dates else 0)


if __name__ == "__main__":
    main()
