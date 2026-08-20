"""Backend UI State Management (JSON Persistence).

Provides a lightweight way to store chat history, document records, and settings
in the data/ directory, avoiding the need for a full SQL database for this
local, zero-cost RAG architecture.

Concurrency safety: all writes use a cross-platform advisory file lock
(see file_lock) to prevent corruption when multiple requests modify state
simultaneously.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from src.core.config import DATA_DIR, settings
from src.core.file_lock import LockMode, locked

logger = logging.getLogger(__name__)


class UIStateManager:
    """Manages JSON file-based persistence for the UI."""

    def __init__(self) -> None:
        self.data_dir = DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.chats_file = self.data_dir / "chats.json"
        self.messages_file = self.data_dir / "messages.json"
        self.documents_file = self.data_dir / "documents.json"
        self.settings_file = self.data_dir / "settings.json"

    def _load_json(self, path: Path, default: Any) -> Any:
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    with locked(f, LockMode.SHARED):
                        return json.load(f)
            except Exception as e:
                logger.error("Failed to load %s: %s", path.name, e)
        return default

    def _save_json(self, path: Path, data: Any) -> None:
        """Atomic write with advisory file locking.

        Writes to a temp file first, then renames it into place. This
        prevents readers from seeing a partially-written file.
        """
        try:
            content = json.dumps(data, indent=2, ensure_ascii=False)
            # Write to a temp file in the same directory, then atomically rename
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=path.stem
            )
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    with locked(f, LockMode.EXCLUSIVE):
                        f.write(content)
                        f.flush()
                # os.replace is atomic on the same filesystem on POSIX *and*
                # Windows, unlike Path.rename which fails on Windows when the
                # destination already exists.
                os.replace(tmp_path, path)
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception as e:
            logger.error("Failed to save %s: %s", path.name, e)

    # --- Chats ---

    def get_chats(self) -> list[dict[str, Any]]:
        return self._load_json(self.chats_file, [])

    def save_chats(self, chats: list[dict[str, Any]]) -> None:
        self._save_json(self.chats_file, chats)

    def create_chat(self, chat: dict[str, Any]) -> None:
        chats = self.get_chats()
        chats.insert(0, chat)
        self.save_chats(chats)

    def update_chat(self, chat_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        chats = self.get_chats()
        for i, c in enumerate(chats):
            if c["id"] == chat_id:
                chats[i].update(updates)
                self.save_chats(chats)
                return chats[i]
        return None

    def delete_chat(self, chat_id: str) -> None:
        chats = [c for c in self.get_chats() if c["id"] != chat_id]
        self.save_chats(chats)

        # Also delete messages for this chat
        messages = self.get_all_messages()
        if chat_id in messages:
            del messages[chat_id]
            self.save_all_messages(messages)

    # --- Messages ---

    def get_all_messages(self) -> dict[str, list[dict[str, Any]]]:
        return self._load_json(self.messages_file, {})

    def save_all_messages(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self._save_json(self.messages_file, data)

    def get_messages(self, chat_id: str) -> list[dict[str, Any]]:
        return self.get_all_messages().get(chat_id, [])

    def add_message(self, chat_id: str, message: dict[str, Any]) -> None:
        all_messages = self.get_all_messages()
        if chat_id not in all_messages:
            all_messages[chat_id] = []
        all_messages[chat_id].append(message)
        self.save_all_messages(all_messages)

    def set_message_feedback(
        self, chat_id: str, message_id: str, feedback: str | None, comment: str | None = None
    ) -> bool:
        """Persist thumbs up/down (or clear it) and comment on a stored message.

        Returns True if the message was found and updated. ``feedback`` of None
        removes any existing rating (toggle-off).
        """
        all_messages = self.get_all_messages()
        for message in all_messages.get(chat_id, []):
            if message.get("id") == message_id:
                if feedback is None:
                    message.pop("feedback", None)
                    message.pop("feedbackComment", None)
                else:
                    message["feedback"] = feedback
                    if comment:
                        message["feedbackComment"] = comment
                self.save_all_messages(all_messages)
                return True
        return False

    # --- Documents ---

    def get_documents(self) -> list[dict[str, Any]]:
        return self._load_json(self.documents_file, [])

    def add_document(self, document: dict[str, Any]) -> None:
        docs = self.get_documents()
        docs.insert(0, document)
        self._save_json(self.documents_file, docs)

    # --- Settings ---

    def get_settings(self) -> dict[str, Any]:
        default_settings = {
            "endpoint": "/api",
            "model": "Auto-routed via ProviderRouter",
            "temperature": 0.0,
            "topP": "1.0",
            "contextLength": "8192",
            "streamResponses": False,
            "autoSync": True,
            "theme": "dark",
        }
        saved = self._load_json(self.settings_file, {})
        default_settings.update(saved)
        return default_settings

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self._save_json(self.settings_file, settings)
        return settings

# Global singleton
state_manager = UIStateManager()
