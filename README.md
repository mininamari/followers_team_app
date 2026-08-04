# Novakid Social Reports

Streamlit application for calculating Instagram followers from Meta Business Suite and Novakid PR CSV exports.

## Features

- Team login with role-based access control.
- Meta Business Suite CSV upload.
- Novakid PR CSV upload with automatic account matching.
- Final follower report with CSV and Excel export.
- Manual PR follower overrides for authorized users.
- Upload history for auditing.
- Automatic and manual SQLite database backups.
- Optional Facebook Marketing API sync (campaigns, ads, creatives, spend), matched against manual follower uploads.

## Environment Variables

Create environment variables in your local shell or Railway project. Do not commit real secrets.

| Variable | Required | Description |
| --- | --- | --- |
| `FOLLOWERS_ADMIN_USERNAME` | First setup only | Username for the first admin user when the database has no users. |
| `FOLLOWERS_ADMIN_PASSWORD` | First setup only | Password for the first admin user. Must be at least 8 characters. |
| `FOLLOWERS_DB_PATH` | No | SQLite database path. Default: `data/followers_team.db`. |
| `FOLLOWERS_UPLOAD_DIR` | No | Uploaded CSV storage directory. Default: `data/uploads`. |
| `FOLLOWERS_BACKUP_DIR` | No | Backup storage directory. Default: `backups`. Use `/backups` when your host provides that mounted directory. |
| `FOLLOWERS_BACKUP_RETENTION` | No | Number of database backups to keep. Default: `8`. |
| `FOLLOWERS_BACKUP_INTERVAL_DAYS` | No | Automatic backup interval. Default: `7`. |
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
| Manager | Access business functionality: upload files, edit manual PR values, view/export reports and upload history. |
| Viewer | Read-only access to dashboard, reports, exports, and upload history. |

Existing databases are migrated automatically. Previous `admin` users remain admins, and previous non-admin users become managers.

## Backups

The app creates a SQLite snapshot automatically once per week using SQLite's backup API. This produces a consistent copy without stopping normal application usage.

Admins can also open the `Backups` page to:

- create a manual backup;
- download a backup file;
- view backup creation times and sizes.

Only the newest 8 backups are kept by default. Older backup files are deleted automatically after a new backup is created.

## Facebook Ads integration

The `Facebook Ads` page pulls campaigns, ads, creatives, and spend from the Facebook
Marketing API, and shows a best-effort follower/CPF match against the same manual
Novakid PR uploads used elsewhere in the app. It does not replace the manual PR
upload — Instagram follower counts are not available through the Marketing API,
so that upload stays the source of truth for followers.

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

## Security Notes

- No default credentials are hardcoded in the application.
- No credentials are shown in the UI.
- Passwords are stored as PBKDF2-SHA256 hashes with per-password salts.
- Secrets must be provided through environment variables.
- SQLite databases, backups, uploads, local env files, caches, and temporary files are ignored by Git.

