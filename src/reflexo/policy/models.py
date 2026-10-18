"""Requests, decisions and compiled sets of the policy engine (BETA_04 §7.1, §7.3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Effect = Literal["act", "decline", "propose", "not_possible", "shadow"]
Layer = Literal["platform", "customer", "shop", "default", "ceiling"]
RuleLayer = Literal["customer", "shop"]
NAMESPACE = "Reflexo"


class EntityRef(BaseModel):
    """A Cedar entity: `type` without the namespace ('Order'), `id` as text."""

    model_config = ConfigDict(frozen=True)
    type: str
    id: str

    @property
    def uid(self) -> dict[str, str]:
        return {"type": f"{NAMESPACE}::{self.type}", "id": self.id}

    @property
    def cedar(self) -> str:
        return f'{NAMESPACE}::{self.type}::"{self.id}"'


class PolicyRequest(BaseModel):
    """Everything both evaluators need: no database behind it."""

    principal: EntityRef  # Agent (a desk) or Member (a person deciding)
    action: str  # the capability
    resource: EntityRef
    context: dict[str, Any]  # typed by the schema's Ctx; requested_by and owner_approved always present
    entities: list[dict[str, Any]]  # Cedar entity JSON: the principal, the business, the resource, its customer
    owner_approved: bool = False

    def entity(self, ref: EntityRef | dict[str, Any] | None) -> dict[str, Any] | None:
        if ref is None:
            return None
        uid = ref.uid if isinstance(ref, EntityRef) else ref
        return next((e for e in self.entities if e["uid"] == uid), None)


class Decision(BaseModel):
    effect: Effect
    layer: Layer
    policy_ids: list[str] = Field(default_factory=list)  # the Cedar @id of every policy that decided
    says: str | None = None  # the owner's words of the deciding rule
    reason: str | None = None  # for not_possible (and decline when the rule gives one)


class CompiledSet(BaseModel):
    layer: RuleLayer
    cedar_text: str
    source_hash: bytes
    rule_ids: list[str] = Field(default_factory=list)  # in policy order
