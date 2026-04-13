"""Moodle Activities Completion Tool for LAMB

Permite consultar el estado de finalización de todas las actividades de todos los cursos del usuario autenticado (alumno o profesor).
El user_id se extrae siempre de las cabeceras (privacidad garantizada).
"""

import json
import os
import logging
from typing import Any, Dict, Optional

import httpx
from .moodle import _moodle_ws_url, _extract_moodle_user_id_from_request

logger = logging.getLogger(__name__)

MOODLE_ACTIVITIES_COMPLETION_STATUS_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "get_moodle_activities_completion_status",
        "description": "Get completion status for all activities in all courses for the authenticated user (privacy: user_id always from headers)",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

async def get_moodle_activities_completion_status(request: Optional[Dict[str, Any]] = None) -> str:
    """
    Get completion status for all activities in all courses for the authenticated user.
    Uses core_completion_get_activities_completion_status para cada curso.
    Devuelve un JSON con la lista de cursos y el estado de sus actividades.
    """
    moodle_url = os.getenv("MOODLE_API_URL")
    moodle_token = os.getenv("MOODLE_TOKEN")

    user_id = _extract_moodle_user_id_from_request(request)
    if not user_id:
        return json.dumps({
            "success": False,
            "error": "No Moodle user ID found in request headers",
            "activities": [],
        })

    if not moodle_url or not moodle_token:
        return json.dumps({
            "success": False,
            "error": "MOODLE_API_URL and/or MOODLE_TOKEN not configured",
            "activities": [],
        })

    ws_url = _moodle_ws_url(moodle_url)

    try:
        # Obtener cursos del usuario
        async with httpx.AsyncClient() as client:
            courses_resp = await client.get(ws_url, params={
                "wstoken": moodle_token,
                "wsfunction": "core_enrol_get_users_courses",
                "moodlewsrestformat": "json",
                "userid": user_id,
            }, timeout=10.0)
            courses_resp.raise_for_status()
            courses_data = courses_resp.json()

        course_ids = [int(c.get("id")) for c in (courses_data or []) if c.get("id") is not None]
        results = []
        for cid in course_ids:
            try:
                async with httpx.AsyncClient() as client:
                    status_resp = await client.get(ws_url, params={
                        "wstoken": moodle_token,
                        "wsfunction": "core_completion_get_activities_completion_status",
                        "moodlewsrestformat": "json",
                        "courseid": cid,
                        "userid": user_id,
                    }, timeout=10.0)
                    status_resp.raise_for_status()
                    status = status_resp.json()
                results.append({
                    "course_id": cid,
                    "course_name": next((c.get("fullname") for c in courses_data if int(c.get("id")) == cid), None),
                    "activities": status.get("statuses", []) if isinstance(status, dict) else [],
                    "raw_status": status,  # Depuración: resultado crudo de la API
                })
            except Exception as e:
                results.append({
                    "course_id": cid,
                    "error": str(e),
                })
        return json.dumps({
            "success": True,
            "user_id": user_id,
            "courses": results,
        })
    except Exception as e:
        logger.error(f"Error in get_moodle_activities_completion_status: {e}")
        return json.dumps({
            "success": False,
            "error": str(e),
            "activities": [],
        })
