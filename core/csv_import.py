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
    REQUIRED_META,
    now_utc,
)
from core.i18n import tr
from core.database import connect_db
from core.follower_totals import period_follower_rows, period_follower_totals


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
        follower_rows = period_follower_rows(conn, account, period_start, period_end)
        current = now_utc()
        conn.execute(
            "DELETE FROM final_results WHERE account=? AND period_start=? AND period_end=?",
            (account, period_start, period_end),
        )
        for row in follower_rows:
            m = row["meta"]
            pr = row["pr"]
            override = row["override"]
            imported_pr_followers = row["imported_paid"]
            manual_pr_followers = row["manual_paid"]
            pr_followers = row["paid"]
            spend = float(pr["spend_usd"]) if pr else 0.0
            if row["paid_only"]:
                warning = ""
            elif row["paid"] > row["raw_total"]:
                warning = tr(
                    "Paid followers exceed the Meta total; total was expanded to preserve total = paid + organic.",
                    "Paid больше значения Meta; total увеличен, чтобы сохранить total = paid + organic.",
                )
            else:
                warning = ""
            final_followers = row["organic"]
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
                    account, m["account_name"] if m else account, period_start, period_end,
                    m["month"] if m else period_start[:7], m["publication_date"] if m else None, row["publication_id"],
                    m["publication_link"] if m else "", int(m["post_reach"]) if m else 0, row["total"],
                    imported_pr_followers, manual_pr_followers, pr_followers, final_followers, spend, cpf, warning,
                    m["uploaded_by"] if m else None,
                    pr["uploaded_by"] if pr else None, override["updated_by"] if override else None,
                    override["updated_at"] if override else None, current,
                ),
            )
        conn.commit()
    recalc_monthly_totals(account, period_start, period_end)


def recalc_monthly_totals(account: str, period_start: str, period_end: str) -> None:
    """Persist total = paid + organic, treating unmatched paid rows as paid-only."""
    with connect_db() as conn:
        imported = period_follower_totals(conn, account, period_start, period_end)
        imported_total = imported["total"]
        imported_paid = imported["paid"]
        paid_only = imported["paid_only"]
        existing = conn.execute(
            "SELECT manual_total_followers, manual_paid_followers, updated_by FROM monthly_follower_totals WHERE account=? AND period_start=? AND period_end=?",
            (account, period_start, period_end),
        ).fetchone()
        manual_total = existing["manual_total_followers"] if existing else None
        manual_paid = existing["manual_paid_followers"] if existing else None
        paid = int(manual_paid) if manual_paid is not None else imported_paid
        selected_total = int(manual_total) if manual_total is not None else imported_total
        total = max(selected_total, paid)
        conn.execute(
            """
            INSERT INTO monthly_follower_totals(
                account, period_start, period_end, month, imported_total_followers, imported_paid_followers,
                paid_only_followers,
                manual_total_followers, manual_paid_followers, total_followers, paid_followers,
                organic_followers, updated_by, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account, period_start, period_end) DO UPDATE SET
                imported_total_followers=excluded.imported_total_followers,
                imported_paid_followers=excluded.imported_paid_followers,
                paid_only_followers=excluded.paid_only_followers,
                total_followers=excluded.total_followers,
                paid_followers=excluded.paid_followers,
                organic_followers=excluded.organic_followers,
                updated_at=excluded.updated_at
            """,
            (account, period_start, period_end, period_start[:7], imported_total, imported_paid, paid_only,
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

def import_meta(
    uploaded_file,
    user: dict,
    manual_start: Optional[date],
    manual_end: Optional[date],
    paid_from_ads_api: bool = False,
) -> tuple[int, list[str]]:
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

    accounts = {str(account) for account in grouped[META_ACCOUNT_USERNAME_COL].unique()}
    if paid_from_ads_api:
        # Imported lazily: the ads integration builds on this module's helpers.
        from integrations.ads_paid_followers import save_paid_from_ads_api

        _, paid_warnings = save_paid_from_ads_api(period_start, period_end, accounts, user["username"])
        warnings.extend(paid_warnings)
    for account in sorted(accounts):
        recalc_final(account, period_start, period_end)
    return len(rows), warnings
