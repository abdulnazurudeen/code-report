# Setting up GitHub login (OAuth app)

The dashboard signs people in with GitHub. GitHub tells the app who the person is
(login, numeric id, verified emails, organisation membership), and the app maps that
to a role. You create the OAuth app once, then give its id and secret to the server
as environment variables.

You need to be an **owner of the GitHub organisation** (or an admin of the account that
will own the app).

## 1. Decide the public URL

Pick the address people will open, for example `https://code-report.example.com`.

- It must be **HTTPS** in production (a reverse proxy or cloud load balancer is fine).
- The callback URL is that address plus `/auth/callback`:
  `https://code-report.example.com/auth/callback`
- For local testing you may use `http://localhost:8765/auth/callback`
  (GitHub allows plain http for localhost only).

## 2. Create the OAuth app

Create it under the **organisation**, not under a personal account, so it survives
people leaving.

1. Open GitHub, click your avatar, then **Your organizations**, then your organisation.
2. **Settings** -> **Developer settings** (left sidebar, bottom) -> **OAuth Apps**.
3. Click **New OAuth App** (or **Register an application**).
4. Fill in:
   - **Application name**: `Code Report`
   - **Homepage URL**: your public URL from step 1
   - **Application description**: optional
   - **Authorization callback URL**: the callback URL from step 1
   - Leave **Enable Device Flow** unticked.
5. Click **Register application**.

For a separate local-testing app, repeat the steps with the localhost callback URL.
Do not reuse one app for both: an OAuth app has exactly one callback URL.

## 3. Get the client id and secret

1. On the app's page, copy the **Client ID**.
2. Click **Generate a new client secret** and copy it **immediately**; GitHub shows it
   only once. If you lose it, generate a new one and delete the old one.

Never commit either value, never paste them in chat or tickets.

## 4. Let the organisation use the app

Only needed if the organisation restricts third-party apps
(Organisation **Settings** -> **Third-party Access** -> **OAuth app policy**).

- If restrictions are on, the app shows as "pending" until an owner clicks **Approve**.
- Without approval, org membership checks fail and members are treated as outsiders.

## 5. Configure the server

Put these in `.env` (copy from `.env.example`) or in your hosting provider's secret
settings:

```
GITHUB_OAUTH_CLIENT_ID=<client id from step 3>
GITHUB_OAUTH_CLIENT_SECRET=<client secret from step 3>
AUDIT_SESSION_SECRET=<long random string, e.g. output of: python -c "import secrets;print(secrets.token_urlsafe(48))">
PUBLIC_URL=https://code-report.example.com
GITHUB_ORG=<your organisation login>
ADMIN_USERS=github-login-1,github-login-2
```

`AUDIT_SESSION_SECRET` signs the login cookie. Changing it signs everyone out.

## 6. Scopes the app asks for

When someone signs in, GitHub shows them what the app can see:

| Scope | Why |
|---|---|
| `read:user` | Login name, numeric id, display name. |
| `user:email` | Verified emails, so a person can be matched to the user table. |
| `read:org` | Whether the person is a member of your organisation (including private members). |

The app never gets write access and never reads repository code through the user's login.
Repository data is collected with the separate read-only `GITHUB_TOKEN`.

## 7. Check it works

1. Open the public URL and click **Sign in with GitHub**.
2. GitHub asks for authorisation the first time; accept.
3. You land back on the dashboard with your role shown in the corner.
4. A login that is in `ADMIN_USERS` should see the admin tabs. An organisation member
   listed in the user table should see only their own data.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| GitHub page says "redirect_uri is not associated with this application" | The callback URL in the app does not match `PUBLIC_URL` + `/auth/callback` exactly (scheme, host, port, path). |
| Signed in, but "access denied" | The login is not in the user table, not in a role list, and not an org member. Ask an admin to add it. |
| Org members are treated as outsiders | The app is not approved by the organisation (step 4), or the `read:org` scope was not granted. Sign out and in again after approval. |
| Everyone gets signed out after a deploy | `AUDIT_SESSION_SECRET` changed or is not set; set it to a fixed value. |
| Works locally but not when hosted | Hosted site is on http, or a proxy rewrites the host; make sure the proxy forwards the original host and `https` scheme. |

## Rotating the secret

1. In the OAuth app page, generate a new client secret.
2. Update `GITHUB_OAUTH_CLIENT_SECRET` on the server and restart.
3. Delete the old secret in GitHub.
