# Gmail IMAP Sync

A lightweight, robust, containerized Python service that connects to Gmail via IMAP, downloads emails under a specific label/folder into a local Maildir directory, and uses IMAP IDLE to monitor and sync new messages in real-time.

---

## Features

- **Real-Time Syncing**: Uses IMAP IDLE to listen for incoming emails and download them immediately.
- **Fail-Safe Startup**: Errors such as missing configs or authentication issues stop the container immediately (fail-fast).
- **Network Resilience**: Reconnection loop handles network dropouts or timeouts, retrying periodically (default: 5 minutes).
- **SQLite State Tracking**: Tracks downloaded message UIDs in an SQLite database (`.sync_state.db`) stored inside the Maildir directory. Survives restarts and prevents duplicates.
- **Post-Sync Actions**: Supports leaving messages untouched, marking them read, moving them to trash, or moving them to a custom folder (marked as read) on the IMAP server.
- **Flexible Search**: Choose between syncing all messages in a folder or only unread ones.
- **Graceful Shutdown**: Properly intercepts `SIGTERM` and `SIGINT` signals for safe daemon shutdowns.
- **Rootless & Secure**: Designed to run as a non-root user (`appuser`, UID/GID `1000`) by default, and fully supports user overriding via standard Docker user flags.
- **Credential Protection**: Supports both plain text and encrypted credentials (using AES-128 via Fernet) with a dedicated CLI encryption helper.

---

## Configuration

The service expects a configuration file located at `/config/config.json` inside the container. 

### Configuration Structure (`config.json`)

Here is an example structure. You can use the provided [config.json.example](config.json.example) as a starting point.

```json
{
  "imap_host": "imap.gmail.com",
  "email": "your_email@gmail.com",
  "app_password": "your_app_password_or_encrypted_string",
  "label": "INBOX",
  "maildir_path": "/data/maildir",
  "retry_interval_minutes": 5,
  "sync_mode": "idle",
  "idle_fallback": true,
  "idle_refresh_interval_minutes": 20,
  "polling_interval_minutes": 5,
  "http_endpoint_enabled": false,
  "http_endpoint_port": 5000,
  "imap_action": "read",
  "move_to_folder": "",
  "search_criteria": "unseen"
}
```

### Settings Reference

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `email` | String | *Required* | Gmail username (e.g. `user@gmail.com`). |
| `app_password` | String | *Required* | Gmail 16-character App Password (plain text or encrypted prefix `enc:`). |
| `imap_host` | String | `imap.gmail.com` | The IMAP server host. |
| `label` | String | `INBOX` | GMail folder/label to sync. |
| `maildir_path` | String | `/data` | Destination path where the Maildir structure will be built. (By defaulting to `/data`, the mountpoint itself becomes the Maildir). |
| `retry_interval_minutes` | Integer | `5` | Minutes to wait before retrying the connection after network drops. |
| `sync_mode` | String | `idle` | Connection mode to use. Options: `idle` (real-time IMAP IDLE) or `polling` (check at regular intervals). |
| `idle_fallback` | Boolean | `true` | If `true`, falls back to `polling` mode if the IMAP server does not support IDLE. If `false`, stops the service. |
| `idle_refresh_interval_minutes` | Integer | `20` | Interval to refresh the IMAP connection in IDLE mode to prevent socket timeouts. |
| `polling_interval_minutes` | Integer | `5` | Wait time between sync attempts when `sync_mode` is set to `polling`. |
| `http_endpoint_enabled` | Boolean | `false` | Enable or disable the local HTTP server to trigger manual synchronization. |
| `http_endpoint_port` | Integer | `5000` | Port number for the local HTTP server if enabled. |
| `imap_action` | String | `read` | Action to run on server after sync. Options: `keep` (do nothing), `read` (mark as read), `trash` (move to Trash folder, or delete as fallback), `move` (mark as read and move to folder specified in `move_to_folder`). |
| `move_to_folder` | String | `""` | IMAP destination folder for the `move` action (e.g. `"INBOX/Archived"`, `"[Gmail]/All Mail"`). Required when `imap_action` is `move`. |
| `search_criteria` | String | `unseen` | Controls which messages to sync. Options: `unseen` (only unread messages), `all` (all messages in the folder). |

### Understanding `maildir_path` & Volume Mounting

You have two main strategies for configuring where emails are downloaded and how volumes are mounted:

#### Strategy A: Direct Folder Isolation (Recommended for Security)
Mount the specific user's maildir folder directly to the container's `/data` directory.
- **`config.json`**: Omit `"maildir_path"` (it defaults to `/data`).
- **Docker Mount**: `-v /var/mail/maildir/marco@miodominio.it:/data`
- **Result**: The container downloads messages directly into `/data` (resulting on the host in `/var/mail/maildir/marco@miodominio.it/new`, `/cur`, `/tmp`). The container is fully isolated and has no visibility into neighboring mailbox folders.

#### Strategy B: Shared Directory Root
Mount the parent maildir directory containing all mailboxes, and specify the user subfolder in the configuration.
- **`config.json`**: Set `"maildir_path": "/data/marco@miodominio.it"`.
- **Docker Mount**: `-v /var/mail/maildir:/data`
- **Result**: The container creates the user subfolder inside the mount and downloads messages there. *Note: This is less secure as it gives the container namespace visibility over all users' mailboxes on the host.*

---

## Manual Synchronization via HTTP

The daemon provides a simple, built-in HTTP server using FastAPI that allows you to trigger a manual synchronization independently of the automatic `idle` or `polling` loops. 

To enable this feature, set `"http_endpoint_enabled": true` in your `config.json`. You can optionally change the port using `"http_endpoint_port": 5000`.

### Triggering a Sync

You can invoke the synchronization by making a simple HTTP GET request to the `/start-sync` endpoint:

```bash
curl http://localhost:5000/start-sync
```

**Concurrency Protection**: The endpoint is fully thread-safe. If a synchronization is already in progress (either triggered by the daemon or another manual request), the endpoint will immediately return an `ignored` status without interfering with the running sync.

---

## Running with Docker Compose

An example [docker-compose.yml](docker-compose.yml) is included in the project directory.

```yaml
services:
  gmail-sync:
    image: skyma3x/gmail-imap-sync:latest
    container_name: gmail-imap-sync
    restart: unless-stopped
    environment:
      - SYNC_ENCRYPTION_KEY=my-super-secret-encryption-passphrase # Remove if using plain-text app_password
    volumes:
      - ./config/config.json:/config/config.json:ro
      - ./maildir/marco@mydomain.com:/data
```

To start the service using Compose:
1. Create a `config` directory next to your `docker-compose.yml` and place your `config.json` inside it.
2. Run the compose up command:
   ```bash
   docker compose up -d
   ```

---

## Security & Running as Non-Root

For security reasons, the container does not run as root. It defines a system user `appuser` with UID `1000` and GID `1000` by default. 

When you bind mount volumes (`/config` and `/data`), make sure the directories on the host are readable/writable by the container user.

### Best Practice: Mount Specific Mailbox Folders
For optimal security (Principle of Least Privilege), do NOT mount the root maildir directory containing all user accounts (e.g. `/var/mail/maildir`). Instead, mount the specific user's folder directly (e.g. `/var/mail/maildir/marco@miodominio.it`) as the `/data` volume inside the container. This isolates the container's namespace completely, preventing any potential security breach from traversing to other user accounts.

### Custom UID/GID (Overriding User)

If your host folders are owned by another user (e.g. UID `1001`), run the container specifying the user ID using standard Docker `-u` or `--user` flags:

```bash
docker run -d \
  --name gmail-sync \
  -u 1001:1001 \
  -v /path/to/host/config:/config:ro \
  -v /path/to/host/maildir/marco@miodominio.it:/data \
  gmail-imap-sync
```

---

## App Password Encryption (Optional)

Instead of saving your App Password in plain text inside `config.json`, you can encrypt it.

### Step 1: Encrypt the Password
Run the encryption command interactively inside the running container (or by starting a temporary container):

```bash
# Set your encryption key in your host environment
export SYNC_ENCRYPTION_KEY="my-super-secret-encryption-passphrase"

# Run the encryption CLI
docker run -it --rm \
  -e SYNC_ENCRYPTION_KEY \
  -v /path/to/host/config:/config \
  gmail-imap-sync python sync_service.py --encrypt
```

Enter your App Password when prompted. The tool will output an encrypted string starting with `enc:`, for example:
`enc:ab3de09f7a...:gAAAAAB...`

### Step 2: Update Configuration
Save the generated `enc:...` string as the value for `"app_password"` in your `config.json`.

### Step 3: Run the Container
Start the container while providing the `SYNC_ENCRYPTION_KEY` environment variable:

```bash
docker run -d \
  --name gmail-sync \
  -e SYNC_ENCRYPTION_KEY="my-super-secret-encryption-passphrase" \
  -v /path/to/host/config:/config:ro \
  -v /path/to/host/maildir:/data \
  gmail-imap-sync
```