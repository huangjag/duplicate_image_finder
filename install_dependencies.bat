@echo off
setlocal
chcp 65001 >nul
title 重复图片查找工具 - 依赖自动安装

REM 切换到脚本所在目录, 支持双击运行
cd /d "%~dp0"

if not exist "requirements.txt" (
    echo [错误] 未找到 requirements.txt, 请把本脚本放在项目根目录下再运行。
    pause
    exit /b 1
)

REM 优先使用 PATH 中的 python, 不行再退回 py 启动器
set "PY_CMD=python"
%PY_CMD% --version >nul 2>&1
if errorlevel 1 (
    set "PY_CMD=py -3"
    py -3 --version >nul 2>&1
    if errorlevel 1 (
        echo [错误] 未检测到 Python, 请先安装 Python 3.x 并勾选 "Add Python to PATH"。
        pause
        exit /b 1
    )
)

echo ==============================================
echo   重复图片查找工具 - 自动安装依赖
echo ==============================================
echo 使用解释器:
%PY_CMD% --version
echo.

echo [1/2] 正在安装 requirements.txt 中的依赖 ...
%PY_CMD% -m pip install -r "requirements.txt"
if errorlevel 1 (
    echo.
    echo [提示] 默认源安装失败, 自动改用清华镜像源重试 ...
    %PY_CMD% -m pip install -r "requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo.
        echo [错误] pip 安装失败, 请检查网络后手动执行:
        echo     %PY_CMD% -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

echo.
echo [2/2] 正在验证依赖是否可正常导入 ...
%PY_CMD% -c "import PIL, imagehash, numpy, tqdm; print('  pillow    ', PIL.__version__); print('  imagehash ', imagehash.__version__); print('  numpy     ', numpy.__version__); print('  tqdm      ', tqdm.__version__)"
if errorlevel 1 (
    echo.
    echo [错误] 部分依赖导入失败, 请把上方的报错信息发给开发者。
    pause
    exit /b 1
)

echo.
echo ==============================================
echo   [完成] 所有依赖已安装并验证通过, 运行方式:
echo     python duplicate_image_finder_v2.py
echo ==============================================
pause
exit /b 0
