"""领域服务使用的业务异常。"""


class DomainError(Exception):
    """所有可预期业务异常的基类。"""

    code = "domain_error"
    status = 400


class ValidationError(DomainError):
    """输入字段不符合业务约束。"""

    code = "validation_error"


class NotFoundError(DomainError):
    """请求引用的业务对象不存在。"""

    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    """操作者没有执行当前动作的权限。"""

    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    """请求编号或业务唯一键与既有内容冲突。"""

    code = "conflict"
    status = 409


class SequenceForkError(ConflictError):
    """离线补传在同一来源序号上出现不同内容，判定为序列分叉。"""

    code = "sequence_fork"


class PeriodClosedError(ConflictError):
    """目标会计期间已经关账，记录只能追加到后续期间。"""

    code = "period_closed"
