"""验证B-2 自测：task_complete 产物下载卡在定稿气泡内可点下载（task 27）.

回归「CodeBuddy-style 气泡过程」前端 ST-06 链路（产物下载卡）：
  task 20  bus.py: emit_task_completed data 加 artifact manifest（files:[{name,path,size,modified_at}]）
  task 21  ChatPanel.tsx: finalizedBubbles 据 task_complete 的 artifact 渲染下载卡数据
            （extractFinalizedArtifacts → artifactFiles prop）
  task 22  ChatMessageBubble.tsx: 下载卡复用 TaskPage 按扩展名图标
            + GET /api/groups/{id}/files/{name}（handleArtifactDownload）

本自测验证「下载卡在定稿气泡内可点下载」契约——分两段：

  阶段 A（前端静态契约）：读 ChatPanel.tsx + ChatMessageBubble.tsx + api.ts + fileIcon.tsx
    源码，断言下载卡渲染 + 点击下载链路完整接线：
    1. ChatPanel.extractFinalizedArtifacts 从 task_complete 事件 data.artifact.files[]
       容错解析出 ArtifactFile[]（非对象/非数组返 [] 不炸渲染）。
    2. finalizedBubbles 把 artifactFiles 塞进 FinalizedBubble + 传 ChatMessageBubble
       artifactFiles prop + groupId prop（GET /api/groups/{id}/files/{name} 路径段）。
    3. ChatMessageBubble 有 hasArtifacts 守卫 + 渲染 .chat-artifact-block 卡片区
       + 按扩展名图标（fileIconFor）+ 文件名 + humanSize + 下载按钮（Button）。
    4. ChatMessageBubble.handleArtifactDownload 调 groupApi.downloadFile(groupId,
       file.path||file.name) → saveBlob（GET /api/groups/{id}/files/{name} → Blob → a.download）。
    5. groupApi.downloadFileUrl 用 /api/groups/{groupId}/files/{encoded}（每段 encodeURIComponent
       后用 / 拼，含子目录/空格/中文安全）。

  阶段 B（后端运行时 + 端到端下载）：单聊群「后端工程师」发 write_file 任务，
    WS 抓 task_complete 事件 → 断言其 data.artifact.files[] 含产物（下载卡有数据）→
    按 manifest 里 file.path 调下载端点 GET /api/groups/{id}/files/{path} →
    200 + 内容含哨兵标记 == 磁盘真源（点击下载真能拿到产物）。
    6. task_complete 事件 data.artifact 存在（manifest 透传到 WS 事件，下载卡数据源）。
    7. data.artifact.files[] 非空 + 含本次产物（name/path/size 字段齐全）。
    8. 按 manifest file.path 下载 → 200 + 内容含哨兵 == 磁盘真源（端到端可点下载）。
    9. task_complete(success) + 磁盘产物落盘（write_file 真实执行）。

为何「WS 事件 data.artifact」而非「TaskEntity.artifact」：
  定稿气泡的下载卡数据源是 task_complete WS 事件的 data.artifact（task 21 设计），
  不是 TaskEntity.artifact（那是 TaskPage 交付物卡的数据源，PL-12）。worker 任务
  用 tq_ runtime id，往往无 TaskEntity 行（set_task_artifact 返回 None，artifact 不
  落库）——但 task_complete 事件 data.artifact 是 emit_task_completed 现场透传的
  scan_workspace_artifacts manifest，不依赖任务行存在。故本自测断言 WS 事件
  data.artifact（下载卡真实数据源），而非 TaskEntity.artifact（PL-12 已覆盖）。

为何用 disk cross-check 而非前端 e2e：
  项目无前端测试运行器。下载卡的「可点下载」=「点击后能拿到产物内容」，前端
  handleArtifactDownload 的核心是 groupApi.downloadFile(groupId, file.path) →
  saveBlob(blob)。groupApi.downloadFile 即 fetch(GET /api/groups/{id}/files/{name})
  → blob。故「前端点击下载真能拿到产物」等价于「GET /api/groups/{id}/files/
  {manifest.path} 返回 200 + 内容 == 磁盘真源」——本自测直接 HTTP 校验此端点，
  与前端 fetch 同 URL 同逻辑。manifest.path 来自 WS 事件 data.artifact.files[].path
  （即下载卡渲染的 file.path，handleArtifactDownload 用的就是这个）。

为何用单聊群「后端工程师」：
  与验证B-1 一致——group_e53545c（single_chat=true，coordinator_id=agent_backend_1），
  @后端工程师 发 write_file 任务直送其 brain（execute 路径），单 worker 单任务内存
  占用低，产物干净（工作区原本基本空，write_file 创建的文件即唯一产物）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as url_quote

import httpx
import websockets

BASE = "http://localhost:8000"
# 单聊群「后端工程师」——single_chat=true，coordinator_id=agent_backend_1。
GROUP_ID = "group_e53545c71a8c4cf8ae5e69d06ef77952"
WS_URL = f"ws://localhost:8000/ws/bus/{GROUP_ID}"
WORKER_ID = "agent_backend_1"

# 自测专属产物文件名 + 内容哨兵（worker 不可能凭空写出固定串 → 证明是任务产物）。
OUT_FILE = "vb2_artifact_download_probe.md"
SIGNATURE = "VB2_ARTIFACT_DOWNLOAD_SIGNATURE_OK"

# 强制 worker 调 write_file 产出文件，首行必须是哨兵标记。
TASK_CONTENT = (
    f"@后端工程师 请直接用 write_file 工具创建文件 {OUT_FILE}，内容要求：\n"
    f"1. 第一行必须是这串固定标记（一字不差）：{SIGNATURE}\n"
    f"2. 第二行写一句简短说明这是产物下载卡自测文件；\n"
    f"3. 第三行写当前任务时间。完成后用一句话回复结论。"
    f"注意：必须用 write_file 工具真实创建该文件，不要只口头描述。"
)

WS_TIMEOUT = 180.0

# 前端源码路径（静态契约断言用）。
REPO = Path(__file__).resolve().parents[2]
CHAT_PANEL = REPO / "src" / "components" / "ChatPanel.tsx"
BUBBLE = REPO / "src" / "components" / "ChatMessageBubble.tsx"
API_TS = REPO / "src" / "services" / "api.ts"
FILE_ICON = REPO / "src" / "lib" / "fileIcon.tsx"


async def health_ok() -> bool:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/health")
        return r.json().get("status") == "ok"


async def worker_status() -> str:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/api/status/{GROUP_ID}")
        for a in r.json():
            if a["id"] == WORKER_ID:
                return a["status"]
    return "unknown"


async def send_message(content: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE}/api/messages",
            json={
                "group_id": GROUP_ID,
                "sender_id": "user",
                "receiver_id": "broadcast",
                "type": "user_input",
                "content": content,
            },
        )
        return r.json()


async def collect_until_done(timeout: float) -> list[dict]:
    """连 WS 收事件直到 task_complete/task_failed 或超时。返回全量事件（到达序）。"""
    events: list[dict] = []
    deadline = time.time() + timeout
    finished = False
    async with websockets.connect(WS_URL) as ws:
        while time.time() < deadline and not finished:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") in ("task_complete", "task_failed"):
                # 收尾后再多收 3s（agent_reply/idle 紧随其后）
                end = time.time() + 3.0
                while time.time() < end:
                    try:
                        raw2 = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, end - time.time())
                        )
                        events.append(json.loads(raw2))
                    except asyncio.TimeoutError:
                        break
                finished = True
    return events


async def download_file(file_name: str) -> tuple[int, bytes, str]:
    """GET 下载端点（与前端 groupApi.downloadFile 同 URL），返回 (status, body, content_type)。"""
    # 复刻前端 downloadFileUrl：每段 encodeURIComponent 后用 / 拼。
    encoded = "/".join(url_quote(seg, safe="") for seg in file_name.split("/"))
    url = f"{BASE}/api/groups/{GROUP_ID}/files/{encoded}"
    async with httpx.AsyncClient() as c:
        r = await c.get(url)
        return r.status_code, r.content, r.headers.get("content-type", "")


# ── 前端静态契约断言 ──


def assert_frontend_contract() -> list[str]:
    """读前端源码断言下载卡渲染 + 点击下载链路。返回 errs 列表。"""
    errs: list[str] = []
    panel = CHAT_PANEL.read_text(encoding="utf-8")
    bubble = BUBBLE.read_text(encoding="utf-8")
    api_ts = API_TS.read_text(encoding="utf-8")
    file_icon = FILE_ICON.read_text(encoding="utf-8")

    # [1] extractFinalizedArtifacts 容错解析 data.artifact.files[]
    if "extractFinalizedArtifacts" not in panel:
        errs.append("[前端1] ChatPanel 未定义 extractFinalizedArtifacts（task 21 数据管道缺失）")
    else:
        m = re.search(
            r"function extractFinalizedArtifacts\(data: unknown\): ArtifactFile\[\] \{(.*?)\n\}",
            panel,
            re.S,
        )
        if not m:
            errs.append("[前端1] extractFinalizedArtifacts 函数结构不符")
        else:
            body = m.group(1)
            # B20：null-guard 下沉到 safeRecord 单一真源（原 typeof data !== 'object' 等三处
            # 守卫改调 safeRecord）。断言：① 三层都调 safeRecord（data/artifact/file 条目）；
            # ② files 仍 Array.isArray 守卫（files 是数组，safeRecord 排除数组故单独判）；
            # ③ safeRecord 真源（services/api.ts）有 typeof !== 'object' 守卫。
            data_guard = "safeRecord(data)" in body
            artifact_guard = "safeRecord(dd['artifact'])" in body
            file_guard = "safeRecord(raw)" in body
            has_files_guard = "Array.isArray(files)" in body
            if not (data_guard and artifact_guard and file_guard and has_files_guard):
                errs.append(
                    "[前端1] extractFinalizedArtifacts 容错守卫不全（B20 safeRecord 下沉后）"
                    f"（data={data_guard} artifact={artifact_guard} file={file_guard} files={has_files_guard}）"
                )
            else:
                # safeRecord 真源守卫
                api_src = API_TS.read_text(encoding="utf-8")
                m_safe = re.search(r"export function safeRecord\([^)]*\)[^{]*\{(.*?)\n\}", api_src, re.S)
                if not m_safe or "typeof data !== 'object'" not in m_safe.group(1):
                    errs.append("[前端1] services/api.ts safeRecord 守卫缺失（typeof !== 'object'）")
                else:
                    print("[前端1] OK  extractFinalizedArtifacts 三层 safeRecord + Array.isArray(files) 容错（B20 守卫下沉真源）")

    # [2] finalizedBubbles 把 artifactFiles + groupId 传 ChatMessageBubble
    if "artifactFiles: extractFinalizedArtifacts(e.data)" not in panel:
        errs.append("[前端2] finalizedBubbles 未把 extractFinalizedArtifacts(e.data) 塞进 artifactFiles")
    elif "artifactFiles={b.artifactFiles}" not in panel:
        errs.append("[前端2] 定稿气泡未传 artifactFiles={b.artifactFiles}（下载卡无数据）")
    elif "groupId={chatGroupId ?? undefined}" not in panel:
        errs.append("[前端2] 定稿气泡未传 groupId（下载端点缺路径段 → 按钮禁用）")
    else:
        print("[前端2] OK  finalizedBubbles 传 artifactFiles + groupId 给定稿气泡")

    # [3] ChatMessageBubble hasArtifacts 守卫 + .chat-artifact-block 卡片区 + 图标 + 下载按钮
    if "hasArtifacts" not in bubble:
        errs.append("[前端3] ChatMessageBubble 无 hasArtifacts 守卫（task 22 渲染缺失）")
    elif "chat-artifact-block" not in bubble:
        errs.append("[前端3] ChatMessageBubble 无 .chat-artifact-block 卡片区")
    elif "fileIconFor" not in bubble:
        errs.append("[前端3] ChatMessageBubble 未用 fileIconFor（按扩展名图标缺失）")
    elif "humanSize" not in bubble:
        errs.append("[前端3] ChatMessageBubble 未用 humanSize（文件大小缺失）")
    elif "DownloadOutlined" not in bubble:
        errs.append("[前端3] ChatMessageBubble 无 DownloadOutlined 下载按钮")
    else:
        print("[前端3] OK  hasArtifacts 守卫 + .chat-artifact-block + fileIconFor + humanSize + 下载按钮")

    # [4] handleArtifactDownload 调 groupApi.downloadFile + saveBlob
    if "handleArtifactDownload" not in bubble:
        errs.append("[前端4] ChatMessageBubble 无 handleArtifactDownload（点击下载未接线）")
    else:
        m = re.search(
            r"const handleArtifactDownload = async \(file: ArtifactFile\) => \{(.*?)\n  \}",
            bubble,
            re.S,
        )
        if not m:
            errs.append("[前端4] handleArtifactDownload 函数结构不符")
        else:
            body = m.group(1)
            has_group_guard = "if (!groupId)" in body
            has_download = "groupApi.downloadFile(groupId" in body
            has_save = "saveBlob(blob" in body
            uses_path = "file.path || file.name" in body
            if not (has_group_guard and has_download and has_save and uses_path):
                errs.append(
                    "[前端4] handleArtifactDownload 链路不全"
                    f"（group_guard={has_group_guard} download={has_download} saveBlob={has_save} uses_path={uses_path}）"
                )
            else:
                print("[前端4] OK  handleArtifactDownload: groupId 守卫 → downloadFile(groupId, file.path||name) → saveBlob")

    # [5] groupApi.downloadFileUrl = /api/groups/{groupId}/files/{encoded}
    if "downloadFileUrl" not in api_ts:
        errs.append("[前端5] api.ts 无 downloadFileUrl")
    elif "/api/groups/" not in api_ts or "/files/" not in api_ts:
        errs.append("[前端5] downloadFileUrl URL 模板不符 /api/groups/{id}/files/{name}")
    elif "encodeURIComponent" not in api_ts:
        errs.append("[前端5] downloadFileUrl 未 encodeURIComponent 各段（含子目录/空格/中文不安全）")
    else:
        print("[前端5] OK  downloadFileUrl = /api/groups/{groupId}/files/{encoded}（各段 encodeURIComponent）")

    # [6] fileIcon.tsx 导出 fileIconFor + saveBlob + humanSize（共享模块）
    for fn in ("fileIconFor", "saveBlob", "humanSize"):
        if f"export function {fn}" not in file_icon and f"export const {fn}" not in file_icon:
            errs.append(f"[前端6] fileIcon.tsx 未导出 {fn}（共享模块缺失）")
    if not any(e.startswith("[前端6]") for e in errs):
        print("[前端6] OK  fileIcon.tsx 导出 fileIconFor + saveBlob + humanSize（ChatMessageBubble/TaskPage 共享）")

    return errs


async def main() -> int:
    print("=== 验证B-2：task_complete 产物下载卡在定稿气泡内可点下载 ===\n")

    # ── 阶段 A：前端静态契约 ──
    print("── 阶段 A：前端静态契约断言 ──")
    fe_errs = assert_frontend_contract()
    if fe_errs:
        print("\n[阶段A] FAIL:")
        for e in fe_errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL（前端契约） ===")
        return 1
    print("[阶段A] PASS\n")

    # ── 阶段 B：后端运行时 + 端到端下载 ──
    print("── 阶段 B：后端运行时 + 端到端下载 ──")
    if not await health_ok():
        print("[fatal] backend 不在线"); return 2
    print("[health] ok")

    # 等 worker 空闲
    for _ in range(30):
        st = await worker_status()
        if st == "idle":
            break
        print(f"[wait] worker 状态={st}，等待空闲...")
        await asyncio.sleep(2)
    else:
        print("[fatal] worker 一直 busy，放弃本次自测"); return 2
    print(f"[worker] {WORKER_ID} idle")

    # 清理残留产物
    data_dir = os.environ.get(
        "MULTI_AGENT_DATA_DIR", str(Path.home() / ".local" / "share" / "multi-agent")
    )
    workspace = Path(data_dir) / "workspaces" / GROUP_ID
    out_path = workspace / OUT_FILE
    if out_path.exists():
        out_path.unlink()
        print(f"[cleanup] 删除残留产物 {OUT_FILE}")

    # 连 WS + 发任务
    ws_task = asyncio.create_task(collect_until_done(WS_TIMEOUT))
    await asyncio.sleep(0.5)
    sent = await send_message(TASK_CONTENT)
    print(f"[send] user message id={sent.get('id','')[:16]}...")

    events = await ws_task
    type_counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1
    print(f"[events] 收到 {len(events)} 条; 类型分布={type_counts}")

    complete_ev = next((e for e in events if e.get("type") == "task_complete"), None)
    failed_ev = next((e for e in events if e.get("type") == "task_failed"), None)

    errs: list[str] = []

    # ── 校验 6：task_complete 事件 data.artifact 存在 ──
    if not complete_ev:
        errs.append(f"未收到 task_complete 事件（任务未成功完成，failed={failed_ev is not None}）")
        artifact = None
    else:
        data = complete_ev.get("data") or {}
        artifact = data.get("artifact")
        # emit_task_completed 用 `if artifact: data["artifact"] = artifact`（key omission when None）
        if not artifact:
            errs.append(
                "task_complete.data.artifact 缺失（成功路径应透传 scan_workspace_artifacts manifest）"
            )
        else:
            print(f"[check 6] OK  task_complete.data.artifact 存在（manifest 透传到 WS 事件 = 下载卡数据源）")

    # ── 校验 7：data.artifact.files[] 非空 + 含本次产物 + 字段齐全 ──
    target_file = None  # manifest 中本次产物的 file 条目（用其 path 下载）
    if artifact:
        files = (artifact or {}).get("files") if isinstance(artifact, dict) else None
        if not isinstance(files, list) or not files:
            errs.append(f"data.artifact.files 非数组或为空（下载卡无文件可渲染）")
        else:
            print(f"[check 7] manifest files[] 共 {len(files)} 个文件")
            # 找本次产物（name == OUT_FILE 或 path 以 OUT_FILE 结尾）
            for f in files:
                if not isinstance(f, dict):
                    continue
                name = str(f.get("name") or "")
                path = str(f.get("path") or "")
                if name == OUT_FILE or path.endswith(OUT_FILE):
                    target_file = f
                    break
            if target_file is None:
                # 列出 manifest 文件名辅助诊断
                names = [str((f or {}).get("name") or "") for f in files if isinstance(f, dict)]
                errs.append(f"manifest.files 不含本次产物 {OUT_FILE}（现有：{names[:8]}）")
            else:
                # 字段齐全校验
                missing = [
                    k for k in ("name", "path", "size", "modified_at")
                    if not target_file.get(k)
                ]
                if missing:
                    errs.append(f"产物文件条目缺字段 {missing}（下载卡渲染不全）")
                else:
                    print(
                        f"[check 7] OK  manifest 含本次产物：name={target_file['name']} "
                        f"path={target_file['path']} size={target_file['size']}"
                    )

    # ── 校验 8：按 manifest file.path 下载 → 200 + 内容含哨兵 == 磁盘真源 ──
    # 这是「点击下载真能拿到产物」的端到端校验——前端 handleArtifactDownload 用的就是
    # groupApi.downloadFile(groupId, file.path||file.name)，与下面 download_file 同 URL 同逻辑。
    disk_ok = out_path.exists() and out_path.stat().st_size > 0
    disk_text = out_path.read_text(encoding="utf-8", errors="replace") if disk_ok else ""

    if target_file is not None:
        dl_name = target_file.get("path") or target_file.get("name") or OUT_FILE
        status, body, ctype = await download_file(dl_name)
        if status != 200:
            errs.append(f"下载 {dl_name} 失败 status={status}（端到端点击下载拿不到产物）")
        else:
            dl_text = body.decode("utf-8", errors="replace")
            if SIGNATURE not in dl_text:
                errs.append(
                    f"下载内容不含哨兵标记 {SIGNATURE}（非任务产物或内容错乱）"
                )
            elif disk_text and dl_text != disk_text:
                errs.append("下载内容与磁盘真源不一致（端到端内容错乱）")
            else:
                print(
                    f"[check 8] OK  下载 200 type={ctype} size={len(body)} "
                    f"含哨兵 == 磁盘真源（端到端可点下载）"
                )
    else:
        errs.append("无 manifest 产物条目，跳过下载校验（已在前序 check 记录）")

    # ── 校验 9：task_complete(success) + 磁盘产物落盘 ──
    success = complete_ev is not None and failed_ev is None
    if not success:
        errs.append("任务未以 task_complete(success) 收尾")
    if not disk_ok:
        errs.append(f"磁盘产物 {OUT_FILE} 未落盘或为空（write_file 未真实执行）")
    elif SIGNATURE not in disk_text:
        errs.append(f"磁盘产物不含哨兵标记 {SIGNATURE}（非任务产物）")
    else:
        print(f"[check 9] OK  task_complete(success) + 磁盘产物落盘（size={out_path.stat().st_size} 含哨兵）")

    # 清理产物
    if out_path.exists():
        try:
            out_path.unlink()
            print(f"[cleanup] 删除产物 {OUT_FILE}")
        except Exception:
            pass

    if errs:
        print("\n[阶段B] FAIL:")
        for e in errs:
            print(f"  - {e}")
        print("\n=== 结果: FAIL（运行时） ===")
        return 1

    print("\n=== 结果: PASS ===")
    print(
        "task_complete 产物下载卡在定稿气泡内可点下载：\n"
        "  · 前端契约：extractFinalizedArtifacts 容错解析 data.artifact.files[] + "
        "finalizedBubbles 传 artifactFiles+groupId + ChatMessageBubble hasArtifacts 卡片区"
        "（fileIconFor/humanSize/下载按钮）+ handleArtifactDownload(downloadFile→saveBlob) "
        "+ downloadFileUrl(/api/groups/{id}/files/{encoded}) + fileIcon 共享模块；\n"
        "  · 运行时：task_complete.data.artifact 透传 manifest + 含本次产物（字段齐全）+ "
        "按 manifest.path 下载 200 含哨兵==磁盘真源（端到端可点下载）+ 磁盘产物落盘。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
