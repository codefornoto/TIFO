#!/usr/bin/env python3
"""北陸3県の統合アンケートCSVをTIFO向けの軽量集計CSVへ変換する（v2）。

標準ではGitHub上の output_merge/merged_survey_YYYY.csv を自動検出する。
ローカル検証時は --input-dir で同形式のCSVを置いたディレクトリを指定できる。
外部ライブラリは使用しないため、GitHub ActionsのPythonだけで実行できる。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import tempfile
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]

AGE_BANDS = ["10歳未満", "10代", "20代", "30代", "40代", "50代", "60代", "70代", "80代", "90代", "100代"]
GENDERS = ["男性", "女性", "その他/無回答"]
STAYS = ["日帰り", "1泊", "2泊", "3泊", "4泊以上"]
COMPANIONS = [
    "自分ひとり", "恋人", "友人", "夫婦2人", "団体旅行", "親戚", "親", "職場の同僚",
    "小学生以下連れの家族", "中学生以下連れの家族", "高校生連れの家族", "災害支援",
]
PURPOSES = [
    "宿でのんびり過ごす", "温泉や露天風呂", "地元の美味しいものを食べる",
    "花見や紅葉などの自然鑑賞", "名所、旧跡の観光",
    "テーマパーク（遊園地、動物園、博物館など）", "買い物、アウトレット",
    "お祭りやイベントへの参加・見物", "スポーツ観戦や芸能鑑賞（コンサート等）",
    "アウトドア（海水浴、釣り、登山など）", "まちあるき、都市散策",
    "各種体験（手作り、果物狩りなど）", "スキー・スノボ、マリンスポーツ",
    "その他スポーツ（ゴルフ、テニスなど）", "ドライブ・ツーリング",
    "友人・親戚を尋ねる", "出張など仕事関係", "その他の目的",
]
SOURCES = [
    "Facebook", "Google", "Googleマップ", "Instagram", "TikTok", "X（旧Twitter）",
    "YouTube", "SNS広告", "ブログ", "まとめサイト", "インターネット・アプリ",
    "デジタルニュース", "宿泊予約Webサイト", "宿泊施設", "TV・ラジオ番組やCM",
    "ラブライブのスタンプラリー", "新聞・雑誌・ガイドブック", "旅行会社",
    "友人・知人", "地元の人", "観光パンフレット・ポスター", "観光案内所",
    "観光展・物産展", "観光連盟やDMOのHP", "その他",
]
INCOMES = ["300万円未満", "300～599万円", "600～999万円", "1,000～1,999万円", "2,000万円以上", "分からない/無回答"]

DIMENSIONS = [
    {"key": "age", "label": "年代", "columns": AGE_BANDS, "multi": False},
    {"key": "gender", "label": "性別", "columns": GENDERS, "multi": False},
    {"key": "origin", "label": "居住都道府県", "columns": PREFECTURES, "multi": False},
    {"key": "stay", "label": "宿泊数", "columns": STAYS, "multi": False},
    {"key": "companion", "label": "同伴者", "columns": COMPANIONS, "multi": True},
    {"key": "purpose", "label": "旅の目的", "columns": PURPOSES, "multi": True},
    {"key": "source", "label": "情報源", "columns": SOURCES, "multi": True},
    {"key": "income", "label": "世帯年収（比較用区分）", "columns": INCOMES, "multi": False},
]

CORE_COLUMNS = [
    "回答数", "旅行全体満足度合計", "旅行全体満足度回答数",
    "商品サービス満足度合計", "商品サービス満足度回答数",
    "推奨度合計", "推奨度回答数", "推奨者", "中立者", "非推奨者",
]
DIMENSION_COLUMNS = [column for dimension in DIMENSIONS for column in dimension["columns"]]
DAILY_COLUMNS = ["日付", "TIF", "回答場所", *CORE_COLUMNS, *DIMENSION_COLUMNS]
SNAPSHOT_COLUMNS = ["TIF", "回答場所", *CORE_COLUMNS, *DIMENSION_COLUMNS]
DATA_MANIFEST_FILE = "tifo_data_manifest.json"
SNAPSHOT_FILE = "tifo_summary_all.csv"
MONTHLY_FILE = "tifo_trend_monthly.csv"
ROUTES_ALL_FILE = "tifo_routes_all.csv"

REQUIRED_COLUMNS = {"対象県（富山/石川/福井）", "アンケート回答日", "回答場所"}
INVALID_PLACE_NAMES = {"なし", "無し", "該当なし", "該当施設なし", "選択なし", "選択無し", "選択されていません", "特になし", "特に無し", "未定", "不明", "無回答", "未回答", "その他", "（回答場所不明）"}


def clean(value: object) -> str:
    return "" if value is None else str(value).replace("\ufeff", "").strip()


def parse_date(value: str) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    match = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if match:
        try:
            return datetime(*(int(v) for v in match.groups()))
        except ValueError:
            return None
    return None


def parse_number(value: str, minimum: float | None = None, maximum: float | None = None) -> float | None:
    text = clean(value).replace(",", "")
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    number = float(match.group())
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def normalize_prefecture(value: str) -> str:
    text = clean(value).replace("県", "")
    aliases = {"富山": "富山", "石川": "石川", "福井": "福井"}
    return aliases.get(text, "")


def normalize_age(row: dict[str, str], response_date: datetime) -> str:
    for key in ("回答時の年齢", "年代", "生まれ年"):
        raw = clean(row.get(key, ""))
        if not raw:
            continue
        if "10歳未満" in raw or "10才未満" in raw:
            return "10歳未満"
        band_match = re.search(r"(10|20|30|40|50|60|70|80|90|100)\s*代", raw)
        if band_match:
            return f"{band_match.group(1)}代"
        number = parse_number(raw)
        if number is None:
            continue
        age = response_date.year - int(number) if 1900 <= number <= response_date.year else int(number)
        if 0 <= age < 10:
            return "10歳未満"
        if 10 <= age <= 109:
            return f"{(age // 10) * 10}代"
    return ""


def normalize_gender(value: str) -> str:
    text = clean(value)
    if text in {"男", "男性", "Male", "male"}:
        return "男性"
    if text in {"女", "女性", "Female", "female"}:
        return "女性"
    return "その他/無回答"


def normalize_stay(value: str) -> str:
    text = clean(value)
    if not text:
        return ""
    if "日帰" in text or text in {"0", "0泊"}:
        return "日帰り"
    number = parse_number(text)
    if number is not None:
        nights = int(number)
        if nights >= 4:
            return "4泊以上"
        if 1 <= nights <= 3:
            return f"{nights}泊"
    return "4泊以上" if "4泊以上" in text else ""


def normalize_income(value: str) -> str:
    text = clean(value).replace(",", "").replace("?", "～").replace("〜", "～")
    if not text:
        return ""
    if any(word in text for word in ("無回答", "分から", "わから", "不明")) or text.startswith("6:"):
        return "分からない/無回答"
    if text.startswith("1:") or "300万円未満" in text or "100万円未満" in text or re.search(r"[12]00万円以上\s*[23]00万円未満", text):
        return "300万円未満"
    if text.startswith("2:") or any(f"{n}00万円以上" in text for n in (3, 4, 5)):
        return "300～599万円"
    if text.startswith("3:") or any(f"{n}00万円以上" in text for n in (6, 7, 8, 9)):
        return "600～999万円"
    if text.startswith("4:") or "1000万円以上" in text or "1,000万円以上" in clean(value) or "1200万円以上" in text or "1500万円以上" in text:
        return "1,000～1,999万円"
    if text.startswith("5:") or "2000万円以上" in text or "2,000万円以上" in clean(value):
        return "2,000万円以上"
    return "分からない/無回答"


def truthy_flag(value: str) -> bool:
    text = clean(value).lower()
    return text in {"1", "1.0", "true", "yes", "有", "あり", "○", "〇"}


def split_tokens(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[,、;；\n|]+", clean(value)) if token.strip()]


def select_flag_categories(row: dict[str, str], columns: list[str], fallback_column: str) -> list[str]:
    selected = [column for column in columns if column in row and truthy_flag(row.get(column, ""))]
    if selected:
        return selected
    tokens = split_tokens(row.get(fallback_column, ""))
    normalized = {re.sub(r"\s+", "", token).replace("(旧：Twitter)", "（旧Twitter）"): token for token in tokens}
    result = []
    for column in columns:
        key = re.sub(r"\s+", "", column)
        if key in normalized or (column == "X（旧Twitter）" and any(token.startswith("X(") or token.startswith("X（") for token in tokens)):
            result.append(column)
    return result


def select_companions(value: str) -> list[str]:
    tokens = split_tokens(value)
    aliases = {"一人": "自分ひとり", "ひとり": "自分ひとり", "夫婦": "夫婦2人", "同僚": "職場の同僚"}
    result = []
    for token in tokens:
        normalized = aliases.get(token, token)
        if normalized in COMPANIONS and normalized not in result:
            result.append(normalized)
    return result


def route_places(value: str) -> list[str]:
    places = []
    for token in re.split(r"[,、;；/／\n|]+", clean(value)):
        place = re.sub(r"\s+", " ", token).strip(" ・-")
        if 1 < len(place) <= 80 and place not in INVALID_PLACE_NAMES and place not in places:
            places.append(place)
    return places[:6]


def update_stats(stats: Counter, row: dict[str, str], response_date: datetime) -> None:
    stats["回答数"] += 1

    travel_satisfaction = parse_number(row.get("満足度（旅行全体）", ""), 0, 5)
    if travel_satisfaction is not None:
        stats["旅行全体満足度合計"] += travel_satisfaction
        stats["旅行全体満足度回答数"] += 1

    product_satisfaction = parse_number(row.get("満足度（商品・サービス）", ""), 0, 5)
    if product_satisfaction is not None:
        stats["商品サービス満足度合計"] += product_satisfaction
        stats["商品サービス満足度回答数"] += 1

    recommendation = parse_number(row.get("おすすめ度", ""), 0, 10)
    if recommendation is not None:
        stats["推奨度合計"] += recommendation
        stats["推奨度回答数"] += 1
        if recommendation >= 9:
            stats["推奨者"] += 1
        elif recommendation <= 6:
            stats["非推奨者"] += 1
        else:
            stats["中立者"] += 1

    single_values = (
        normalize_age(row, response_date),
        normalize_gender(row.get("性別", "")),
        clean(row.get("居住都道府県", "")),
        normalize_stay(row.get("宿泊数（全行程）", "")),
        normalize_income(row.get("世帯年収", "")),
    )
    for value in single_values:
        if value in DIMENSION_COLUMNS:
            stats[value] += 1

    for value in select_companions(row.get("同伴者", "")):
        stats[value] += 1
    for value in select_flag_categories(row, PURPOSES, "目的"):
        stats[value] += 1
    for value in select_flag_categories(row, SOURCES, "情報源"):
        stats[value] += 1


def discover_remote_files(repo: str, branch: str, token: str = "") -> list[tuple[int, str]]:
    api_url = f"https://api.github.com/repos/{repo}/contents/output_merge?ref={branch}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "TIFO-data-builder"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        entries = json.load(response)
    files = []
    for entry in entries:
        match = re.fullmatch(r"merged_survey_(\d{4})\.csv", entry.get("name", ""))
        if match and entry.get("download_url"):
            files.append((int(match.group(1)), entry["download_url"]))
    return sorted(files)


def discover_local_files(input_dir: Path) -> list[tuple[int, Path]]:
    files = []
    for path in input_dir.glob("merged_survey_*.csv"):
        match = re.fullmatch(r"merged_survey_(\d{4})\.csv", path.name)
        if match:
            files.append((int(match.group(1)), path))
    return sorted(files)


def download_file(url: str, destination: Path, token: str = "") -> None:
    headers = {"User-Agent": "TIFO-data-builder"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as target:
        shutil.copyfileobj(response, target, length=1024 * 1024)


def read_rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path.name}: 必須列がありません: {', '.join(sorted(missing))}")
        yield from reader


def write_wide(path: Path, rows: Iterable[tuple[tuple, Counter]], include_date: bool) -> None:
    columns = DAILY_COLUMNS if include_date else SNAPSHOT_COLUMNS
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for key, stats in rows:
            if include_date:
                date_value, pref, facility = key
                output = {"日付": date_value, "TIF": pref, "回答場所": facility}
            else:
                pref, facility = key
                output = {"TIF": pref, "回答場所": facility}
            output.update({column: stats.get(column, 0) for column in CORE_COLUMNS + DIMENSION_COLUMNS})
            writer.writerow(output)


def write_monthly(path: Path, monthly: dict[tuple[str, str], Counter]) -> None:
    columns = ["月", "TIF", *CORE_COLUMNS]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for (month, pref), stats in sorted(monthly.items()):
            row = {"月": month, "TIF": pref}
            row.update({column: stats.get(column, 0) for column in CORE_COLUMNS})
            writer.writerow(row)


def write_routes(path: Path, routes: Counter, year: int | None = None) -> None:
    # 全期間版は日付を落として再集計し、初回の回遊表示を軽量化する。
    compact = Counter()
    for (_date_value, pref, origin, destination), count in routes.items():
        compact[(pref, origin, destination)] += count
    # 自由入力の訪問地を含むため、全期間で3件未満の組合せは公開用データから除外する。
    allowed = {key for key, count in compact.items() if count >= 3}
    columns = ["日付", "TIF", "移動元", "移動先", "回答数"] if year is not None else ["TIF", "移動元", "移動先", "回答数"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        if year is None:
            for (pref, origin, destination), count in sorted(compact.items()):
                if (pref, origin, destination) not in allowed:
                    continue
                writer.writerow({"TIF": pref, "移動元": origin, "移動先": destination, "回答数": count})
            return
        for (date_value, pref, origin, destination), count in sorted(routes.items()):
            if not date_value.startswith(str(year)):
                continue
            if (pref, origin, destination) not in allowed:
                continue
            writer.writerow({"日付": date_value, "TIF": pref, "移動元": origin, "移動先": destination, "回答数": count})


def build(source_files: list[tuple[int, Path]], output_dir: Path, source_label: str) -> dict:
    all_stats: dict[tuple[str, str], Counter] = defaultdict(Counter)
    daily_stats: dict[int, dict[tuple[str, str, str], Counter]] = defaultdict(lambda: defaultdict(Counter))
    monthly_stats: dict[tuple[str, str], Counter] = defaultdict(Counter)
    routes: Counter = Counter()
    min_date: str | None = None
    max_date: str | None = None
    invalid_dates = 0
    processed = 0

    for source_year, path in source_files:
        print(f"Processing {path.name} ...", flush=True)
        for row in read_rows(path):
            response_date = parse_date(row.get("アンケート回答日", ""))
            if response_date is None:
                invalid_dates += 1
                continue
            pref = normalize_prefecture(row.get("対象県（富山/石川/福井）", ""))
            facility = clean(row.get("回答場所", "")) or "（回答場所不明）"
            if not pref:
                continue
            date_value = response_date.strftime("%Y-%m-%d")
            year = response_date.year
            if abs(year - source_year) > 1:
                print(f"Warning: {path.name} に {date_value} の行があります", flush=True)
            min_date = date_value if min_date is None or date_value < min_date else min_date
            max_date = date_value if max_date is None or date_value > max_date else max_date

            update_stats(all_stats[(pref, facility)], row, response_date)
            update_stats(daily_stats[year][(date_value, pref, facility)], row, response_date)
            update_stats(monthly_stats[(date_value[:7], pref)], row, response_date)
            processed += 1

            before = route_places(row.get("アンケート前に訪れた主な場所（施設やスポット名称、エリアなど）", ""))
            before += route_places(row.get("上記名称リストにない場合について、具体的にお答えください。(前)", ""))
            after = route_places(row.get("アンケート後に訪れる予定の場所（施設やスポット名称、エリア）", ""))
            after += route_places(row.get("上記名称リストにない場合について、具体的にお答えください。(後)", ""))
            if facility not in INVALID_PLACE_NAMES:
                for place in dict.fromkeys(before):
                    if place != facility:
                        routes[(date_value, pref, place, facility)] += 1
                for place in dict.fromkeys(after):
                    if place != facility:
                        routes[(date_value, pref, facility, place)] += 1

    if not processed:
        raise RuntimeError("有効な回答を1件も集計できませんでした。")

    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_pattern in ("tifo_daily_*.csv", "tifo_routes_*.csv"):
        for stale in output_dir.glob(stale_pattern):
            stale.unlink()

    write_wide(output_dir / SNAPSHOT_FILE, sorted(all_stats.items()), include_date=False)
    write_monthly(output_dir / MONTHLY_FILE, monthly_stats)
    write_routes(output_dir / ROUTES_ALL_FILE, routes)
    for year, rows in sorted(daily_stats.items()):
        write_wide(output_dir / f"tifo_daily_{year}.csv", sorted(rows.items()), include_date=True)
        write_routes(output_dir / f"tifo_routes_{year}.csv", routes, year=year)

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    years = sorted(daily_stats)
    manifest = {
        "version": 1,
        "generated_at": generated_at,
        "source": source_label,
        "source_files": [path.name for _, path in source_files],
        "min_date": min_date,
        "max_date": max_date,
        "response_count": processed,
        "invalid_date_count": invalid_dates,
        "available_years": years,
        "files": {
            "snapshot": SNAPSHOT_FILE,
            "monthly": MONTHLY_FILE,
            "daily": {str(year): f"tifo_daily_{year}.csv" for year in years},
            "routes_all": ROUTES_ALL_FILE,
            "routes": {str(year): f"tifo_routes_{year}.csv" for year in years},
        },
        "dimensions": DIMENSIONS,
        "notes": [
            "日次CSVの数値は加算可能な件数・合計値です。平均値とNPSは選択期間の合計から再計算してください。",
            "複数回答項目は各選択肢の件数のため、構成比の合計が100%を超える場合があります。",
            "回遊データはアンケートに前後訪問地の回答がある行のみを集計しています。",
            "回遊データは自由入力を含むため、全期間で同じ移動組合せが3件以上あるものだけを出力しています。",
        ],
    }
    (output_dir / DATA_MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, help="merged_survey_YYYY.csv のあるローカルディレクトリ")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="公開ファイルの出力先。TIFOではリポジトリ直下を指定する")
    parser.add_argument("--repo", default="hokuriku-inbound-kanko/opendata")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--source-label", help="manifest.jsonに記録する出典表示（省略時は入力元から自動設定）")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if args.input_dir:
        local_files = discover_local_files(args.input_dir)
        if not local_files:
            raise SystemExit(f"{args.input_dir} に merged_survey_YYYY.csv がありません。")
        manifest = build(local_files, args.output_dir, args.source_label or str(args.input_dir))
    else:
        remote_files = discover_remote_files(args.repo, args.branch, token)
        if not remote_files:
            raise SystemExit("GitHub上に merged_survey_YYYY.csv がありません。")
        with tempfile.TemporaryDirectory(prefix="tifo-source-") as temp_name:
            temp_dir = Path(temp_name)
            local_files = []
            for year, url in remote_files:
                destination = temp_dir / f"merged_survey_{year}.csv"
                print(f"Downloading {url} ...", flush=True)
                download_file(url, destination, token)
                local_files.append((year, destination))
            manifest = build(local_files, args.output_dir, args.source_label or f"https://github.com/{args.repo}/tree/{args.branch}/output_merge")
    print(json.dumps({key: manifest[key] for key in ("generated_at", "min_date", "max_date", "response_count", "available_years")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
