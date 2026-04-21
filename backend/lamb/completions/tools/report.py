"""Moodle Report Builder Tool for LAMB

Genera reportes para docentes sobre datos relevantes del curso:
- tareas pendientes en curso
- usuarios con último acceso mayor a X días
- estado de completion de las tareas

La herramienta usa APIs de Moodle que permiten obtener cursos, usuarios y assignments,
con soporte básico de filtros y paginación.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from .moodle import (
    _moodle_ws_url,
    _extract_moodle_user_id_from_request,
    _moodle_ws_get,
    _ts_to_iso,
    _now_ts,
)

logger = logging.getLogger(__name__)

MOODLE_REPORT_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "get_moodle_report_data",
        "description": "Generate Moodle teacher reports for pending assignments, inactive users, and completion overview.",
        "parameters": {
            "type": "object",
            "properties": {
                "report_type": {
                    "type": "string",
                    "description": "Type of report to generate.",
                    "enum": ["summary", "pending_in_course", "inactive_users", "completion_status"],
                },
                "course_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional list of course IDs to include in the report.",
                },
                "days_inactive": {
                    "type": "integer",
                    "description": "Threshold in days for considering a user inactive.",
                    "default": 14,
                },
                "page": {
                    "type": "integer",
                    "description": "Page number for paginated results.",
                    "default": 1,
                },
                "page_size": {
                    "type": "integer",
                    "description": "Number of rows per page for paginated results.",
                    "default": 20,
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of columns to include in each report row.",
                },
            },
            "required": [],
        },
    },
}


def _paginate(items: List[Dict[str, Any]], page: int, page_size: int) -> List[Dict[str, Any]]:
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 20
    start = (page - 1) * page_size
    return items[start:start + page_size]


def _select_columns(item: Dict[str, Any], columns: Optional[List[str]]) -> Dict[str, Any]:
    if not columns:
        return item
    allowed = set(columns)
    return {k: v for k, v in item.items() if k in allowed}


async def _user_is_teacher_in_course(
    ws_url: str,
    token: str,
    course_id: int,
    user_id: int,
) -> bool:
    try:
        enrolled_users = await _moodle_ws_get(
            ws_url,
            token,
            "core_enrol_get_enrolled_users",
            [("courseid", course_id)],
            timeout_s=15.0,
        )
        for user in (enrolled_users or []):
            if int(user.get("id") or -1) != user_id:
                continue
            roles = user.get("roles") or []
            for role in roles:
                role_name = str(role.get("shortname") or role.get("name") or "").lower()
                if any(k in role_name for k in ["teacher", "instructor", "editingteacher"]):
                    return True
        return False
    except Exception as e:
        logger.warning(f"Unable to verify teacher role for user {user_id} in course {course_id}: {e}")
        return False


async def _fetch_submission_status(
    ws_url: str,
    token: str,
    assign_id: int,
    user_id: int,
) -> Dict[str, Any]:
    return await _moodle_ws_get(
        ws_url,
        token,
        "mod_assign_get_submission_status",
        [("assignid", assign_id), ("userid", user_id)],
        timeout_s=15.0,
    )


async def get_moodle_report_data(
    request: Optional[Dict[str, Any]] = None,
    report_type: str = "summary",
    course_ids: Optional[List[int]] = None,
    days_inactive: int = 14,
    page: int = 1,
    page_size: int = 20,
    columns: Optional[List[str]] = None,
) -> str:
    moodle_url = os.getenv("MOODLE_API_URL")
    moodle_token = os.getenv("MOODLE_TOKEN")

    user_id = _extract_moodle_user_id_from_request(request)
    if not user_id:
        return json.dumps({
            "success": False,
            "error": "No Moodle user ID found in request headers",
            "reports": [],
        })

    if not moodle_url or not moodle_token:
        return json.dumps({
            "success": False,
            "error": "MOODLE_API_URL and/or MOODLE_TOKEN not configured",
            "reports": [],
        })

    ws_url = _moodle_ws_url(moodle_url)
    now_ts = _now_ts()

    try:
        courses_data = await _moodle_ws_get(
            ws_url,
            moodle_token,
            "core_enrol_get_users_courses",
            [("userid", user_id)],
            timeout_s=10.0,
        )

        course_ids_set = None
        if course_ids:
            course_ids_set = set(course_ids)

        reports: List[Dict[str, Any]] = []
        for course in (courses_data or []):
            cid = int(course.get("id"))
            if course_ids_set and cid not in course_ids_set:
                continue

            is_teacher = await _user_is_teacher_in_course(ws_url, moodle_token, cid, int(user_id))
            if not is_teacher:
                logger.info(f"Skipping course {cid} because user {user_id} is not teacher")
                continue

            course_name = course.get("fullname")
            enrolled_users = []
            assignments = []
            inactive_users: List[Dict[str, Any]] = []
            pending_assignments: List[Dict[str, Any]] = []
            completion_overview: List[Dict[str, Any]] = []

            try:
                enrolled_users = await _moodle_ws_get(
                    ws_url,
                    moodle_token,
                    "core_enrol_get_enrolled_users",
                    [("courseid", cid)],
                    timeout_s=15.0,
                )
            except Exception as e:
                logger.warning(f"Unable to load enrolled users for course {cid}: {e}")
                enrolled_users = []

            try:
                assigns_data = await _moodle_ws_get(
                    ws_url,
                    moodle_token,
                    "mod_assign_get_assignments",
                    [("courseids[0]", cid)],
                    timeout_s=15.0,
                )
                for course_payload in (assigns_data or {}).get("courses", []):
                    assignments.extend(course_payload.get("assignments", []))
            except Exception as e:
                logger.warning(f"Unable to load assignments for course {cid}: {e}")
                assignments = []

            if report_type in {"summary", "pending_in_course", "completion_status"}:
                for assignment in assignments:
                    duedate = int(assignment.get("duedate") or 0)
                    if duedate and duedate >= now_ts:
                        pending_assignments.append({
                            "assignment_id": assignment.get("id"),
                            "assignment_name": assignment.get("name"),
                            "description": assignment.get("intro"),
                            "duedate": duedate,
                            "due": _ts_to_iso(duedate),
                            "late": False,
                        })

            if report_type in {"summary", "inactive_users"}:
                cutoff_ts = now_ts - int(max(days_inactive, 0)) * 86400
                for user in enrolled_users:
                    lastaccess = int(user.get("lastaccess") or 0)
                    if lastaccess == 0 or lastaccess <= cutoff_ts:
                        inactive_users.append({
                            "id": user.get("id"),
                            "fullname": user.get("fullname"),
                            "email": user.get("email"),
                            "lastaccess": lastaccess,
                            "lastaccess_iso": _ts_to_iso(lastaccess),
                            "days_inactive": None if lastaccess == 0 else (now_ts - lastaccess) // 86400,
                        })

            if report_type in {"summary", "completion_status"}:
                # Limitar la cantidad de usuarios para no saturar Moodle en informes grandes
                user_rows = enrolled_users[:50]
                semaphore = asyncio.Semaphore(8)

                async def fetch_status(assign_id: int, uid: int) -> Dict[str, Any]:
                    async with semaphore:
                        return await _fetch_submission_status(ws_url, moodle_token, assign_id, uid)

                for assignment in assignments[:20]:
                    assign_id = int(assignment.get("id") or 0)
                    if not assign_id:
                        continue

                    statuses = await asyncio.gather(
                        *[
                            fetch_status(assign_id, int(user.get("id")))
                            for user in user_rows
                            if user.get("id")
                        ],
                        return_exceptions=True,
                    )

                    counts = {"submitted": 0, "not_submitted": 0, "graded": 0, "errors": 0}
                    for item in statuses:
                        if isinstance(item, BaseException):
                            counts["errors"] += 1
                            continue
                        status = item or {}
                        submissionstatus = (status.get("submissionstatus") or "").lower()
                        graded = status.get("graded") is True
                        if graded:
                            counts["graded"] += 1
                        if submissionstatus in {"submitted", "graded"}:
                            counts["submitted"] += 1
                        else:
                            counts["not_submitted"] += 1

                    completion_overview.append({
                        "assignment_id": assign_id,
                        "assignment_name": assignment.get("name"),
                        "due": _ts_to_iso(int(assignment.get("duedate") or 0)),
                        "counts": counts,
                    })

            report: Dict[str, Any] = {
                "course_id": cid,
                "course_name": course_name,
                "pending_assignments": _paginate([_select_columns(item, columns) for item in pending_assignments], page, page_size),
                "pending_assignments_count": len(pending_assignments),
                "inactive_users": _paginate([_select_columns(item, columns) for item in inactive_users], page, page_size),
                "inactive_users_count": len(inactive_users),
                "completion_overview": _paginate([_select_columns(item, columns) for item in completion_overview], page, page_size),
            }
            reports.append(report)

        return json.dumps({
            "success": True,
            "user_id": user_id,
            "report_type": report_type,
            "page": page,
            "page_size": page_size,
            "reports": reports,
        })
    except Exception as e:
        logger.error(f"Error in get_moodle_report_data: {e}")
        return json.dumps({
            "success": False,
            "error": str(e),
            "reports": [],
        })
