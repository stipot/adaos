# src/adaos/agent/core/scenario_engine/dsl.py
from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator  # <— v2

RunOn = Literal["hub", "member:self"] | str


class Trigger(BaseModel):
    type: Literal["time", "intent"]
    cron: Optional[str] = None
    name: Optional[str] = None
    runOn: RunOn = "hub"


class Slot(BaseModel):
    cap: str
    optional: bool = False


class IOSettings(BaseModel):
    input: List[str] = Field(default_factory=lambda: ["voice", "text"])
    output: List[str] = Field(default_factory=lambda: ["voice", "text"])
    settings: Dict[str, Any] = Field(default_factory=dict)


class Step(BaseModel):
    id: Optional[str] = None
    do: Literal["say", "call", "for", "if", "wait", "let"]
    runOn: Optional[RunOn] = None
    alias: Optional[str] = None
    if_: Optional[str] = Field(default=None, alias="if")

    text: Optional[str] = None  # say
    slot: Optional[str] = None  # call
    method: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    activityId: Optional[str] = None
    each: Optional[str] = None  # for
    steps: Optional[List["Step"]] = None
    sec: Optional[float] = None  # wait
    var: Optional[str] = None  # let
    value: Optional[Any] = None

    @model_validator(mode="after")  # <— v2 вместо root_validator
    def check_fields(self) -> "Step":
        op = self.do
        if op == "say" and not self.text:
            raise ValueError("say requires 'text'")
        if op == "call" and not self.slot:
            raise ValueError("call requires 'slot'")
        if op == "for" and not self.each:
            raise ValueError("for requires 'each'")
        if op == "wait" and self.sec is None:
            raise ValueError("wait requires 'sec'")
        if op == "let" and not self.var:
            raise ValueError("let requires 'var'")
        return self


# важное изменение имени метода для форвард-рефов (v2):
Step.model_rebuild()


class Prototype(BaseModel):
    id: str
    version: str
    name: str
    multiUser: bool = True
    triggers: List[Trigger] = Field(default_factory=list)
    slots: Dict[str, Slot] = Field(default_factory=dict)
    params: Dict[str, Any] = Field(default_factory=dict)
    io: IOSettings = Field(default_factory=IOSettings)
    steps: List[Step] = Field(default_factory=list)
    anchors: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    @field_validator("steps")  # <— v2 вместо validator("steps")
    @classmethod
    def ensure_ids_unique(cls, steps: List[Step]) -> List[Step]:
        ids = [s.id for s in steps if s.id]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        return steps


class RewriteAction(BaseModel):
    match: Dict[str, str]
    action: Literal["drop", "move_before", "move_after", "param", "io.set", "cap.narrow"]
    set: Optional[Dict[str, Any]] = None
    ref: Optional[str] = None


class ImplementationRewrite(BaseModel):
    rewriteVersion: int = 1
    params: Dict[str, Any] = Field(default_factory=dict)
    io: Dict[str, Any] = Field(default_factory=dict)
    bindings: Dict[str, Any] = Field(default_factory=dict)
    rewrite: List[RewriteAction] = Field(default_factory=list)


def validate_rewrite(proto: Prototype, imp: ImplementationRewrite) -> None:
    allowed_ids = {s.id for s in proto.steps if s.id}
    for r in imp.rewrite:
        mid = r.match.get("id")
        if mid and mid not in allowed_ids:
            raise ValueError(f"rewrite references unknown step id: {mid}")
