from __future__ import annotations

import io
import re
from datetime import datetime, date
from typing import Optional

import pandas as pd

from core.auth import require_permission
from core.config import (
    UPLOAD_DIR,
    META_ID_COL,
    META_FOLLOWERS_COL,
    META_LINK_COL,
    META_REACH_COL,
    META_ACCOUNT_USERNAME_COL,
    META_ACCOUNT_NAME_COL,
    META_PUBLISHED_AT_COL,
    META_COLUMN_ALIASES,
    PR_START_COL,
    PR_END_COL,
    PR_AD_NAME_COL,
    PR_PAGE_COL,
    PR_FOLLOWERS_COL,
    PR_SPEND_COL,
    PR_COLUMN_ALIASES,
    REQUIRED_META,
    REQUIRED_PR,
    now_utc,
)
from core.i18n import tr
from core.database import connect_db


# -------------------- generic helpers --------------------

def is_novakid_account(account: object) -> bool:
    return str(account).strip().lstrip("@").lower().startswith("novakid")


def clean_id(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def to_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(" ", "", regex=False).str.replace(",", ".", regex=False),
        errors="coerce",
    ).fillna(0)


def normalize_period(value) -> str:
    dt = pd.to_datetime(value, errors="coerce", dayfirst=False)
    if pd.isna(dt):
        dt = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(dt):
        raise ValueError(tr(f"Could not parse period date: {value}", f"Не удалось распознать дату периода: {value}"))
    return dt.date().isoformat()


def normalize_publication_date(value) -> str:
    dt = pd.to_datetime(value, errors="coerce", dayfirst=False)
    if pd.isna(dt):
        dt = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(dt):
        raise ValueError(tr(f"Could not parse publication date: {value}", f"Не удалось распознать дату публикации: {value}"))
    return dt.date().isoformat()


def month_from_period(period_start: str) -> str:
    return period_start[:7]


def validate_columns(df: pd.DataFrame, required: list[str], file_label: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(tr(f"The {file_label} file is missing columns: {', '.join(missing)}", f"В файле {file_label} нет колонок: {', '.join(missing)}"))


def normalize_meta_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    rename_map = {}
    used_aliases = []
    for canonical, aliases in META_COLUMN_ALIASES.items():
        if canonical in df.columns:
            continue
        for alias in aliases:
            if alias in df.columns:
                rename_map[alias] = canonical
                used_aliases.append(f"{alias} -> {canonical}")
                break

    normalized = df.rename(columns=rename_map).copy()
    return normalized, used_aliases


def normalize_pr_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    rename_map = {}
    used_aliases = []
    normalized_columns = {str(column).strip().casefold(): column for column in df.columns}
    for canonical, aliases in PR_COLUMN_ALIASES.items():
        if canonical in df.columns:
            continue
        for alias in aliases:
            source_column = normalized_columns.get(alias.casefold())
            if source_column:
                rename_map[source_column] = canonical
                used_aliases.append(f"{source_column} -> {canonical}")
                break

    normalized = df.rename(columns=rename_map).copy()
    return normalized, used_aliases


def parse_meta_period_from_filename(filename: str) -> Optional[tuple[str, str]]:
    month_map = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    pattern = re.compile(
        r"(?P<m1>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(?P<d1>\d{1,2})-(?P<y1>\d{4})"
        r"[_\s-]+"
        r"(?P<m2>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(?P<d2>\d{1,2})-(?P<y2>\d{4})",
        re.IGNORECASE,
    )
    m = pattern.search(filename)
    if not m:
        return None
    start = f"{m.group('y1')}-{month_map[m.group('m1').lower()]}-{int(m.group('d1')):02d}"
    end = f"{m.group('y2')}-{month_map[m.group('m2').lower()]}-{int(m.group('d2')):02d}"
    return start, end


def parse_pr_period_from_filename(filename: str) -> Optional[tuple[str, str]]:
    month_map = {
        "янв": "01",
        "фев": "02",
        "мар": "03",
        "апр": "04",
        "май": "05",
        "мая": "05",
        "июн": "06",
        "июл": "07",
        "авг": "08",
        "сен": "09",
        "сент": "09",
        "окт": "10",
        "ноя": "11",
        "дек": "12",
    }
    month_pattern = "|".join(sorted(month_map, key=len, reverse=True))
    pattern = re.compile(
        rf"(?P<d1>\d{{1,2}})[\s_.-]*(?P<m1>{month_pattern})[\s_.-]*(?P<y1>\d{{4}})"
        rf".*?"
        rf"(?P<d2>\d{{1,2}})[\s_.-]*(?P<m2>{month_pattern})[\s_.-]*(?P<y2>\d{{4}})",
        re.IGNORECASE,
    )
    m = pattern.search(filename)
    if not m:
        return None
    start = f"{m.group('y1')}-{month_map[m.group('m1').lower()]}-{int(m.group('d1')):02d}"
    end = f"{m.group('y2')}-{month_map[m.group('m2').lower()]}-{int(m.group('d2')):02d}"
    return start, end


def infer_accounts_from_meta(df: pd.DataFrame) -> list[str]:
    df, _ = normalize_meta_columns(df)
    if META_ACCOUNT_USERNAME_COL not in df.columns:
        return []
    return sorted([
        str(x).strip()
        for x in df[META_ACCOUNT_USERNAME_COL].dropna().unique()
        if is_novakid_account(x)
    ])


def read_table_any(uploaded_file) -> pd.DataFrame:
    raw = uploaded_file.getvalue()
    if str(uploaded_file.name).lower().endswith(".xlsx"):
        try:
            return pd.read_excel(io.BytesIO(raw))
        except Exception as exc:
            raise ValueError(tr("Could not read the Excel file.", "Не удалось прочитать Excel-файл.")) from exc
    for enc in ("utf-8-sig", "utf-16", "cp1251", "latin1"):
        try:
            text = raw.decode(enc)
            first = text.splitlines()[0]
            sep = ";" if first.count(";") > first.count(",") else ","
            return pd.read_csv(io.StringIO(text), sep=sep)
        except Exception:
            continue
    raise ValueError(tr("Could not read the CSV. Check the encoding and file format.", "Не удалось прочитать CSV. Проверьте кодировку и формат файла."))


def read_csv_any(uploaded_file) -> pd.DataFrame:
    """Backward-compatible name; reads both CSV and Excel uploads."""
    return read_table_any(uploaded_file)


PR_PAGE_ACCOUNT_SUGGESTIONS = {
    "novakid türkiye": "novakidturkiye",
    "novakid school": "novakidschool",
    "novakid italia": "novakid_italia",
    "novakid españa": "novakid_spain",
    "novakid deutschland": "novakid_de",
    "novakid mena": "novakid_mena",
    "novakid polska": "novakidpolska",
    "novakid korea": "novakid_korea",
    "novakid romania": "novakid_romania",
    "novakid france": "novakid_france",
    "novakid israel": "novakid_israel",
    "novakid japan": "novakid_jp",
    "novakid czech": "novakid_czech",
}


def pr_page_summary(uploaded_file) -> pd.DataFrame:
    df, _ = normalize_pr_columns(read_table_any(uploaded_file))
    if PR_PAGE_COL not in df.columns or PR_FOLLOWERS_COL not in df.columns:
        return pd.DataFrame(columns=[PR_PAGE_COL, PR_FOLLOWERS_COL, "account"])
    data = df.copy()
    data[PR_PAGE_COL] = data[PR_PAGE_COL].fillna("").astype(str).str.strip()
    data = data[(data[PR_PAGE_COL] != "") & (data[PR_PAGE_COL] != "12")].copy()
    data[PR_FOLLOWERS_COL] = to_number(data[PR_FOLLOWERS_COL]).astype(int)
    summary = data.groupby(PR_PAGE_COL, as_index=False)[PR_FOLLOWERS_COL].sum()
    summary["account"] = summary[PR_PAGE_COL].str.casefold().map(PR_PAGE_ACCOUNT_SUGGESTIONS).fillna("")
    return summary.sort_values(PR_FOLLOWERS_COL, ascending=False)


def save_uploaded_file(uploaded_file, file_type: str, data: Optional[bytes] = None) -> str:
    safe_name = re.sub(r"[^A-Za-zА-Яа-я0-9_.() -]+", "_", uploaded_file.name)
    target = UPLOAD_DIR / file_type / datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    target.mkdir(parents=True, exist_ok=True)
    path = target / safe_name
    with path.open("wb") as f:
        f.write(uploaded_file.getvalue() if data is None else data)
    return str(path)


def dataframe_to_excel_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    return output.getvalue()


def monthly_increment_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["month", "account", "monthly_followers", "month_label"])

    monthly = (
        df.groupby(["month", "account"], as_index=False)["final_followers"]
        .sum()
        .sort_values(["account", "month"])
    )
    monthly["monthly_followers"] = monthly["final_followers"].astype(int)
    monthly["month_label"] = pd.to_datetime(monthly["month"] + "-01").dt.strftime("%b %Y")
    return monthly


def latest_publications_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    sort_cols = [c for c in ["account", "publication_id", "month", "period_end", "updated_at"] if c in df.columns]
    latest = df.sort_values(sort_cols).drop_duplicates(["account", "publication_id", "month"], keep="last")
    return latest


# -------------------- Recalculation --------------------

def recalc_final(account: str, period_start: str, period_end: str) -> None:
    with connect_db() as conn:
        meta_rows = conn.execute(
            "SELECT * FROM meta_publications WHERE account=? AND period_start=? AND period_end=?",
            (account, period_start, period_end),
        ).fetchall()
        current = now_utc()
        conn.execute(
            "DELETE FROM final_results WHERE account=? AND period_start=? AND period_end=?",
            (account, period_start, period_end),
        )
        for m in meta_rows:
            pr = conn.execute(
                "SELECT * FROM pr_ads WHERE account=? AND period_start=? AND period_end=? AND publication_id=?",
                (account, period_start, period_end, m["publication_id"]),
            ).fetchone()
            imported_pr_followers = int(pr["pr_followers"]) if pr else 0
            override = conn.execute(
                """
                SELECT manual_pr_followers, updated_by, updated_at
                FROM follower_overrides
                WHERE account=? AND period_start=? AND period_end=? AND publication_id=?
                """,
                (account, period_start, period_end, m["publication_id"]),
            ).fetchone()
            manual_pr_followers = int(override["manual_pr_followers"]) if override else None
            pr_followers = manual_pr_followers if manual_pr_followers is not None else imported_pr_followers
            spend = float(pr["spend_usd"]) if pr else 0.0
            raw_final = int(m["meta_followers"]) - pr_followers
            warning = ""
            if raw_final < 0:
                warning = tr("Follower count became negative. Check Meta/Novakid PR data.", "Получилось отрицательное значение подписчиков. Нужно проверить Meta/Novakid PR.")
            final_followers = max(0, raw_final)
            cpf = round(spend / pr_followers, 4) if pr_followers > 0 else None
            conn.execute(
                """
                INSERT INTO final_results(
                    account, account_name, period_start, period_end, month, publication_date, publication_id, publication_link,
                    post_reach, meta_followers, imported_pr_followers, manual_pr_followers, pr_followers, final_followers,
                    spend_usd, cpf_usd, warning, meta_uploaded_by, pr_uploaded_by,
                    override_updated_by, override_updated_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    account, m["account_name"], period_start, period_end, m["month"], m["publication_date"], m["publication_id"],
                    m["publication_link"], int(m["post_reach"]), int(m["meta_followers"]), imported_pr_followers, manual_pr_followers,
                    pr_followers, final_followers, spend, cpf, warning, m["uploaded_by"],
                    pr["uploaded_by"] if pr else None, override["updated_by"] if override else None,
                    override["updated_at"] if override else None, current,
                ),
            )
        conn.commit()
    recalc_monthly_totals(account, period_start, period_end)


def recalc_monthly_totals(account: str, period_start: str, period_end: str) -> None:
    """Persist the complete regional month, including paid rows not matched to Meta posts."""
    with connect_db() as conn:
        imported_total = int(conn.execute(
            "SELECT COALESCE(SUM(meta_followers), 0) FROM meta_publications WHERE account=? AND period_start=? AND period_end=?",
            (account, period_start, period_end),
        ).fetchone()[0])
        imported_paid = int(conn.execute(
            """
            SELECT COALESCE(SUM(paid), 0) FROM (
                SELECT COALESCE(o.manual_pr_followers, p.pr_followers) AS paid
                FROM pr_ads p
                LEFT JOIN follower_overrides o USING(account, period_start, period_end, publication_id)
                WHERE p.account=? AND p.period_start=? AND p.period_end=?
                UNION ALL
                SELECT o.manual_pr_followers AS paid
                FROM follower_overrides o
                LEFT JOIN pr_ads p USING(account, period_start, period_end, publication_id)
                WHERE o.account=? AND o.period_start=? AND o.period_end=? AND p.publication_id IS NULL
            )
            """,
            (account, period_start, period_end, account, period_start, period_end),
        ).fetchone()[0])
        existing = conn.execute(
            "SELECT manual_total_followers, manual_paid_followers, updated_by FROM monthly_follower_totals WHERE account=? AND period_start=? AND period_end=?",
            (account, period_start, period_end),
        ).fetchone()
        manual_total = existing["manual_total_followers"] if existing else None
        manual_paid = existing["manual_paid_followers"] if existing else None
        total = int(manual_total) if manual_total is not None else imported_total
        paid = int(manual_paid) if manual_paid is not None else imported_paid
        conn.execute(
            """
            INSERT INTO monthly_follower_totals(
                account, period_start, period_end, month, imported_total_followers, imported_paid_followers,
                manual_total_followers, manual_paid_followers, total_followers, paid_followers,
                organic_followers, updated_by, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account, period_start, period_end) DO UPDATE SET
                imported_total_followers=excluded.imported_total_followers,
                imported_paid_followers=excluded.imported_paid_followers,
                total_followers=excluded.total_followers,
                paid_followers=excluded.paid_followers,
                organic_followers=excluded.organic_followers,
                updated_at=excluded.updated_at
            """,
            (account, period_start, period_end, period_start[:7], imported_total, imported_paid,
             manual_total, manual_paid, total, paid, max(0, total - paid),
             existing["updated_by"] if existing else None, now_utc()),
        )
        conn.commit()


def save_monthly_follower_totals(rows: pd.DataFrame, user: dict) -> int:
    require_permission(user, "edit_reports")
    updated_at = now_utc()
    affected: list[tuple[str, str, str]] = []
    with connect_db() as conn:
        for _, row in rows.iterrows():
            manual_total = None if pd.isna(row["manual_total_followers"]) else int(row["manual_total_followers"])
            manual_paid = None if pd.isna(row["manual_paid_followers"]) else int(row["manual_paid_followers"])
            if manual_total is not None and manual_total < 0 or manual_paid is not None and manual_paid < 0:
                raise ValueError(tr("Follower counts cannot be negative.", "Количество подписчиков не может быть отрицательным."))
            if manual_total is not None and manual_paid is not None and manual_paid > manual_total:
                raise ValueError(tr("Paid followers cannot exceed total followers.", "Платных подписчиков не может быть больше, чем всех подписчиков."))
            key = (str(row["account"]), str(row["period_start"]), str(row["period_end"]))
            conn.execute(
                "UPDATE monthly_follower_totals SET manual_total_followers=?, manual_paid_followers=?, updated_by=?, updated_at=? WHERE account=? AND period_start=? AND period_end=?",
                (manual_total, manual_paid, user["username"], updated_at, *key),
            )
            affected.append(key)
        conn.commit()
    for key in affected:
        recalc_monthly_totals(*key)
    return len(affected)


def save_follower_overrides(rows: pd.DataFrame, user: dict) -> int:
    require_permission(user, "edit_reports")
    selected = rows[rows["Изменить"] == True].copy()  # noqa: E712
    if selected.empty:
        raise ValueError(tr("Select rows where a manual value should be saved.", "Отметьте строки, для которых нужно сохранить ручное значение."))

    affected_periods: set[tuple[str, str, str]] = set()
    updated_at = now_utc()
    with connect_db() as conn:
        for _, row in selected.iterrows():
            key = (str(row["account"]), str(row["period_start"]), str(row["period_end"]), str(row["publication_id"]))
            value = row["manual_pr_followers"]
            if pd.isna(value):
                conn.execute(
                    """
                    DELETE FROM follower_overrides
                    WHERE account=? AND period_start=? AND period_end=? AND publication_id=?
                    """,
                    key,
                )
            else:
                manual_value = int(value)
                if manual_value < 0:
                    raise ValueError(tr("Manual follower count cannot be negative.", "Ручное количество подписчиков не может быть отрицательным."))
                conn.execute(
                    """
                    INSERT INTO follower_overrides(
                        account, period_start, period_end, publication_id,
                        manual_pr_followers, updated_by, updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(account, period_start, period_end, publication_id)
                    DO UPDATE SET
                        manual_pr_followers=excluded.manual_pr_followers,
                        updated_by=excluded.updated_by,
                        updated_at=excluded.updated_at
                    """,
                    (*key, manual_value, user["username"], updated_at),
                )
            affected_periods.add(key[:3])
        conn.commit()

    for account, period_start, period_end in affected_periods:
        recalc_final(account, period_start, period_end)
    return len(selected)


# -------------------- Import logic --------------------

def import_meta(uploaded_file, user: dict, manual_start: Optional[date], manual_end: Optional[date]) -> tuple[int, list[str]]:
    require_permission(user, "upload_meta")
    df = read_csv_any(uploaded_file)
    df, used_aliases = normalize_meta_columns(df)
    validate_columns(df, REQUIRED_META, "Meta Business Suite")
    period = parse_meta_period_from_filename(uploaded_file.name)
    warnings: list[str] = []
    if used_aliases:
        warnings.append(tr("Meta columns were recognized by English names: ", "Meta-колонки распознаны по английским названиям: ") + ", ".join(used_aliases) + ".")
    if period:
        period_start, period_end = period
    elif manual_start and manual_end:
        period_start, period_end = manual_start.isoformat(), manual_end.isoformat()
        warnings.append(tr("Meta period was taken from manual input because it could not be detected from the filename.", "Период Meta взят из ручного ввода, потому что его не удалось определить из имени файла."))
    else:
        raise ValueError(tr("Could not detect the Meta period from the filename. Enter dates manually.", "Не удалось определить период Meta из имени файла. Укажите даты вручную."))

    if period_start > period_end:
        raise ValueError(tr("Period start date is after the end date.", "Дата начала периода больше даты окончания."))

    df = df.copy()
    df[META_ID_COL] = df[META_ID_COL].apply(clean_id)
    df[META_PUBLISHED_AT_COL] = df[META_PUBLISHED_AT_COL].apply(normalize_publication_date)
    df[META_FOLLOWERS_COL] = to_number(df[META_FOLLOWERS_COL]).astype(int)
    if META_REACH_COL not in df.columns:
        df[META_REACH_COL] = 0
        warnings.append(tr("The Meta file has no reach column; publications were saved with reach 0.", "В Meta-файле нет колонки охвата; для публикаций сохранено значение 0."))
    df[META_REACH_COL] = to_number(df[META_REACH_COL]).astype(int)
    df[META_ACCOUNT_USERNAME_COL] = df[META_ACCOUNT_USERNAME_COL].astype(str).str.strip()
    if META_ACCOUNT_NAME_COL not in df.columns:
        df[META_ACCOUNT_NAME_COL] = ""

    df = df[(df[META_ID_COL] != "") & (df[META_ACCOUNT_USERNAME_COL] != "") & (df[META_FOLLOWERS_COL] >= 1)].copy()
    skipped_accounts = sorted(
        df.loc[~df[META_ACCOUNT_USERNAME_COL].apply(is_novakid_account), META_ACCOUNT_USERNAME_COL].unique().tolist()
    )
    df = df[df[META_ACCOUNT_USERNAME_COL].apply(is_novakid_account)].copy()
    if skipped_accounts:
        warnings.append(
            tr(f"Skipped blogger accounts ({len(skipped_accounts)}): ", f"Пропущены аккаунты блогеров ({len(skipped_accounts)}): ")
            + ", ".join(skipped_accounts[:10])
            + (tr(f" and {len(skipped_accounts) - 10} more.", " и другие.") if len(skipped_accounts) > 10 else ".")
        )
    if df.empty:
        raise ValueError(tr("The Meta file has no Novakid publications with 1+ follower.", "В Meta-файле нет публикаций Novakid с 1+ подписчиком."))

    # Внутри файла группируем по аккаунту + ID, чтобы не было дублей.
    grouped = (
        df.groupby([META_ACCOUNT_USERNAME_COL, META_ID_COL], as_index=False)
        .agg({
            META_ACCOUNT_NAME_COL: "first",
            META_LINK_COL: "first",
            META_PUBLISHED_AT_COL: "first",
            META_REACH_COL: "max",
            META_FOLLOWERS_COL: "sum",
        })
    )

    filtered_file = df.to_csv(index=False).encode("utf-8-sig")
    stored_path = save_uploaded_file(uploaded_file, "meta", filtered_file)
    uploaded_at = now_utc()
    rows = []
    for _, r in grouped.iterrows():
        publication_date = r[META_PUBLISHED_AT_COL]
        month = month_from_period(publication_date)
        rows.append((
            r[META_ACCOUNT_USERNAME_COL], r.get(META_ACCOUNT_NAME_COL, ""), period_start, period_end, month, publication_date,
            r[META_ID_COL], r.get(META_LINK_COL, ""), int(r[META_REACH_COL]), int(r[META_FOLLOWERS_COL]), uploaded_file.name,
            user["username"], uploaded_at,
        ))

    with connect_db() as conn:
        conn.executemany(
            """
            INSERT INTO meta_publications(
                account, account_name, period_start, period_end, month, publication_date, publication_id, publication_link,
                post_reach, meta_followers, meta_filename, uploaded_by, uploaded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account, period_start, period_end, publication_id)
            DO UPDATE SET
                account_name=excluded.account_name,
                month=excluded.month,
                publication_date=excluded.publication_date,
                publication_link=excluded.publication_link,
                post_reach=excluded.post_reach,
                meta_followers=excluded.meta_followers,
                meta_filename=excluded.meta_filename,
                uploaded_by=excluded.uploaded_by,
                uploaded_at=excluded.uploaded_at
            """,
            rows,
        )
        conn.execute(
            "INSERT INTO uploads(file_type,account,period_start,period_end,filename,stored_path,uploaded_by,uploaded_at,rows_saved,warnings) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("meta", None, period_start, period_end, uploaded_file.name, stored_path, user["username"], uploaded_at, len(rows), "\n".join(warnings)),
        )
        conn.commit()

    for account in sorted(grouped[META_ACCOUNT_USERNAME_COL].unique()):
        recalc_final(str(account), period_start, period_end)
    return len(rows), warnings


def import_pr(
    uploaded_file,
    user: dict,
    account: str,
    auto_detect_accounts: bool = False,
    page_account_map: Optional[dict[str, str]] = None,
    add_only: bool = False,
) -> tuple[int, list[str]]:
    require_permission(user, "upload_pr")
    page_account_map = {str(k).strip(): str(v).strip() for k, v in (page_account_map or {}).items() if str(v).strip()}
    if not auto_detect_accounts and not page_account_map and not account.strip():
        raise ValueError(tr("Choose an account for the PR file, for example novakid_israel.", "Для PR-файла нужно выбрать аккаунт, например novakid_israel."))
    account = account.strip()
    if not auto_detect_accounts and not page_account_map and not is_novakid_account(account):
        raise ValueError(tr("Data can only be saved for Novakid accounts.", "Можно сохранять данные только для аккаунтов Novakid."))
    df = read_csv_any(uploaded_file)
    df, used_aliases = normalize_pr_columns(df)
    validate_columns(df, REQUIRED_PR, "Novakid PR")
    warnings: list[str] = []
    if used_aliases:
        warnings.append(tr("PR columns were recognized by alternative names: ", "PR-колонки распознаны по альтернативным названиям: ") + ", ".join(used_aliases) + ".")
    df = df.copy()
    if page_account_map and PR_PAGE_COL in df.columns:
        df[PR_PAGE_COL] = df[PR_PAGE_COL].fillna("").astype(str).str.strip()
        summary_rows = df[PR_PAGE_COL] == "12"
        if summary_rows.any():
            df = df[~summary_rows].copy()
            warnings.append(tr("The Excel summary row was skipped.", "Итоговая строка Excel была пропущена."))
        unknown_pages = sorted(set(df.loc[~df[PR_PAGE_COL].isin(page_account_map), PR_PAGE_COL]) - {""})
        if unknown_pages:
            raise ValueError(tr(
                "Choose an Instagram account for every page: " + ", ".join(unknown_pages),
                "Выберите Instagram-аккаунт для каждой страницы: " + ", ".join(unknown_pages),
            ))
        df["__account"] = df[PR_PAGE_COL].map(page_account_map)
    df[PR_START_COL] = df[PR_START_COL].apply(normalize_period)
    df[PR_END_COL] = df[PR_END_COL].apply(normalize_period)
    starts = sorted(df[PR_START_COL].dropna().unique())
    ends = sorted(df[PR_END_COL].dropna().unique())
    file_period = parse_pr_period_from_filename(uploaded_file.name)
    if file_period:
        period_start, period_end = file_period
        column_periods = {(start, end) for start in starts for end in ends}
        if column_periods != {file_period}:
            warnings.append(
                tr(
                    f"PR period was taken from the filename ({period_start} - {period_end}); CSV columns contain {', '.join(f'{start} - {end}' for start, end in sorted(column_periods))}.",
                    f"Период PR взят из имени файла ({period_start} - {period_end}); в колонках CSV указано {', '.join(f'{start} - {end}' for start, end in sorted(column_periods))}.",
                )
            )
    elif len(starts) != 1 or len(ends) != 1:
        raise ValueError(tr("Multiple periods were found in Novakid PR. Upload a file for one period only.", "В Novakid PR найдено несколько периодов. Загрузите файл только за один период."))
    else:
        period_start, period_end = starts[0], ends[0]
    if period_start > period_end:
        raise ValueError(tr("PR period start date is after the end date.", "Дата начала периода PR больше даты окончания."))
    month = month_from_period(period_start)

    df[PR_AD_NAME_COL] = df[PR_AD_NAME_COL].apply(clean_id)
    df[PR_FOLLOWERS_COL] = to_number(df[PR_FOLLOWERS_COL]).astype(int)
    df[PR_SPEND_COL] = to_number(df[PR_SPEND_COL]).astype(float)
    df = df[df[PR_AD_NAME_COL] != ""].copy()
    group_columns = (["__account", PR_AD_NAME_COL] if page_account_map and PR_PAGE_COL in df.columns else [PR_AD_NAME_COL])
    grouped = (
        df.groupby(group_columns, as_index=False)
        .agg({PR_FOLLOWERS_COL: "sum", PR_SPEND_COL: "sum"})
        .rename(columns={PR_AD_NAME_COL: "publication_id"})
    )
    if grouped.empty:
        raise ValueError(tr("Novakid PR has no rows with a filled ad name.", "В Novakid PR нет строк с заполненным названием объявления."))

    if page_account_map and "__account" in grouped.columns:
        grouped = grouped.rename(columns={"__account": "account"})
    elif auto_detect_accounts:
        ids = grouped["publication_id"].dropna().astype(str).tolist()
        placeholders = ",".join(["?"] * len(ids))
        with connect_db() as conn:
            cursor = conn.execute(
                f"""
                SELECT publication_id, account
                FROM meta_publications
                WHERE period_start=? AND period_end=? AND publication_id IN ({placeholders})
                GROUP BY publication_id, account
                """,
                (period_start, period_end, *ids),
            )
            meta_matches = pd.DataFrame(
                [(row[0], row[1]) for row in cursor.fetchall()],
                columns=["publication_id", "account"],
            )

        if meta_matches.empty:
            raise ValueError(tr("No PR-to-Meta matches were found by publication ID for this period.", "Не найдено совпадений PR с Meta по ID публикации за этот период."))

        account_counts = meta_matches.groupby("publication_id")["account"].nunique()
        ambiguous_ids = set(account_counts[account_counts > 1].index.astype(str))
        matched_once = meta_matches[~meta_matches["publication_id"].isin(ambiguous_ids)].copy()
        grouped = grouped.merge(matched_once, on="publication_id", how="left")

        unmatched_ids = grouped.loc[grouped["account"].isna(), "publication_id"].astype(str).tolist()
        excluded_ids = sorted(set(unmatched_ids) | ambiguous_ids)
        if excluded_ids:
            preview = ", ".join(excluded_ids[:25])
            extra = "" if len(excluded_ids) <= 25 else tr(f" and {len(excluded_ids) - 25} more", f" и еще {len(excluded_ids) - 25}")
            warnings.append(
                tr("Excluded PR rows without an unambiguous Meta match by publication ID: ", "Исключены строки PR без однозначного совпадения с Meta по ID публикации: ")
                + f"{preview}{extra}."
            )

        grouped = grouped[grouped["account"].notna()].copy()
        if grouped.empty:
            raise ValueError(tr("After excluding unmatched IDs, there are no PR rows left to save.", "После исключения несовпавших ID не осталось строк PR для сохранения."))
    else:
        grouped["account"] = account

    stored_path = save_uploaded_file(uploaded_file, "pr")
    uploaded_at = now_utc()
    rows = []
    for _, r in grouped.iterrows():
        rows.append((
            r["account"], period_start, period_end, month, r["publication_id"], int(r[PR_FOLLOWERS_COL]),
            float(r[PR_SPEND_COL]), uploaded_file.name, user["username"], uploaded_at,
        ))

    rows_to_save = rows
    if add_only:
        with connect_db() as conn:
            existing_keys = {
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT account, publication_id FROM pr_ads WHERE period_start=? AND period_end=?",
                    (period_start, period_end),
                ).fetchall()
            }
        rows_to_save = [row for row in rows if (str(row[0]), str(row[4])) not in existing_keys]
        skipped = len(rows) - len(rows_to_save)
        if skipped:
            warnings.append(tr(
                f"Existing rows left unchanged: {skipped}.",
                f"Существующие строки оставлены без изменений: {skipped}.",
            ))

    with connect_db() as conn:
        conn.executemany(
            """
            INSERT INTO pr_ads(
                account, period_start, period_end, month, publication_id, pr_followers, spend_usd,
                pr_filename, uploaded_by, uploaded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account, period_start, period_end, publication_id)
            DO UPDATE SET
                month=excluded.month,
                pr_followers=excluded.pr_followers,
                spend_usd=excluded.spend_usd,
                pr_filename=excluded.pr_filename,
                uploaded_by=excluded.uploaded_by,
                uploaded_at=excluded.uploaded_at
            """,
            rows_to_save,
        )
        conn.execute(
            "INSERT INTO uploads(file_type,account,period_start,period_end,filename,stored_path,uploaded_by,uploaded_at,rows_saved,warnings) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "pr",
                "auto" if auto_detect_accounts else account,
                period_start,
                period_end,
                uploaded_file.name,
                stored_path,
                user["username"],
                uploaded_at,
                len(rows_to_save),
                "\n".join(warnings),
            ),
        )
        conn.commit()

    affected_accounts = sorted({str(row[0]) for row in rows_to_save})
    for affected_account in affected_accounts:
        recalc_final(affected_account, period_start, period_end)
    return len(rows_to_save), warnings
