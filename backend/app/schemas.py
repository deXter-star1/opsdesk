"""Request bodies. Pydantic validates shape and bounds; business rules live in domain/policy.py."""
from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .domain.workflow import KINDS, TRANSITIONS

Kind = Literal[KINDS]  # type: ignore[valid-type]
Action = Literal[tuple(TRANSITIONS)]  # type: ignore[valid-type]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=20000)


class LoginIn(Strict):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class CreateItemIn(Strict):
    team_id: int
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=20000)
    kind: Kind
    priority: int = Field(ge=1, le=4)
    due_at: Optional[datetime] = None
    requires_approval: Optional[bool] = None


class ItemChanges(Strict):
    title: Optional[str] = Field(default=None, min_length=3, max_length=200)
    description: Optional[str] = Field(default=None, max_length=20000)
    priority: Optional[int] = Field(default=None, ge=1, le=4)
    kind: Optional[Kind] = None
    due_at: Optional[datetime] = None          # explicit null clears the deadline
    requires_approval: Optional[bool] = None

    NON_NULLABLE: ClassVar[tuple] = ("title", "description", "priority", "kind", "requires_approval")

    def provided(self) -> dict:
        """Only the fields the client actually sent (PATCH semantics)."""
        return {k: getattr(self, k) for k in self.model_fields_set}


class UpdateItemIn(Strict):
    expected_version: int = Field(ge=1)
    changes: ItemChanges


class AssignIn(Strict):
    assignee_id: Optional[int] = None
    expected_version: Optional[int] = None


class TransitionIn(Strict):
    action: Action
    expected_version: Optional[int] = None
    note: Optional[str] = Field(default=None, max_length=5000)


class MoveIn(Strict):
    team_id: int
    expected_version: Optional[int] = None


class CommentIn(Strict):
    body: str = Field(min_length=1, max_length=10000)


class MarkReadIn(Strict):
    ids: list[int] = Field(default_factory=list, max_length=500)
    all: bool = False
