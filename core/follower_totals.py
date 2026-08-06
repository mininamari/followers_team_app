from __future__ import annotations


def period_follower_rows(conn, account: str, period_start: str, period_end: str) -> list[dict]:
    """Return one effective follower row for every Meta, PR, or manual publication ID."""
    key = (account, period_start, period_end)
    meta_by_id = {
        str(row["publication_id"]): row
        for row in conn.execute(
            "SELECT * FROM meta_publications WHERE account=? AND period_start=? AND period_end=?",
            key,
        ).fetchall()
    }
    pr_by_id = {
        str(row["publication_id"]): row
        for row in conn.execute(
            "SELECT * FROM pr_ads WHERE account=? AND period_start=? AND period_end=?",
            key,
        ).fetchall()
    }
    override_by_id = {
        str(row["publication_id"]): row
        for row in conn.execute(
            "SELECT * FROM follower_overrides WHERE account=? AND period_start=? AND period_end=?",
            key,
        ).fetchall()
    }

    result: list[dict] = []
    for publication_id in sorted(set(meta_by_id) | set(pr_by_id) | set(override_by_id)):
        meta = meta_by_id.get(publication_id)
        pr = pr_by_id.get(publication_id)
        override = override_by_id.get(publication_id)
        raw_total = int(meta["meta_followers"]) if meta else 0
        imported_paid = int(pr["pr_followers"]) if pr else 0
        manual_paid = int(override["manual_pr_followers"]) if override else None
        paid = manual_paid if manual_paid is not None else imported_paid
        organic = max(0, raw_total - paid) if meta else 0
        total = organic + paid
        result.append(
            {
                "publication_id": publication_id,
                "meta": meta,
                "pr": pr,
                "override": override,
                "raw_total": raw_total,
                "imported_paid": imported_paid,
                "manual_paid": manual_paid,
                "paid": paid,
                "organic": organic,
                "total": total,
                "paid_only": meta is None,
            }
        )
    return result


def period_follower_totals(conn, account: str, period_start: str, period_end: str) -> dict[str, int]:
    rows = period_follower_rows(conn, account, period_start, period_end)
    return {
        "total": sum(row["total"] for row in rows),
        "paid": sum(row["paid"] for row in rows),
        "organic": sum(row["organic"] for row in rows),
        "paid_only": sum(row["paid"] for row in rows if row["paid_only"]),
    }
