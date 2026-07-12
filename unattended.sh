#!/bin/bash
# 24h 无人值守自动开发脚本（单文件版）
# 用法：
#   ./unattended.sh "目标"       # 启动（自动拆任务 + 主循环 + 看门狗）
#   ./unattended.sh --stop       # 停止所有进程
#   ./unattended.sh --status     # 看实时状态
#   ./unattended.sh --report     # 生成事后报告

set -euo pipefail

TASK_FILE=".task.md"
LOG_FILE="night_run.log"
STATUS_FILE=".status"
MEMO_DIR=".claude/memory"
MAIN_PID_FILE=".pid.main"
WDOG_PID_FILE=".pid.watchdog"
TIMEOUT_MIN=60

# -------- 工具函数 --------

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

write_status() {
  cat > "$STATUS_FILE" <<EOF
last_heartbeat: $(date '+%Y-%m-%d %H:%M:%S')
status: ${1:-idle}
current_task: ${2:-N/A}
remaining: ${3:-0}
completed: ${4:-0}
EOF
}

is_running() {
  [ -f "$1" ] && kill -0 "$(cat "$1" 2>/dev/null)" 2>/dev/null
}

# 递归杀掉一个进程及其所有子进程（claude 子进程、测试进程等）
# 避免主进程被杀后 claude 变成孤儿继续占用会话
# $1=要杀的 PID  $2=要跳过的 PID（保护看门狗自身，因为它是主进程的子进程）
kill_tree() {
  local pid="$1"
  local exclude="${2:-}"
  [ -z "$pid" ] && return 0
  kill -0 "$pid" 2>/dev/null || return 0
  local child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    [ "$child" = "$exclude" ] && continue
    kill_tree "$child" "$exclude" || true
  done
  kill -9 "$pid" 2>/dev/null || true
  return 0
}

# -------- 看门狗模式 --------

watchdog_mode() {
  echo $$ > "$WDOG_PID_FILE"
  log "[WATCHDOG] 启动，PID=$$"

  while true; do
    sleep 300

    # 主进程还活着？
    if is_running "$MAIN_PID_FILE"; then
      # 检查心跳
      if [ -f "$STATUS_FILE" ]; then
        local last_beat idle_min
        last_beat=$(grep "last_heartbeat:" "$STATUS_FILE" | cut -d':' -f2- | xargs)
        local last_ts now_ts
        last_ts=$(date -d "$last_beat" +%s 2>/dev/null || echo 0)
        now_ts=$(date +%s)
        idle_min=$(((now_ts - last_ts) / 60))

        local st rm
        st=$(grep "status:" "$STATUS_FILE" | awk '{print $2}')
        rm=$(grep "remaining:" "$STATUS_FILE" | awk '{print $2}')

        if [ "$idle_min" -gt "$TIMEOUT_MIN" ]; then
          local main_pid wdog_pid
          main_pid=$(cat "$MAIN_PID_FILE" 2>/dev/null || echo 0)
          wdog_pid=$(cat "$WDOG_PID_FILE" 2>/dev/null || echo 0)
          log "[WATCHDOG] 💀 假死！${idle_min}m 无心跳，杀死主进程 $main_pid 及其子进程（保护看门狗 $wdog_pid）"
          kill_tree "$main_pid" "$wdog_pid"
          rm -f "$MAIN_PID_FILE"
          sleep 5
        else
          log "[WATCHDOG] 💓 正常（${idle_min}m 前） status=$st 剩余=$rm"
        fi
      fi
    else
      # 主进程没了，重新拉起（不带 --watchdog，所以它会走主逻辑）
      # 但如果任务已全部完成（.status 显示 remaining=0），就别重启了
      local done_flag=0
      if [ -f "$STATUS_FILE" ]; then
        local s_rm
        s_rm=$(grep "remaining:" "$STATUS_FILE" | awk '{print $2}')
        [ "${s_rm:-0}" -eq 0 ] && done_flag=1
      fi
      if [ "$done_flag" -eq 1 ]; then
        log "[WATCHDOG] 主进程已退出且任务全部完成，看门狗也退出"
        rm -f "$WDOG_PID_FILE"
        exit 0
      fi
      log "[WATCHDOG] ⚠️ 主进程不存在，重新拉起..."
      nohup "$0" --internal-resume > /dev/null 2>&1 &
      echo $! > "$MAIN_PID_FILE"
      log "[WATCHDOG] 🚀 新主进程 PID=$!"
    fi
  done
}

# -------- 主循环 --------

main_loop() {
  echo $$ > "$MAIN_PID_FILE"
  log "主进程启动，PID=$$"

  local TOTAL COMPLETED LOOP_COUNT
  TOTAL=$(grep -c '^- \[' "$TASK_FILE" 2>/dev/null || true)
  TOTAL=${TOTAL:-0}
  COMPLETED=0
  LOOP_COUNT=0

  # 会话 id：首轮用 `claude -p`（无 --continue）开一个全新会话，从 stream-json 的
  # init 事件抓 session_id 落盘；后续轮次用 `claude --resume <id> -p` 续接这个会话。
  # 为什么不用 --continue：--continue 续接「项目最近会话」，而最近会话往往是上一批
  # 跑完的旧会话（上下文里满是「全部完成」的认知）→ worker 走「已完成检测」直接
  # 判定已实现、秒退打勾、零产出。新会话从零开始，才能真去做任务。
  # 见用户反馈 [[unattended-new-session-fix]]。
  local SESSION_ID_FILE=".session_id"
  local SESSION_ID=""
  [ -f "$SESSION_ID_FILE" ] && SESSION_ID=$(cat "$SESSION_ID_FILE" 2>/dev/null || true)

  while true; do
    local REMAINING CURRENT_TASK
    REMAINING=$(grep -c '\[ \]' "$TASK_FILE" 2>/dev/null || true)
    REMAINING=${REMAINING:-0}
    COMPLETED=$((TOTAL - REMAINING))

    if [ "$REMAINING" -eq 0 ]; then
      log "✅ 全部完成！共 $TOTAL 个任务"
      write_status "completed" "全部完成" 0 "$TOTAL"
      rm -f "$MAIN_PID_FILE"
      # 任务全部完成，停止看门狗，否则它会无限重启主进程
      # 注意：看门狗是 nohup 派生的兄弟进程，不是本进程的子进程，
      # kill_tree 的 pgrep -P 找不到它，必须直接按 PID 杀
      if is_running "$WDOG_PID_FILE"; then
        local wpid
        wpid=$(cat "$WDOG_PID_FILE" 2>/dev/null)
        # 先杀看门狗，再杀它的子进程（看门狗可能派生了主进程）
        kill -9 "$wpid" 2>/dev/null || true
        kill_tree "$wpid" 2>/dev/null
        rm -f "$WDOG_PID_FILE"
        log "🛑 看门狗已随任务完成而退出 (PID=$wpid)"
      fi
      exit 0
    fi

    CURRENT_TASK=$(grep '^- \[ \]' "$TASK_FILE" | head -1)
    LOOP_COUNT=$((LOOP_COUNT + 1))

    log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    log "🔄 第 $LOOP_COUNT 轮 | 剩余 $REMAINING/$TOTAL"
    log "📋 $CURRENT_TASK"

    local start_ts end_ts dur_min
    start_ts=$(date +%s)
    write_status "running" "$CURRENT_TASK" "$REMAINING" "$COMPLETED"

    # 核心：调用 Claude，注入"禁止提问"铁律
    # 注意：打勾由脚本在 claude 退出后用 sed 完成，不依赖 claude 自己改 .task.md
    #
    # 会话策略：首轮 SESSION_ID 为空 → 用 `claude -p`（无 --continue）开新会话，
    #   --output-format stream-json 抓 init 事件的 session_id 落盘；
    #   后续轮次 SESSION_ID 非空 → 用 `claude --resume <id> -p` 续接同一新会话。
    #   看门狗 --internal-resume 重启时也会读 .session_id 续接，不丢上下文。
    local CLAUDE_PROMPT RESUME_ARG SESSION_JSON
    CLAUDE_PROMPT="## 角色
你是无人值守开发助手，当前处于 24 小时自动运行模式。全程没有任何人在场，你必须独立完成决策。

## 铁律（违反会导致系统崩溃）
1. 绝对不要向用户提问任何问题，不要等待确认。
2. 执行任务过程中遇到任何选择或决策点，自己直接做决定，选择你判断的最优解，不要询问用户。
   - 技术选型、接口设计、命名、依赖取舍、实现方式……全部自己定。
   - 原则：选最稳妥、最标准、最易维护的方案；在多个可行方案间选风险最低的那个。
3. 如果进入计划模式，直接执行计划，不要等待批准。
4. 如果检测到危险操作，低风险直接执行，高风险跳过并在 progress.md 说明原因。
5. 基于最佳实践自行推断，绝不索要任何额外信息。宁可基于合理假设推进，也不要停下来等。
6. 遇到报错或失败，自己排查、自己修，不要向用户求助。

## 本轮任务（只做这一个）
任务：$CURRENT_TASK

## 流程
1. 【必做第一步·已完成检测】该任务对应的代码/文件可能已经存在并实现完整。
   - 用 Read/Grep 检查任务涉及的目标文件是否已存在、关键函数/接口是否已实现。
   - 若已存在且实现完整 → 直接结束本轮（脚本会自动打勾），不要重写、不要修改。
   - 若不存在或实现不完整 → 继续执行下面的流程。
2. 用 Read 工具读取 .task.md，确认第一个 [ ] 未完成任务与上面一致。
3. 读取 .claude/memory/ 了解项目背景与已有决策。
4. 执行该任务（代码写到文件）。遇到任何决策点自己拍板，不要询问。
5. 更新 .claude/memory/progress.md（追加「已完成」、记录关键决策与理由）。

## 规则
- 一次只做一个任务，不考虑后续任务。
- 不要自己修改 .task.md 的勾选状态——打勾由外部脚本负责。
- 依赖已有文件时自己读。
- 本轮结束后直接结束，不要输出总结。
"

    if [ -n "$SESSION_ID" ]; then
      # 续接已有会话（纯文本输出，stderr 入日志）
      claude --resume "$SESSION_ID" -p "$CLAUDE_PROMPT" \
        --dangerously-skip-permissions < /dev/null 2>> "$LOG_FILE" || true
    else
      # 首轮：开新会话，stream-json 抓 session_id
      # 注意：stream-json 必须配 --verbose，否则 claude 报
      # "When using --print, --output-format=stream-json requires --verbose" 直接退出
      SESSION_JSON=$(claude -p "$CLAUDE_PROMPT" \
        --output-format stream-json --verbose \
        --dangerously-skip-permissions < /dev/null 2>> "$LOG_FILE" || true)
      # 从 init 事件抓 session_id（type:system, subtype:init 那行）
      SESSION_ID=$(printf '%s' "$SESSION_JSON" | grep -o '"session_id":"[^"]*"' | head -1 | sed 's/"session_id":"//; s/"$//' || true)
      if [ -n "$SESSION_ID" ]; then
        echo "$SESSION_ID" > "$SESSION_ID_FILE"
        log "🔗 新会话已建立: $SESSION_ID"
      else
        log "⚠️ 未能从首轮输出抓到 session_id，下轮仍尝试新会话"
      fi
    fi

    end_ts=$(date +%s)
    dur_min=$(((end_ts - start_ts) / 60))

    # 【C 方案】脚本侧自动打勾：把第一个 [ ] 改成 [x]
    # 这样即使 claude 压缩会话后忘记打勾，进度也能推进
    # 注意：不用 && 串联，避免 set -e 在 sed 无匹配时（返回非0）误杀脚本
    local TICK_RESULT
    if sed -i '0,/^- \[ \]/s/^- \[ \]/- [x]/' "$TASK_FILE" 2>/dev/null; then
      TICK_RESULT=ok
    else
      TICK_RESULT=fail
    fi

    # 打勾后重新计算剩余，写准确的完成状态
    # 注意：grep -c 无匹配返回非0，配合 set -e 会杀脚本，必须用 || true 兜底
    local NEW_REMAINING
    NEW_REMAINING=$(grep -c '\[ \]' "$TASK_FILE" 2>/dev/null || true)
    NEW_REMAINING=${NEW_REMAINING:-0}
    local NEW_COMPLETED=$((TOTAL - NEW_REMAINING))

    log "⏱️ 第 $LOOP_COUNT 轮结束，耗时 ${dur_min}m（打勾: $TICK_RESULT）"
    write_status "idle" "$CURRENT_TASK" "$NEW_REMAINING" "$NEW_COMPLETED"

    # 每轮结束自动 commit（仅本地，不 push）
    # 由脚本提交而非 Claude，保证提交信息统一、不误提交运行产物
    if [ -d .git ]; then
      git add -A 2>/dev/null || true
      if ! git diff --cached --quiet 2>/dev/null; then
        # 从任务行提取编号 [CF-01] 作为 scope，提取描述作 message
        # ⚠️ 命令替换必须 || true 兜底：无 [XX-NN] tag 的任务会让 grep 无匹配返回非0，
        #    set -e + pipefail 会在这一行直接杀死 main_loop —— 表现为「每轮主进程退出
        #    + commit 永远跳过」(git add 已执行但 commit 走不到)。这是与 sed/grep -c
        #    兜底写法一致的本意修复。
        local task_tag task_desc commit_msg
        task_tag=$(echo "$CURRENT_TASK" | grep -oE '\[[A-Z]+-[0-9]+\]' | head -1 | tr -d '[]' || true)
        task_desc=$(echo "$CURRENT_TASK" | sed 's/^- \[[ x]\] //; s/\[[A-Z]*-[0-9]*\] *//' | head -c 200 || true)
        if [ -n "$task_tag" ]; then
          commit_msg="feat($task_tag): $task_desc"
        else
          commit_msg="feat: $task_desc"
        fi
        if git commit -m "$commit_msg" -m "Co-Authored-By: Claude <noreply@anthropic.com>" --no-verify --quiet 2>>"$LOG_FILE"; then
          log "📦 已提交: $commit_msg"
        else
          log "⚠️ 提交失败"
        fi
      else
        log "📦 无改动，跳过提交"
      fi
    fi

    # 每 5 轮普通压缩一次；压缩后下一轮 Prompt 会强制重读 .task.md（见上）
    # 用 --resume <SESSION_ID> 续接同一新会话做压缩（不能 --continue，理由见会话策略注释）
    if [ $((LOOP_COUNT % 5)) -eq 0 ]; then
      log "🗜️ 压缩会话..."
      if [ -n "$SESSION_ID" ]; then
        echo "/compact" | claude --resume "$SESSION_ID" --dangerously-skip-permissions 2>> "$LOG_FILE" || true
      else
        echo "/compact" | claude --dangerously-skip-permissions 2>> "$LOG_FILE" || true
      fi
      log "🗜️ 压缩完成"
    fi

    sleep 5
  done
}

# -------- 首次任务拆解 --------

bootstrap_tasks() {
  local GOAL="$1"
  log "首次运行，拆解任务..."
  log "目标：$GOAL"

  claude -p "根据用户目标拆解为最小可执行任务列表。
用户目标：$GOAL
要求：
- 每个任务足够小（一个函数、一个文件、一个接口）
- 按依赖顺序排列
- 输出格式只有任务列表，每行一个：
- [ ] 任务描述
- [ ] 任务描述
..." --dangerously-skip-permissions 2>> "$LOG_FILE" > "${TASK_FILE}.raw"

  grep '^- \[' "${TASK_FILE}.raw" > "$TASK_FILE" 2>/dev/null || true
  rm -f "${TASK_FILE}.raw"

  local TASK_COUNT
  TASK_COUNT=$(grep -c '\[ \]' "$TASK_FILE" 2>/dev/null || echo 0)
  if [ "$TASK_COUNT" -eq 0 ]; then
    log "❌ 任务拆解失败"
    rm -f "$TASK_FILE"
    exit 1
  fi
  log "✅ 共 $TASK_COUNT 个任务"
}

# -------- 状态 / 报告 --------

show_status() {
  if [ -f "$STATUS_FILE" ]; then
    cat "$STATUS_FILE"
  else
    echo "暂无状态文件"
  fi
  echo ""
  echo "进程状态："
  is_running "$MAIN_PID_FILE"  && echo "  主进程: 运行中 PID=$(cat "$MAIN_PID_FILE")"  || echo "  主进程: 未运行"
  is_running "$WDOG_PID_FILE" && echo "  看门狗: 运行中 PID=$(cat "$WDOG_PID_FILE")" || echo "  看门狗: 未运行"
}

show_report() {
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "📊 无人值守运行报告"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""

  if [ -f "$TASK_FILE" ]; then
    local total done
    total=$(grep -c '^- \[' "$TASK_FILE" 2>/dev/null || echo 0)
    done=$(grep -c '\[x\]' "$TASK_FILE" 2>/dev/null || echo 0)
    echo "任务进度: $done / $total 已完成"
    echo ""
  fi

  if [ -f "$STATUS_FILE" ]; then
    echo "最后状态:"
    cat "$STATUS_FILE"
    echo ""
  fi

  echo "最耗时的 10 轮："
  grep "第.*轮结束" "$LOG_FILE" 2>/dev/null | sort -t'耗时' -k2 -n -r | head -10 || echo "（无数据）"
  echo ""

  echo "异常/报错："
  grep -i "错误\|失败\|❌\|异常\|timeout\|假死" "$LOG_FILE" 2>/dev/null | tail -20 || echo "（未发现）"
  echo ""

  echo "看门狗日志："
  grep "\[WATCHDOG\]" "$LOG_FILE" 2>/dev/null | tail -10 || echo "（无）"
}

stop_all() {
  log "收到停止指令"
  local wdog_pid
  wdog_pid=$(cat "$WDOG_PID_FILE" 2>/dev/null || echo 0)
  if is_running "$MAIN_PID_FILE"; then
    kill_tree "$(cat "$MAIN_PID_FILE")" "$wdog_pid"
    rm -f "$MAIN_PID_FILE"
    echo "主进程已停止"
  fi
  if is_running "$WDOG_PID_FILE"; then
    kill_tree "$wdog_pid"
    rm -f "$WDOG_PID_FILE"
    echo "看门狗已停止"
  fi
}

# -------- 入口 --------

case "${1:-}" in
  --stop)
    stop_all
    exit 0
    ;;
  --status)
    show_status
    exit 0
    ;;
  --report)
    show_report
    exit 0
    ;;
  --watchdog)
    # 内部使用：看门狗模式
    watchdog_mode
    exit 0
    ;;
  --internal-resume)
    # 内部使用：看门狗拉起主进程时的入口
    main_loop
    exit 0
    ;;
esac

# 正常启动流程
GOAL="${1:-}"

# 首次运行需要目标
if [ ! -f "$TASK_FILE" ]; then
  [ -z "$GOAL" ] && { echo "首次运行需要指定目标，例如："; echo "  ./unattended.sh \"构建一个Go REST API\""; exit 1; }
  bootstrap_tasks "$GOAL"
fi

mkdir -p "$MEMO_DIR"

# 如果看门狗没跑，自动拉起
if ! is_running "$WDOG_PID_FILE"; then
  nohup "$0" --watchdog > /dev/null 2>&1 &
  echo $! > "$WDOG_PID_FILE"
  log "看门狗已启动 PID=$!"
  sleep 2
fi

# 进入主循环
main_loop
