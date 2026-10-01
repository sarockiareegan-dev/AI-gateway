from enum import StrEnum


class ResourceType(StrEnum):
    ORGANIZATION = "organization"
    TEAM = "team"
    KEY = "key"
    BUDGET = "budget"
    MODEL = "model"


class Action(StrEnum):
    VIEW = "view"
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    MANAGE_MEMBERS = "manage_members"
    REVOKE = "revoke"
    USE = "use"
    OVERRIDE = "override"
    ASSIGN = "assign"
    RESTRICT = "restrict"
