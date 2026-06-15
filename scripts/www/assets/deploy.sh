#!/data/data/com.termux/files/usr/bin/bash
# ============================================
#  自动部署脚本 - 由 App「启动服务」按钮触发
#  首次运行: 安装所有依赖 + 配置环境
#  后续运行: 更新文件 + 重启服务
# ============================================

PROJECT_DIR="$HOME/video-downloader"
DOWNLOADS="$HOME/storage/downloads"
PID_FILE="$PROJECT_DIR/.server.pid"
SETUP_DONE="$PROJECT_DIR/.setup_done_v2"
LOG_FILE="$PROJECT_DIR/server.log"

echo "========================================"
echo "  视频下载工具 - 自动部署"
echo "========================================"
echo ""

# ── 1. 创建目录 ──
mkdir -p "$PROJECT_DIR" "$PROJECT_DIR/videos"
mkdir -p ~/.termux

# ── 2. 请求存储权限（放在复制文件之前！）──
echo "[1/5] 检查存储权限..."
if [ ! -d "$DOWNLOADS" ]; then
    echo "  ⚠️ 首次需授权，请在弹窗中点「允许」"
    termux-setup-storage
    sleep 4
fi

if [ -d "$DOWNLOADS" ]; then
    echo "  ✅ 存储已就绪"
else
    echo "  ⚠️ 存储权限未获取，将保存到 Termux 内部目录"
fi

# ── 3. 从 Downloads 复制/更新文件（存储就绪后才执行）──
echo "[2/5] 更新脚本文件..."
if [ -d "$DOWNLOADS" ]; then
    for f in app.py start.sh setup.sh deploy.sh; do
        if [ -f "$DOWNLOADS/$f" ]; then
            cp "$DOWNLOADS/$f" "$PROJECT_DIR/$f" 2>/dev/null
            echo "  ✅ $f"
        fi
    done
else
    echo "  ⚠️ 存储不可用，跳过文件更新"
fi
chmod +x "$PROJECT_DIR/start.sh" "$PROJECT_DIR/deploy.sh" 2>/dev/null

# ── 4. 配置 allow-external-apps（允许 App 调 Termux）──
echo "[3/5] 配置外部调用权限..."
if ! grep -q "^allow-external-apps=true" ~/.termux/termux.properties 2>/dev/null; then
    sed -i '/^#\s*allow-external-apps/d' ~/.termux/termux.properties 2>/dev/null
    echo "allow-external-apps=true" >> ~/.termux/termux.properties
    echo "  ✅ 已配置（需重启 Termux 才能生效）"
else
    echo "  ✅ 已就绪"
fi

# ── 5. 首次安装依赖（有标记文件则跳过）──
if [ ! -f "$SETUP_DONE" ]; then
    echo "[4/5] 首次安装依赖（约 3-8 分钟）..."
    pkg update -y 2>/dev/null
    pkg upgrade -y -o Dpkg::Options::="--force-confnew" 2>/dev/null
    pkg install -y python ffmpeg wget 2>/dev/null
    pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ 2>/dev/null
    pip install flask flask-cors yt-dlp requests 2>/dev/null
    touch "$SETUP_DONE"
    echo "  ✅ 依赖安装完成"
else
    echo "[4/5] 依赖已安装，跳过"
fi

# ── 6. 确保下载目录 ──
mkdir -p "$DOWNLOADS/video-downloader" 2>/dev/null
mkdir -p ~/storage/movies/video-downloader 2>/dev/null
mkdir -p ~/storage/dcim/video-downloader 2>/dev/null

# ── 7. 启动服务 ──
echo "[5/5] 启动后端服务..."
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

cd "$PROJECT_DIR"
nohup python app.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo ""
    echo "========================================"
    echo "  ✅ 服务启动成功!"
    echo "  📡 地址: http://127.0.0.1:5000"
    echo "  📁 下载: $DOWNLOADS/video-downloader/"
    echo "========================================"
    echo ""
    echo "  💡 可以返回 App 了"
else
    echo "❌ 启动失败，查看: cat $LOG_FILE"
    tail -20 "$LOG_FILE" 2>/dev/null
    exit 1
fi
