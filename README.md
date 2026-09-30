# MARK/01 — Social Growth System

Streamlit application for calculating Instagram followers from Meta Business Suite exports and Facebook Ads API data.

## Features

- Team login with role-based access control.
- Meta Business Suite CSV upload.
- Paid followers and spend for the same period are loaded from the Facebook Ads API automatically on every Meta upload.
- Paid-only ads without a Meta publication match increase paid and total without reducing organic.
- Final follower report with CSV and Excel export.
- Manual paid follower overrides for authorized users.
- Upload history for auditing.
- Automatic and manual SQLite database backups.
- Facebook Marketing API sync (campaigns, ads, creatives, spend, Instagram follows).
- Optional Instagram Graph API sync of every post (reach, views, likes, comments, saves, shares, follows), joined with followers calculated from uploads on the `Organic posts` page.

## Environment Variables

Create environment variables in your local shell or Railway project. Do not commit real secrets.

| Variable | Required | Description |
| --- | --- | --- |
| `FOLLOWERS_ADMIN_USERNAME` | First setup only | Username for the first admin user when the database has no users. |
| `FOLLOWERS_ADMIN_PASSWORD` | First setup only | Password for the first admin user. Must be at least 8 characters. |
| `FOLLOWERS_DB_PATH` | No | SQLite database path. Default: `data/followers_team.db`. |
| `DATABASE_URL` | Recommended on Railway | PostgreSQL connection URL. When set, PostgreSQL is used instead of SQLite. Keep it in Railway variables only. |
| `FOLLOWERS_UPLOAD_DIR` | No | Uploaded CSV storage directory. Default: `data/uploads`. |
| `FOLLOWERS_BACKUP_DIR` | No | Backup storage directory. Default: `backups`. Use `/backups` when your host provides that mounted directory. |
| `FOLLOWERS_BACKUP_RETENTION` | No | Number of database backups to keep. Default: `8`. |
| `FOLLOWERS_BACKUP_INTERVAL_DAYS` | No | Automatic backup interval. Default: `7`. |
| `SESSION_IDLE_TIMEOUT_MINUTES` | No | Sign out an inactive browser session after this many minutes. Default: `60`. |
| `SESSION_MAX_AGE_HOURS` | No | Require a fresh login after this many hours, even if active. Default: `12`. |
| `LOGIN_MAX_ATTEMPTS` | No | Failed attempts allowed during the login window. Default: `5`. |
| `LOGIN_ATTEMPT_WINDOW_MINUTES` | No | Window used to count failed login attempts. Default: `15`. |
| `LOGIN_LOCKOUT_MINUTES` | No | Temporary login lock after too many failures. Default: `15`. |
| `META_APP_ID` | No | Meta App ID, only needed if you later add token refresh. |
| `META_APP_SECRET` | No | Meta App Secret, only needed if you later add token refresh. |
| `META_ACCESS_TOKEN` | No | System User access token for the Facebook Marketing API. Without it, the "Facebook Ads" page shows a "not configured" message. |
| `META_GRAPH_API_VERSION` | No | Graph API version. Default: `v21.0`. |

## Local Development

```bash
pip install -r requirements.txt
export FOLLOWERS_ADMIN_USERNAME="your-admin-user"
export FOLLOWERS_ADMIN_PASSWORD="change-this-password"
streamlit run app.py
```

On first startup, the app creates the first admin only from the environment variables above. Credentials are never displayed on the login screen.

## Roles And Permissions

| Role | Permissions |
| --- | --- |
| Admin | Access all features, create/edit/delete users, assign roles, manage backups. |
| Manager | Access business functionality: upload files, edit manual paid values, view/export reports and upload history. |
| Viewer | Read-only access to dashboard, reports, exports, and upload history. |

Existing databases are migrated automatically. Previous `admin` users remain admins, and previous non-admin users become managers.

## Follower calculation

Every Meta upload syncs all active Facebook ad accounts for exactly the same period and sums each ad's Instagram follows (`instagram_profile_follow`) and spend. By team convention the ad name is the Meta `ID публикации`, so it is matched first; the post promoted by the creative (`effective_instagram_media_id`) is the fallback. Matched rows split the Meta total into paid and organic. Ads without a Meta match are kept as paid-only rows under their ad name: they add the same value to paid and total, while existing organic stays unchanged. Every calculated row and monthly total therefore follows `total = paid + organic`.

The Instagram account of an ad comes from the matched Meta publication, then from the synced Instagram post, the ad creative's Instagram account, or the `[r:xx]` campaign naming convention. Ads whose account cannot be determined are skipped and listed in the upload warnings. Only accounts present in the Meta upload are updated. Their paid rows for that period are replaced by API data, while manual corrections stay in effect. If any ad account fails to sync, paid rows are left unchanged.

`Upload Meta` → **Reload paid for an uploaded period** re-runs this for a period that was already uploaded, for example after late attributed follows.

## Backups

The app creates a SQLite snapshot automatically once per week using SQLite's backup API. This produces a consistent copy without stopping normal application usage.

Admins can also open the `Backups` page to:

- create a manual backup;
- download a backup file;
- view backup creation times and sizes.

Only the newest 8 backups are kept by default. Older backup files are deleted automatically after a new backup is created.

## Facebook Ads integration

The `Facebook Ads` page pulls campaigns, ads, creatives, and spend from the Facebook
Marketing API. The same sync provides paid followers for every Meta upload (see
"Follower calculation"), which replaced the former manual Novakid PR upload.

To enable it, you need a **System User access token** from your own Meta Business
Manager (no public App Review required, since this only reads your own ad accounts):

1. In [Meta Business Manager](https://business.facebook.com), go to **Business Settings → Users → System Users**.
2. Create a system user with the **Employee** or **Admin** role.
3. Under **Assign Assets**, give that system user access to the ad account(s) you want to sync.
4. Click **Generate New Token**, select your app (create one under **Business Settings → Accounts → Apps** if you don't have one yet), and grant the `ads_read` scope.
5. Copy the generated token — it does not expire like a personal user token, but treat it as a secret.
6. Set it as `META_ACCESS_TOKEN` in your environment (see the table above). `META_APP_ID` / `META_APP_SECRET` are optional and only needed if you add token refreshing later.
7. In the app, open `Facebook Ads` → **Рекламные аккаунты Facebook** and map each `act_XXXXXXXXX` ad account ID to its Novakid region, then click **Sync now**.

Google Slides export of selected campaigns/creatives is a planned follow-up, not yet implemented.

## Instagram posts (Organic posts page)

The `Organic posts` page loads statistics for every Instagram post through the
Instagram Graph API and shows them next to the total / paid / organic followers
calculated from the Meta uploads and ad data. API posts are matched to uploaded rows
by publication ID (the Meta export `ID публикации` is the Instagram media ID),
falling back to the permalink. Uploaded posts that were not synced and synced
posts without an upload both stay in the table, marked by the `Source` column.

It uses the same `META_ACCESS_TOKEN`. Add these permissions to the system user
token and assign the Novakid Facebook Pages to the system user:

- `instagram_basic`
- `instagram_manage_insights`
- `pages_show_list`
- `pages_read_engagement`

Then open `Organic posts` → **Instagram API sync**, click **Find Instagram
accounts**, choose a publication period (up to 365 days) and click **Load posts
statistics**. Metrics Meta does not provide for a format (for example `follows`
for Reels) stay empty; per-post API errors are shown in the `API note` column.

## Railway Deployment

1. Create a Railway service from this repository.
2. Add the required first-setup variables:
   - `FOLLOWERS_ADMIN_USERNAME`
   - `FOLLOWERS_ADMIN_PASSWORD`
3. Optional: add persistent volume paths and configure:
   - `FOLLOWERS_DB_PATH=/data/followers_team.db`
   - `FOLLOWERS_UPLOAD_DIR=/data/uploads`
   - `FOLLOWERS_BACKUP_DIR=/backups`
4. Use this start command:

```bash
streamlit run app.py --server.port $PORT --server.address 0.0.0.0
```

5. After the first admin user exists, rotate or remove first-setup environment variables according to your team's secret management policy.

### Migrating SQLite to Railway PostgreSQL

Create a PostgreSQL service first, but do not switch the application until the migration succeeds. Run the one-time migration against a recent SQLite backup from a secure environment:

```bash
SOURCE_SQLITE_PATH=/path/to/followers_backup.db \
DATABASE_URL='postgresql://...' \
python3 scripts/migrate_sqlite_to_postgres.py
```

The script refuses to write to a non-empty PostgreSQL database and verifies row counts table by table. After it succeeds, add Railway's PostgreSQL `DATABASE_URL` reference to the application service and redeploy. Retain the SQLite backup until the new deployment has been verified.

## Security Notes

- No default credentials are hardcoded in the application.
- No credentials are shown in the UI.
- Passwords are stored as PBKDF2-SHA256 hashes with per-password salts.
- Secrets must be provided through environment variables.
- SQLite databases, backups, uploads, local env files, caches, and temporary files are ignored by Git.
