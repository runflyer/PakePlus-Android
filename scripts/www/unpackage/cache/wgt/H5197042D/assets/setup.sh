#!/data/data/com.termux/files/usr/bin/bash
# ============================================
#  视频下载工具 - Termux 一键安装脚本
#  在 Termux 中运行: bash setup.sh
#  前置步骤: 把 app.py 放到手机 Download 文件夹
# ============================================

echo "========================================"
echo "  视频下载工具 - 手机端安装"
echo "========================================"
echo ""

# 更新软件源
echo "[1/6] 更新软件源..."
pkg update -y && pkg upgrade -y

# 安装核心依赖
echo "[2/6] 安装 Python 和 ffmpeg..."
pkg install -y python ffmpeg wget

# 授予存储权限
echo "[3/6] 配置存储权限..."
termux-setup-storage
sleep 2

# 设置 pip 镜像源（国内加速）
echo "[4/6] 安装 Python 包..."
pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
pip install flask flask-cors yt-dlp requests

# 创建项目目录
echo "[5/6] 创建项目结构..."
mkdir -p ~/video-downloader/videos
mkdir -p ~/storage/downloads/video-downloader

# 复制 app.py（从手机 Download 目录）
echo "[6/6] 查找并复制 app.py..."
if [ -f ~/storage/downloads/app.py ]; then
    cp ~/storage/downloads/app.py ~/video-downloader/app.py
    echo "  ✅ 已从 Download 文件夹复制 app.py"
elif [ -f ~/storage/shared/Download/app.py ]; then
    cp ~/storage/shared/Download/app.py ~/video-downloader/app.py
    echo "  ✅ 已从 Download 文件夹复制 app.py"
else
    echo "  ⚠️  未找到 app.py，请手动放到 ~/video-downloader/ 目录"
    echo "  方法1: 把 app.py 保存到手机 Download 文件夹，然后重新运行本脚本"
    echo "  方法2: cp ~/storage/downloads/app.py ~/video-downloader/"
fi

# 启用外部应用调用 Termux 执行命令（App 一键启动服务依赖此配置）
echo ""
echo "[7/7] 配置 Termux 允许外部应用调用..."
mkdir -p ~/.termux
if [ -f ~/.termux/termux.properties ]; then
    if grep -q "allow-external-apps" ~/.termux/termux.properties; then
        sed -i 's/^#*allow-external-apps.*/allow-external-apps=true/' ~/.termux/termux.properties
    else
        echo "allow-external-apps=true" >> ~/.termux/termux.properties
    fi
else
    echo "allow-external-apps=true" > ~/.termux/termux.properties
fi
echo "  ✅ 已配置 allow-external-apps=true"
echo "  📱 请重启 Termux 应用使配置生效（完全退出再打开）"

echo ""
echo "========================================"
echo "  ✅ 安装完成！"
echo "========================================"
echo ""
echo "使用方法:"
echo "  方式1（推荐）: 打开打包好的App → 点「🚀 启动服务」"
echo "  方式2（手动）: cd ~/video-downloader && bash start.sh"
echo ""
echo "⚠️  重要：安装完成后请完全退出Termux再重新打开！"
echo ""
echo "下载的视频在: 手机存储/Download/video-downloader/"
