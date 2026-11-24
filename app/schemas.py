from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, ValidationError


class LecturerRegisterSchema(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    username: Optional[str] = Field(default=None, max_length=40)
    email: str = Field(..., min_length=5, max_length=160)
    password: str = Field(..., min_length=8, max_length=128)
    confirm_password: Optional[str] = None
    department: str = Field(default="Computer Science", max_length=120)


class LecturerLoginSchema(BaseModel):
    identifier: str = Field(..., min_length=1, max_length=160)
    password: str = Field(..., min_length=1, max_length=128)
    remember_me: bool = True


class CourseCreateSchema(BaseModel):
    course_code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=200)
    department: Optional[str] = Field(default=None, max_length=120)
    semester: str = Field(default="Fall 2026", max_length=64)
    lecturer_name: Optional[str] = Field(default=None, max_length=120)
    initial_lecture_title: Optional[str] = Field(default=None, max_length=200)


class CourseStaffAddSchema(BaseModel):
    identifier: str = Field(..., min_length=2, max_length=160)
    role: str = Field(default="ta", pattern="^(ta|co_lecturer)$")


def parse_schema(model_cls: type[BaseModel], payload: dict[str, Any]) -> tuple[Optional[BaseModel], Optional[str]]:
    """Validate payload with Pydantic v2 and return (model_instance, error_message)."""
    try:
        return model_cls.model_validate(payload), None
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"msg": "Invalid request payload."}
        loc = ".".join(str(x) for x in first.get("loc", ()))
        msg = f"{loc}: {first.get('msg')}" if loc else str(first.get("msg"))
        return None, msg


def build_openapi_spec() -> dict[str, Any]:
    """Generate a compact OpenAPI 3.0 specification for ClassPulse core & auth endpoints."""
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "ClassPulse Classroom Intelligence API",
            "version": "2.0.0",
            "description": "Multi-tenant interactive classroom attendance, polls, Q&A, CV headcount, and Gemini AI study hub.",
        },
        "paths": {
            "/healthz": {"get": {"summary": "Liveness probe"}},
            "/readyz": {"get": {"summary": "Readiness probe (DB & storage check)"}},
            "/api/auth/lecturer/register": {
                "post": {
                    "summary": "Register a new Professor account",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": LecturerRegisterSchema.model_json_schema()
                            }
                        }
                    },
                }
            },
            "/api/auth/lecturer/login": {
                "post": {
                    "summary": "Authenticate Professor via username/email + password",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": LecturerLoginSchema.model_json_schema()
                            }
                        }
                    },
                }
            },
            "/api/auth/lecturer/logout-all": {"post": {"summary": "Revoke all active sessions"}},
            "/api/auth/lecturer/audit-logs": {"get": {"summary": "List recent lecturer security audit logs"}},
            "/api/courses": {"get": {"summary": "List owned & co-staffed courses"}, "post": {"summary": "Create course"}},
            "/api/courses/{course_id}/staff": {"get": {"summary": "List TAs / co-lecturers"}, "post": {"summary": "Add TA or co-lecturer"}},
            "/api/student/login": {"post": {"summary": "Student ID + Email onboarding / sign-in"}},
            "/api/student/courses/catalog": {"get": {"summary": "Public non-PII course catalog"}},
            "/api/sessions/{session_id}/lectures/summarize": {"post": {"summary": "Upload recording & run Gemini transcription + summary"}},
            "/api/lectures/{artifact_id}": {"delete": {"summary": "Delete lecture artifact & uploaded recording"}},
        },
    }
