"""
ORM 模型汇总

所有模型在此导入，Alembic 自动生成迁移时会扫描 Base.metadata。
"""
from app.models.agent_definition import AgentDefinition
from app.models.agent_instance import AgentInstance
from app.models.group import Group
from app.models.group_member import GroupMember
from app.models.task import Task
from app.models.message import Message

__all__ = [
    "AgentDefinition",
    "AgentInstance",
    "Group",
    "GroupMember",
    "Task",
    "Message",
]
