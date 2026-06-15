"""
群共享文件目录服务

存储位置: data/group_files/{group_id}/
- 群共享根目录，只有一级，无子目录
- 子智能体可以通过后端操作读写文件
- 人类只读展示，不能操作
"""
import os
import time
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "group_files"


def _group_dir(group_id: str) -> Path:
    return DATA_ROOT / group_id


def ensure_group_dir(group_id: str) -> Path:
    """确保群组的共享目录存在，返回目录路径"""
    d = _group_dir(group_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_files(group_id: str) -> list[dict]:
    """列出群共享根目录下的所有文件"""
    d = _group_dir(group_id)
    if not d.exists():
        return []

    files = []
    for f in sorted(d.iterdir()):
        if f.is_file():
            stat = f.stat()
            files.append({
                "name": f.name,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            })
    return files


def read_file(group_id: str, filename: str) -> bytes:
    """读取群共享目录下的文件"""
    d = _group_dir(group_id)
    fp = d / filename
    if not fp.exists() or not fp.is_file():
        raise FileNotFoundError(f"文件不存在: {filename}")
    return fp.read_bytes()


def write_file(group_id: str, filename: str, content: bytes) -> dict:
    """写入文件到群共享目录（覆盖或新建）"""
    d = ensure_group_dir(group_id)
    # 安全校验：禁止路径穿越
    fp = (d / filename).resolve()
    if not str(fp).startswith(str(d)):
        raise ValueError("非法文件名")
    fp.write_bytes(content)
    stat = fp.stat()
    return {
        "name": fp.name,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
    }


def delete_file(group_id: str, filename: str) -> bool:
    """删除群共享目录下的文件"""
    d = _group_dir(group_id)
    fp = (d / filename).resolve()
    if not str(fp).startswith(str(d)):
        return False
    if fp.exists() and fp.is_file():
        fp.unlink()
        return True
    return False


def file_info(group_id: str, filename: str) -> dict | None:
    """获取文件信息"""
    d = _group_dir(group_id)
    fp = d / filename
    if not fp.exists() or not fp.is_file():
        return None
    stat = fp.stat()
    return {
        "name": fp.name,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
    }
