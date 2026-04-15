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
    import datetime
    moodle_url = os.getenv("MOODLE_API_URL")
    moodle_token = os.getenv("MOODLE_TOKEN")

    user_id = _extract_moodle_user_id_from_request(request)
    if not user_id:
        return json.dumps({
            "success": False,
            "error": "No Moodle user ID found in request headers",
            "assignments": [],
        })

    if not moodle_url or not moodle_token:
        return json.dumps({
            "success": False,
            "error": "MOODLE_API_URL and/or MOODLE_TOKEN not configured",
            "assignments": [],
        })

    ws_url = _moodle_ws_url(moodle_url)

    try:
        # 1. Obtener cursos del usuario
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
        assignments_out = []
        for cid in course_ids:
            course_name = next((c.get("fullname") for c in courses_data if int(c.get("id")) == cid), None)
            # 2. Obtener estados de completion
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
                activities = status.get("statuses", []) if isinstance(status, dict) else []
            except Exception as e:
                activities = []
                logger.warning(f"Error fetching completion for course {cid}: {e}")

            # 3. Obtener assignments del curso
            try:
                async with httpx.AsyncClient() as client:
                    assigns_resp = await client.get(ws_url, params={
                        "wstoken": moodle_token,
                        "wsfunction": "mod_assign_get_assignments",
                        "moodlewsrestformat": "json",
                        "courseids[0]": cid,
                    }, timeout=10.0)
                    assigns_resp.raise_for_status()
                    assigns_data = assigns_resp.json()
                assignments = []
                for course in assigns_data.get("courses", []):
                    assignments.extend(course.get("assignments", []))
            except Exception as e:
                assignments = []
                logger.warning(f"Error fetching assignments for course {cid}: {e}")

            # 4. Cruzar assignments con estados de completion
            for assign in assignments:
                assign_id = assign.get("id")
                assign_name = assign.get("name")
                assign_desc = assign.get("intro")
                duedate = assign.get("duedate")
                # Buscar el estado de completion para este assignment
                completion_state = None
                for act in activities:
                    if act.get("cmid") == assign.get("cmid"):
                        completion_state = act.get("state")
                        break
                # Estado legible
                if completion_state == 1:
                    estado = "completed"
                elif completion_state == 0:
                    estado = "incomplete"
                elif completion_state == 2:
                    estado = "complete (conditions met)"
                else:
                    estado = "unknown"
                # ¿Fuera de plazo?
                now = int(datetime.datetime.utcnow().timestamp())
                fuera_plazo = duedate and now > duedate
                # Formato legible de la fecha
                duedate_str = None
                duedate_time = None
                if duedate:
                    try:
                        dt = datetime.datetime.utcfromtimestamp(duedate)
                        duedate_str = dt.strftime('%Y-%m-%d')
                        duedate_time = dt.strftime('%H:%M UTC')
                    except Exception:
                        duedate_str = str(duedate)
                        duedate_time = None
                # Descripción resumida (primeros 200 caracteres sin saltos de línea)
                descripcion_resumida = None
                if assign_desc:
                    descripcion_resumida = assign_desc.replace('\n', ' ').replace('\r', ' ')
                    if len(descripcion_resumida) > 200:
                        descripcion_resumida = descripcion_resumida[:200] + '...'
                assignments_out.append({
                    "course_id": cid,
                    "course_name": course_name,
                    "assignment_id": assign_id,
                    "assignment_name": assign_name,
                    "assignment_description": assign_desc,
                    "descripcion_resumida": descripcion_resumida,
                    "duedate": duedate,
                    "duedate_str": duedate_str,
                    "duedate_time": duedate_time,
                    "completion_state": completion_state,
                    "completion_state_label": estado,
                    "late": bool(fuera_plazo) if duedate else None
                })

        return json.dumps({
            "success": True,
            "user_id": user_id,
            "assignments": assignments_out,
        })
    except Exception as e:
        logger.error(f"Error in get_moodle_activities_completion_status: {e}")
        return json.dumps({
            "success": False,
            "error": str(e),
            "assignments": [],
        })
