#!/usr/bin/env python3
import os
import sys
import getpass
import json
import sqlite3
import signal
import time
import logging
import mailbox
import socket
import ssl
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
from imap_tools import MailBox, MailMessageFlags, MailboxLoginError

# Setup Logging to stdout for Docker compatibility (unbuffered)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("gmail-imap-sync")

# Global exit flag
exit_requested = False

def handle_signal(signum, frame):
    global exit_requested
    logger.info(f"Signal {signum} received. Requesting graceful shutdown...")
    exit_requested = True


# ==========================================
# Cryptography Utilities
# ==========================================

def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derives a Fernet key from a passphrase and a salt using PBKDF2HMAC."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))

def encrypt_password(password: str, secret_key: str) -> str:
    """Encrypts a password using a secret key, returning a formatted string."""
    salt = os.urandom(16)
    key = derive_key(secret_key, salt)
    fernet = Fernet(key)
    ciphertext = fernet.encrypt(password.encode())
    return f"enc:{salt.hex()}:{ciphertext.decode()}"

def decrypt_password(enc_password: str, secret_key: str) -> str:
    """Decrypts a formatted encrypted password using the secret key."""
    if not enc_password.startswith("enc:"):
        return enc_password
    
    parts = enc_password.split(":")
    if len(parts) != 3:
        raise ValueError("Invalid encrypted password format. Expected 'enc:salt_hex:ciphertext'")
    
    _, salt_hex, ciphertext = parts
    try:
        salt = bytes.fromhex(salt_hex)
        key = derive_key(secret_key, salt)
        fernet = Fernet(key)
        decrypted = fernet.decrypt(ciphertext.encode())
        return decrypted.decode()
    except Exception as e:
        raise ValueError(
            f"Failed to decrypt password. Please check if your SYNC_ENCRYPTION_KEY is correct. Error: {e}"
        )

def run_encryption_cli():
    """Interactive CLI to encrypt passwords and print configuration lines."""
    print("=== Gmail IMAP Sync Password Encryption Tool ===")
    
    # Check for secret key in environment
    secret_key = os.environ.get("SYNC_ENCRYPTION_KEY")
    if not secret_key:
        print("Warning: SYNC_ENCRYPTION_KEY environment variable is not set.")
        secret_key = getpass.getpass("Enter a secret key to use for encryption: ")
        if not secret_key:
            print("Error: Secret key cannot be empty.")
            sys.exit(1)
        confirm_secret = getpass.getpass("Confirm secret key: ")
        if secret_key != confirm_secret:
            print("Error: Secret keys do not match.")
            sys.exit(1)
            
    p1 = getpass.getpass("Enter the App Password to encrypt: ")
    if not p1:
        print("Error: Password cannot be empty.")
        sys.exit(1)
    p2 = getpass.getpass("Confirm the App Password: ")
    if p1 != p2:
        print("Error: Passwords do not match.")
        sys.exit(1)
        
    try:
        enc_val = encrypt_password(p1, secret_key)
        print("\nEncryption successful!")
        print("Add the following value to your 'app_password' field in config.json:")
        print(f"\n{enc_val}\n")
        if not os.environ.get("SYNC_ENCRYPTION_KEY"):
            print("IMPORTANT: When running the sync service container, you MUST define the environment variable:")
            print("SYNC_ENCRYPTION_KEY with the secret key you just entered.")
    except Exception as e:
        print(f"Error during encryption: {e}")
        sys.exit(1)


# ==========================================
# SQLite State Database
# ==========================================

class StateDatabase:
    """Manages SQLite database for tracking synced email UIDs to prevent duplicate syncs."""
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        self.init_db()
        
    def init_db(self):
        try:
            self.conn = sqlite3.connect(self.db_path)
            # Enable WAL mode for better concurrency and write performance
            self.conn.execute("PRAGMA journal_mode=WAL;")
            with self.conn:
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS synced_messages (
                        folder_name TEXT,
                        uid INTEGER,
                        uid_validity INTEGER,
                        synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (folder_name, uid)
                    );
                """)
        except Exception as e:
            logger.critical(f"Failed to initialize SQLite state database at {self.db_path}: {e}")
            sys.exit(1)
            
    def verify_uid_validity(self, folder_name, current_validity):
        """Checks if UIDVALIDITY matches previous sessions. If not, invalidates cache."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT DISTINCT uid_validity FROM synced_messages WHERE folder_name = ?",
                (folder_name,)
            )
            rows = cursor.fetchall()
            if rows:
                recorded_validity = rows[0][0]
                if recorded_validity != current_validity:
                    logger.warning(
                        f"IMAP UIDVALIDITY changed for folder '{folder_name}' "
                        f"(recorded: {recorded_validity}, current: {current_validity}). "
                        f"Invalidating local state for this folder to avoid inconsistency."
                    )
                    with self.conn:
                        self.conn.execute(
                            "DELETE FROM synced_messages WHERE folder_name = ?",
                            (folder_name,)
                        )
                    return False
            return True
        except Exception as e:
            logger.error(f"Error verifying UIDVALIDITY in state database: {e}")
            return False
            
    def is_message_synced(self, folder_name, uid):
        """Returns True if the message UID is recorded as synced."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT 1 FROM synced_messages WHERE folder_name = ? AND uid = ?",
                (folder_name, uid)
            )
            return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"Error querying state database for UID {uid}: {e}")
            return False
            
    def mark_message_synced(self, folder_name, uid, uid_validity):
        """Saves message UID state into the database."""
        try:
            with self.conn:
                self.conn.execute("""
                    INSERT OR REPLACE INTO synced_messages (folder_name, uid, uid_validity)
                    VALUES (?, ?, ?)
                """, (folder_name, uid, uid_validity))
        except Exception as e:
            logger.error(f"Error recording message UID {uid} in state database: {e}")
            
    def close(self):
        if self.conn:
            self.conn.close()


# ==========================================
# Sync Engine and Business Logic
# ==========================================

def find_trash_folder(mailbox_conn):
    """Finds the name of the IMAP Trash folder using SPECIAL-USE flags or fallbacks."""
    try:
        # Check special flags first (e.g. \Trash)
        for f in mailbox_conn.folder.list():
            if '\\Trash' in f.flags:
                logger.info(f"Detected Trash folder by '\\Trash' flag: '{f.name}'")
                return f.name
        # Fallback to common localized folder names
        for name in ['[Gmail]/Trash', '[Gmail]/Cestino', 'Trash', 'Cestino', 'Deleted Items', 'Deleted']:
            for f in mailbox_conn.folder.list():
                if f.name.lower() == name.lower():
                    logger.info(f"Detected Trash folder by name matching: '{f.name}'")
                    return f.name
    except Exception as e:
        logger.error(f"Error checking folders for Trash: {e}")
    return None

def sync_emails(mailbox_conn, folder_name, maildir_path, state_db, imap_action):
    """Fetches unsynced messages from the server, stores them in Maildir, and runs post-sync actions."""
    global exit_requested
    
    logger.info(f"Selecting folder: '{folder_name}'")
    try:
        mailbox_conn.folder.set(folder_name)
    except Exception as e:
        logger.error(f"Failed to select folder '{folder_name}' on IMAP server: {e}")
        return False
        
    # Retrieve UIDVALIDITY
    try:
        status_info = mailbox_conn.folder.status(folder_name)
        current_uid_validity = status_info.get('UIDVALIDITY', 0)
    except Exception as e:
        logger.warning(f"Error fetching folder status: {e}. Using default UIDVALIDITY 0.")
        current_uid_validity = 0
        
    # Verify UIDVALIDITY
    state_db.verify_uid_validity(folder_name, current_uid_validity)
    
    # Initialize Maildir (creates subdirectories automatically if missing)
    try:
        md = mailbox.Maildir(maildir_path)
    except Exception as e:
        logger.error(f"Failed to initialize Maildir at '{maildir_path}': {e}")
        return False
        
    # Retrieve all server UIDs for the folder
    try:
        all_server_uids = mailbox_conn.uids()
    except Exception as e:
        logger.error(f"Failed to retrieve message UIDs from folder '{folder_name}': {e}")
        return False
        
    # Convert and sort UIDs
    try:
        server_uids = sorted([int(uid) for uid in all_server_uids])
    except ValueError as e:
        logger.error(f"Failed to parse server message UIDs: {e}")
        return False
        
    # Find UIDs that haven't been downloaded yet
    unsynced_uids = [str(uid) for uid in server_uids if not state_db.is_message_synced(folder_name, uid)]
    
    if not unsynced_uids:
        logger.info(f"Folder '{folder_name}' is fully synced. No new messages.")
        return True
        
    logger.info(f"Found {len(unsynced_uids)} unsynced messages in '{folder_name}'. Syncing...")
    
    # Sync messages in chunks
    chunk_size = 50
    success_count = 0
    
    trash_folder_name = None
    if imap_action == 'trash':
        trash_folder_name = find_trash_folder(mailbox_conn)
        if not trash_folder_name:
            logger.warning("Could not identify Trash folder. Fallback post-sync action will be IMAP Delete.")
            
    for i in range(0, len(unsynced_uids), chunk_size):
        if exit_requested:
            logger.info("Sync execution interrupted by shutdown request.")
            break
            
        chunk = unsynced_uids[i:i + chunk_size]
        try:
            # Fetch message objects (fetch raw RFC822 bytes parsing to Email message)
            # mark_seen=False guarantees we don't change unread states unless requested.
            for msg in mailbox_conn.fetch(uids=chunk, mark_seen=False):
                if exit_requested:
                    break
                    
                try:
                    # Write message to Maildir
                    md.add(msg.obj)
                    
                    # Log state
                    state_db.mark_message_synced(folder_name, int(msg.uid), current_uid_validity)
                    success_count += 1
                    
                    # Post-sync IMAP Actions
                    if imap_action == 'read':
                        mailbox_conn.flag(msg.uid, MailMessageFlags.SEEN, True)
                    elif imap_action == 'trash':
                        if trash_folder_name:
                            mailbox_conn.move(msg.uid, trash_folder_name)
                        else:
                            mailbox_conn.delete(msg.uid)
                            
                except Exception as e:
                    logger.error(f"Failed to save message UID {msg.uid}: {e}")
                    
        except Exception as e:
            logger.error(f"Failed to fetch chunk of messages: {e}")
            return False
            
    logger.info(f"Synchronization complete. Successfully downloaded {success_count} messages.")
    return True


# ==========================================
# Config Loader & Daemon Runner
# ==========================================

def load_config(config_path):
    """Loads, default-values, and validates configuration file."""
    if not os.path.exists(config_path):
        logger.critical(f"Configuration file not found at '{config_path}'. Please check docker bind mounts.")
        sys.exit(1)
        
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        logger.critical(f"Failed to parse config.json. Invalid JSON syntax: {e}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Error reading configuration file: {e}")
        sys.exit(1)
        
    # Check required fields
    for field in ["email", "app_password"]:
        if field not in config or not config[field]:
            logger.critical(f"Required configuration field '{field}' is missing or empty.")
            sys.exit(1)
            
    # Set default values
    config.setdefault("imap_host", "imap.gmail.com")
    config.setdefault("label", "INBOX")
    config.setdefault("maildir_path", "/data")
    config.setdefault("retry_interval_minutes", 5)
    config.setdefault("imap_action", "keep")
    
    # Validate imap_action
    valid_actions = ["keep", "read", "trash"]
    if config["imap_action"] not in valid_actions:
        logger.warning(
            f"Invalid imap_action '{config['imap_action']}'. "
            f"Falling back to default 'keep'. (Allowed values: {', '.join(valid_actions)})"
        )
        config["imap_action"] = "keep"
        
    return config

def get_decrypted_password(config):
    """Decrypted configuration password if encrypted."""
    raw_password = config["app_password"]
    if not raw_password.startswith("enc:"):
        return raw_password
        
    secret_key = os.environ.get("SYNC_ENCRYPTION_KEY")
    if not secret_key:
        logger.critical(
            "Encrypted password detected in config.json, but SYNC_ENCRYPTION_KEY "
            "environment variable is not defined. Cannot start sync service."
        )
        sys.exit(1)
        
    try:
        return decrypt_password(raw_password, secret_key)
    except ValueError as e:
        logger.critical(str(e))
        sys.exit(1)

def run_service(config):
    """Starts the connection loop and runs the IMAP sync daemon."""
    global exit_requested
    
    email = config["email"]
    imap_host = config["imap_host"]
    label = config["label"]
    maildir_path = config["maildir_path"]
    retry_interval_minutes = config["retry_interval_minutes"]
    imap_action = config["imap_action"]
    
    app_password = get_decrypted_password(config)
    
    # Initialize Maildir root path
    try:
        os.makedirs(maildir_path, exist_ok=True)
    except Exception as e:
        logger.critical(f"Failed to create target maildir directory '{maildir_path}': {e}")
        sys.exit(1)
        
    # Open local state database in the maildir
    db_path = os.path.join(maildir_path, ".sync_state.db")
    state_db = StateDatabase(db_path)
    
    retry_seconds = int(retry_interval_minutes) * 60
    logger.info(f"Syncing Gmail IMAP folder '{label}' to local Maildir '{maildir_path}'")
    
    while not exit_requested:
        logger.info(f"Connecting to IMAP host {imap_host}...")
        try:
            # Login and select folder
            with MailBox(imap_host).login(email, app_password) as mailbox_conn:
                logger.info("IMAP Login successful.")
                
                # Force initial synchronization
                sync_success = sync_emails(mailbox_conn, label, maildir_path, state_db, imap_action)
                if not sync_success:
                    logger.warning("Initial sync failed. Will retry inside daemon loop.")
                
                # Refreshes connection every 20 minutes to prevent socket dropouts
                idle_refresh_interval = 20 * 60
                connection_start_time = time.time()
                
                logger.info(f"Entering IMAP IDLE real-time listen mode on label '{label}'...")
                
                while not exit_requested:
                    if time.time() - connection_start_time > idle_refresh_interval:
                        logger.info("Refreshing connection to prevent IMAP idle timeout...")
                        break
                        
                    try:
                        # Wait for server notifications (10 seconds timeout allows responsive signal checks)
                        responses = mailbox_conn.idle.wait(timeout=10)
                        if responses:
                            logger.info("IMAP IDLE notification received. Syncing...")
                            sync_emails(mailbox_conn, label, maildir_path, state_db, imap_action)
                    except (socket.timeout, TimeoutError):
                        # standard timeout without events
                        continue
                    except Exception as e:
                        logger.warning(f"Connection issue encountered inside IDLE loop: {e}")
                        raise
                        
        except MailboxLoginError as e:
            # Fatal error (wrong credentials, App Password required, etc.) -> fail-fast
            logger.critical(f"Connection rejected by IMAP server (Invalid Credentials): {e}. "
                            "Please check your login credentials or App Password. Exiting.")
            state_db.close()
            sys.exit(1)
            
        except (socket.gaierror, ConnectionRefusedError, socket.timeout, TimeoutError,
                ConnectionError, ssl.SSLError, OSError) as e:
            # Technical/Network error -> retry loop
            logger.error(f"Network error: {e}. Servicing is still running.")
            if exit_requested:
                break
                
            logger.info(f"Retrying connection in {retry_interval_minutes} minutes ({retry_seconds}s)...")
            sleep_elapsed = 0
            while sleep_elapsed < retry_seconds and not exit_requested:
                time.sleep(5)
                sleep_elapsed += 5
                
        except Exception as e:
            logger.error(f"Unexpected technical error: {e}")
            if exit_requested:
                break
                
            logger.info(f"Retrying connection in {retry_interval_minutes} minutes ({retry_seconds}s)...")
            sleep_elapsed = 0
            while sleep_elapsed < retry_seconds and not exit_requested:
                time.sleep(5)
                sleep_elapsed += 5
                
    state_db.close()
    logger.info("Gmail IMAP Sync service stopped gracefully.")


# ==========================================
# Main CLI Entry Point
# ==========================================

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Gmail IMAP to Maildir Sync Daemon")
    parser.add_argument(
        "--encrypt", "-e",
        action="store_true",
        help="Run the password encryption helper tool"
    )
    parser.add_argument(
        "--config", "-c",
        default="/config/config.json",
        help="Path to the config.json file (default: /config/config.json)"
    )
    
    args = parser.parse_args()
    
    # Register signal handling for container termination
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    if args.encrypt:
        run_encryption_cli()
    else:
        config = load_config(args.config)
        run_service(config)
