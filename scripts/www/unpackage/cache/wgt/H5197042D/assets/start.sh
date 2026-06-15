#!/data/data/com.termux/files/usr/bin/bash
# ============================================
#  视频下载服务 - 后台启动脚本
#  用法: bash start.sh
#        bash start.sh stop   停止服务
#        bash start.sh status 查看状态
# ============================================

PID_FILE="$HOME/video-downloader/.server.pid"
LOG_FILE="$HOME/video-downloader/server.log"

cd ~/video-downloader

# ── 停止服务 ──
if [ "$1" = "stop" ]; then
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID"
            rm -f "$PID_FILE"
            echo "✅ 服务已停止 (PID: $PID)"
        else
            rm -f "$PID_FILE"
            echo "⚠️  PID 文件存在但进程已不在，已清理"
        fi
    else
        echo "⚠️  服务未运行"
    fi
    termux-wake-unlock 2>/dev/null
    exit 0
fi

# ── 查看状态 ──
if [ "$1" = "status" ]; then
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "✅ 服务运行中 (PID: $PID)"
            echo "   地址: http://127.0.0.1:5000"
            exit 0
        fi
    fi
    echo "❌ 服务未运行"
    exit 1
fi

# ── 检查是否已运行 ──
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "⚠️  服务已在运行 (PID: $PID)"
        echo "   地址: http://127.0.0.1:5000"
        echo "   停止: bash start.sh stop"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

# ── 检查 app.py ──
if [ ! -f "app.py" ]; then
    echo "❌ 未找到 app.py，请先放到 ~/video-downloader/"
    exit 1
fi

# ── 存储权限 ──
if [ ! -d ~/storage/downloads ]; then
    echo "请允许存储权限..."
    termux-setup-storage
    sleep 3
fi

# ── 创建下载目录 ──
mkdir -p ~/storage/movies/video-downloader 2>/dev/null
mkdir -p ~/storage/dcim/video-downloader 2>/dev/null
mkdir -p ~/storage/downloads/video-downloader 2>/dev/null

# ── 防止被系统杀后台 ──
termux-wake-lock acquire 2>/dev/null || termux-wake-lock 2>/dev/null

# ── 后台启动 ──
echo "🚀 正在后台启动服务..."
nohup python app.py > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

# 等 2 秒确认启动
sleep 2
if kill -0 "$SERVER_PID" 2>/dev/null; then
    echo ""
    echo "========================================"
    echo "  ✅ 服务已启动 (PID: $SERVER_PID)"
    echo "  地址: http://127.0.0.1:5000"
    echo "  下载: ~/storage/downloads/video-downloader"
    echo "========================================"
    echo ""
    echo "  💡 现在打开 App 或浏览器即可使用"
    echo "  💡 服务在后台运行，可以关掉 Termux 窗口"
    echo ""
    echo "  管理命令:"
    echo "    bash start.sh status  查看状态"
    echo "    bash start.sh stop    停止服务"
    echo ""
else
    echo "❌ 启动失败，查看日志: cat $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi
