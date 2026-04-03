"""Moodle Augment Prompt Processor

Extends the default prompt processing to support a Moodle-aware placeholder:

- {moodle_user}: best-effort user identifier (prefers Moodle numeric ID when resolvable)

User ID Extraction Priority:
1. request.__openwebui_headers__.x-openwebui-user-email (attempt to resolve to Moodle ID)
2. request.__openwebui_headers__.x-openwebui-user-id
3. request.metadata.user_id
4. request.metadata.lti_user_id
5. request.metadata.lis_person_sourcedid
6. request.metadata.email
7. "default" (fallback)
"""

from typing import Any, Dict, List, Optional

import json
import logging
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load Moodle credentials from the .env file in this directory
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=False)

from lamb.lamb_classes import Assistant

logger = logging.getLogger(__name__)


def _moodle_ws_url(moodle_url: str) -> str:
    if "server.php" in moodle_url:
        return moodle_url
    return f"{moodle_url.rstrip('/')}/webservice/rest/server.php"


def _resolve_moodle_user_id_from_email(email: str) -> Optional[str]:
    """Best-effort mapping from email to Moodle numeric user ID."""
    moodle_url = os.getenv("MOODLE_API_URL")
    moodle_token = os.getenv("MOODLE_TOKEN")

    if not (moodle_url and moodle_token):
        return None

    ws_url = _moodle_ws_url(moodle_url)
    params = {
        "wstoken": moodle_token,
        "wsfunction": "core_user_get_users_by_field",
        "moodlewsrestformat": "json",
        "field": "email",
        "values[0]": email,
    }

    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(ws_url, params=params)
            response.raise_for_status()
            data = response.json()

        if isinstance(data, list) and data and isinstance(data[0], dict) and "id" in data[0]:
            return str(data[0]["id"])
    except Exception as e:
        logger.warning(f"Unable to resolve Moodle user ID for email '{email}': {e}")

    return None


def _has_vision_capability(assistant: Assistant) -> bool:
    if not assistant:
        return False

    metadata_str = getattr(assistant, "metadata", None) or getattr(assistant, "api_callback", None)
    if not metadata_str:
        return False

    try:
        metadata = json.loads(metadata_str)
        capabilities = metadata.get("capabilities", {})
        return capabilities.get("vision", False)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False


def _extract_requested_user_id(text: str) -> Optional[str]:
    """Extract a numeric user ID mentioned in the user text.

    Supports multiple Spanish/English patterns such as:
    - "mi id es 123"
    - "id: 123"
    - "mi identificador es 123"
    - "my id is 123"
    - "usuario 123" / "user 123"
    - "id=123" / "id 123"
    """
    patterns = [
        # Common patterns with explicit "id" or "identificador"
        r"\b(?:id|identificador)\b\s*(?:[:=]|es|is)?\s*(\d+)\b",
        r"\b(?:mi\s+id|my\s+id)\b\s*(?:es|is)?\s*(\d+)\b",
        # Patterns using "usuario" / "user"
        r"\b(?:usuario|user)\b\s*(?:id)?\s*(?:[:=]|es|is)?\s*(\d+)\b",
        r"\b(?:mi\s+usuario|my\s+user)\b\s*(?:es|is)?\s*(\d+)\b",
        # Some users might write like "id=123" or "id 123" without spaces
        r"\b(?:id)\s*[=:]?\s*(\d+)\b",
    ]

    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def _extract_user_id(request: Dict[str, Any]) -> str:
    openwebui_headers = request.get("__openwebui_headers__", {}) or {}

    # Prefer email resolution (gives numeric Moodle IDs)
    email = openwebui_headers.get("x-openwebui-user-email") or openwebui_headers.get("X-OpenWebUI-User-Email")
    if email:
        resolved = _resolve_moodle_user_id_from_email(str(email))
        if resolved and resolved.isdigit():
            return resolved

    # Fallback to user-id header only if it looks numeric (not a UUID)
    owui_user_id = openwebui_headers.get("x-openwebui-user-id") or openwebui_headers.get("X-OpenWebUI-User-Id")
    if owui_user_id:
        user_id_str = str(owui_user_id).strip()
        if user_id_str.isdigit():
            return user_id_str

    # Do not fallback to request metadata (untrusted by users)
    return "default"


def prompt_processor(
    request: Dict[str, Any],
    assistant: Optional[Assistant] = None,
    rag_context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Moodle-aware prompt processor that extends simple_augment.

    Supports all placeholders from simple_augment plus:
    - {moodle_user}
    """
    logger.debug("moodle_augment: Starting prompt processing")

    messages = request.get("messages", [])
    if not messages:
        return messages

    last_message = messages[-1]["content"]
    processed_messages: List[Dict[str, str]] = []

    if not assistant:
        return messages

    if assistant.system_prompt:
        processed_messages.append({"role": "system", "content": assistant.system_prompt})

    processed_messages.extend(messages[:-1])

    if not assistant.prompt_template:
        processed_messages.append(messages[-1])
        return processed_messages

    template = assistant.prompt_template
    # Always resolve the Moodle user ID from the trusted request headers.
    # This ensures we never use an ID provided by the user prompt.
    moodle_user = _extract_user_id(request)

    uses_moodle_user = "{moodle_user}" in template
    has_vision = _has_vision_capability(assistant)

    if isinstance(last_message, list):
        user_input_text = " ".join([item.get("text", "") for item in last_message if item.get("type") == "text"])
    else:
        user_input_text = str(last_message)

    # If the user explicitly provides a different user ID than their authenticated one, refuse.
    header_id = moodle_user if moodle_user and moodle_user.isdigit() else None
    requested_id = _extract_requested_user_id(user_input_text)
    if header_id and requested_id and requested_id != header_id:
        refusal = (
            "Lo siento, no puedo proporcionar información de otros usuarios. "
            "Por favor, utiliza tu propia cuenta para obtener tus datos."
        )
        processed_messages.append({"role": messages[-1]["role"], "content": refusal})
        return processed_messages

    if isinstance(last_message, list) and has_vision:
        augmented_content = []

        augmented_text = template.replace("{user_input}", "\n\n" + user_input_text + "\n\n")
        if uses_moodle_user:
            augmented_text = augmented_text.replace("{moodle_user}", moodle_user)
        else:
            # Ensure the model calls the Moodle tool using the current user's headers (no user_id from prompt)
            augmented_text += (
                "\n\n[IMPORTANTE] Para respuestas sobre cursos, llama a la herramienta `get_moodle_courses()` "
                "sin pasar ningún user_id; el sistema usará el ID Moodle del usuario actual obtenido de las cabeceras. "
                "No inventes ni uses IDs proporcionados por el usuario."
            )

        if rag_context:
            context = json.dumps(rag_context)
            augmented_text = augmented_text.replace("{context}", "\n\n" + context + "\n\n")
        else:
            augmented_text = augmented_text.replace("{context}", "")

        augmented_content.append({"type": "text", "text": augmented_text})
        for item in last_message:
            if item.get("type") != "text":
                augmented_content.append(item)

        processed_messages.append({"role": messages[-1]["role"], "content": augmented_content})
        logger.debug("moodle_augment: Prompt processing complete")
        return processed_messages

    prompt = template.replace("{user_input}", "\n\n" + user_input_text + "\n\n")
    if uses_moodle_user:
        prompt = prompt.replace("{moodle_user}", moodle_user)
    else:
        # Ensure the model calls the Moodle tool using the current user's headers (no user_id from prompt)
        prompt += (
            "\n\n[IMPORTANTE] Para respuestas sobre cursos, llama a la herramienta `get_moodle_courses()` "
            "sin pasar ningún user_id; el sistema usará el ID Moodle del usuario actual obtenido de las cabeceras. "
            "No inventes ni uses IDs proporcionados por el usuario."
        )

    if rag_context:
        context = json.dumps(rag_context)
        prompt = prompt.replace("{context}", "\n\n" + context + "\n\n")
    else:
        prompt = prompt.replace("{context}", "")

    processed_messages.append({"role": messages[-1]["role"], "content": prompt})
    logger.debug("moodle_augment: Prompt processing complete")
    return processed_messages
